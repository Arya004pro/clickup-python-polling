"""Send Email Step - queue-triggered, formats and sends the report email.

Receives markdown reports from the generate step and sends an HTML summary email.
Transport is selected via EMAIL_TRANSPORT env var (brevo_api | smtp | auto).
"""

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any

from motia import FlowContext, queue

from steps.email_sender import send_report_email


config = {
    "name": "SendReportEmail",
    "description": "Renders markdown reports as HTML and sends via Brevo API or SMTP",
    "flows": ["clickup-daily-reports"],
    "triggers": [
        queue("report::send-email"),
    ],
    "enqueues": [],
}

_EMAIL_DEDUP_LOCK = asyncio.Lock()
_CRON_EMAIL_DEDUP_WINDOW_S = 3600
_MANUAL_EMAIL_DEDUP_WINDOW_S = 120


async def handler(input_data: dict, ctx: FlowContext[Any]) -> None:
    """Format and send the report email."""
    from steps.config import SMTP_EMAIL, SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_TO

    reports_markdown = input_data.get("reports_markdown", [])
    schedule_label = input_data.get("schedule_label", "Report")
    period = str(input_data.get("period", "") or "")
    timing_meta = dict(input_data.get("timing_meta") or {})

    email_started_epoch_ms = int(time.time() * 1000)
    email_started_iso = datetime.now(timezone.utc).isoformat()

    ok_count = sum(1 for r in reports_markdown if not r.get("error") and r.get("markdown"))
    err_count = sum(1 for r in reports_markdown if r.get("error"))

    ctx.logger.info(
        f"Sending email - {len(reports_markdown)} spaces "
        f"({ok_count} OK, {err_count} errors), label={schedule_label}"
    )

    trigger_source = str(timing_meta.get("trigger_source") or "unknown")

    dedup_window_s = (
        _CRON_EMAIL_DEDUP_WINDOW_S
        if trigger_source.startswith("cron")
        else _MANUAL_EMAIL_DEDUP_WINDOW_S
    )

    dedup_period = str(timing_meta.get("period") or period or "unknown")
    dedup_key = (
        f"{schedule_label}::{dedup_period}::"
        f"{'cron' if trigger_source.startswith('cron') else 'manual'}"
    )

    now_ts = time.time()

    async with _EMAIL_DEDUP_LOCK:
        last_sent = await ctx.state.get("report_email_last_sent", dedup_key)

        if last_sent is not None:
            elapsed = now_ts - float(last_sent)
            if elapsed < dedup_window_s:
                ctx.logger.warning(
                    f"[DEDUP] Skipping duplicate email for '{dedup_key}' "
                    f"(last sent {elapsed:.0f}s ago, window={dedup_window_s}s)"
                )
                return

        await ctx.state.set("report_email_last_sent", dedup_key, now_ts)

    # PDF rendering via api-server (used by both transports)
    api_server_url = os.getenv("API_SERVER_URL", "").rstrip("/")

    result = send_report_email(
        reports_markdown=reports_markdown,
        schedule_label=schedule_label,
        smtp_host=SMTP_HOST or "",
        smtp_port=int(SMTP_PORT or 587),
        smtp_email=SMTP_EMAIL or "",
        smtp_password=SMTP_PASSWORD or "",
        to_email=SMTP_TO or "",
        pdf_render_base_url=api_server_url,
    )

    if result.get("status") == "sent":
        ctx.logger.info(f"[OK] Email sent to {result.get('to')} - {result.get('subject')}")
    else:
        ctx.logger.error(f"[FAIL] Email failed: {result.get('error')}")

    email_finished_epoch_ms = int(time.time() * 1000)
    email_elapsed_s = round((email_finished_epoch_ms - email_started_epoch_ms) / 1000, 2)

    trigger_epoch_ms = timing_meta.get("triggered_at_epoch_ms")
    end_to_end_s = None
    if trigger_epoch_ms is not None:
        try:
            end_to_end_s = round((email_finished_epoch_ms - int(trigger_epoch_ms)) / 1000, 2)
        except (TypeError, ValueError):
            end_to_end_s = None

    ctx.logger.info(
        "Pipeline timing - "
        f"email_elapsed_s={email_elapsed_s}, "
        f"end_to_end_s={end_to_end_s if end_to_end_s is not None else 'unknown'}"
    )

    result["timing"] = {
        "trigger_source": timing_meta.get("trigger_source"),
        "triggered_at_iso": timing_meta.get("triggered_at_iso"),
        "triggered_at_epoch_ms": timing_meta.get("triggered_at_epoch_ms"),
        "generation_started_iso": timing_meta.get("generation_started_iso"),
        "generation_finished_iso": timing_meta.get("generation_finished_iso"),
        "generation_elapsed_s": timing_meta.get("generation_elapsed_s"),
        "report_api_mode": timing_meta.get("report_api_mode"),
        "report_concurrency": timing_meta.get("report_concurrency"),
        "email_started_iso": email_started_iso,
        "email_finished_iso": datetime.now(timezone.utc).isoformat(),
        "email_elapsed_s": email_elapsed_s,
        "end_to_end_s": end_to_end_s,
    }

    await ctx.state.set("email_results", schedule_label, result)