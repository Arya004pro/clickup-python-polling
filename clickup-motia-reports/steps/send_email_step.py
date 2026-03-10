"""
Send Email Step - queue-triggered, formats and sends the report email.

Receives OpenRouter-generated markdown reports from the generate step, builds
an HTML email (same styling as localhost page) with a combined .md attachment,
and sends via Gmail SMTP.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from motia import FlowContext, queue


config = {
    "name": "SendReportEmail",
    "description": "Renders OpenRouter markdown reports as HTML email + .md attachment and sends via SMTP",
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
    from steps.config import (
        API_SERVER_URL,
        SMTP_EMAIL,
        SMTP_HOST,
        SMTP_PASSWORD,
        SMTP_PORT,
        SMTP_TO,
    )
    from steps.email_sender import send_report_email

    reports_markdown = input_data.get("reports_markdown", [])
    schedule_label = input_data.get("schedule_label", "Report")
    period = str(input_data.get("period", "") or "")
    timing_meta = dict(input_data.get("timing_meta") or {})
    email_started_epoch_ms = int(time.time() * 1000)
    email_started_iso = datetime.now(timezone.utc).isoformat()

    if not SMTP_EMAIL or not SMTP_PASSWORD:
        ctx.logger.error(
            "SMTP credentials not configured. Set SMTP_EMAIL and SMTP_PASSWORD env vars."
        )
        return

    ok_count = sum(
        1 for r in reports_markdown if not r.get("error") and r.get("markdown")
    )
    err_count = sum(1 for r in reports_markdown if r.get("error"))
    ctx.logger.info(
        f"Sending email - {len(reports_markdown)} spaces "
        f"({ok_count} OK, {err_count} errors), label={schedule_label}"
    )
    if timing_meta:
        trigger_iso = timing_meta.get("triggered_at_iso", "unknown")
        trigger_source = timing_meta.get("trigger_source", "unknown")
        generation_elapsed = timing_meta.get("generation_elapsed_s")
        ctx.logger.info(
            "Timing before email - "
            f"source={trigger_source}, triggered_at={trigger_iso}, "
            f"generation_elapsed_s={generation_elapsed}"
        )

    trigger_source = str(timing_meta.get("trigger_source") or "unknown")
    dedup_window_s = (
        _CRON_EMAIL_DEDUP_WINDOW_S
        if trigger_source.startswith("cron")
        else _MANUAL_EMAIL_DEDUP_WINDOW_S
    )
    dedup_period = str(timing_meta.get("period") or period or "unknown")
    dedup_key = f"{schedule_label}::{dedup_period}::{'cron' if trigger_source.startswith('cron') else 'manual'}"
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

    result = send_report_email(
        reports_markdown=reports_markdown,
        schedule_label=schedule_label,
        smtp_host=SMTP_HOST,
        smtp_port=SMTP_PORT,
        smtp_email=SMTP_EMAIL,
        smtp_password=SMTP_PASSWORD,
        to_email=SMTP_TO,
        pdf_render_base_url=API_SERVER_URL,
    )

    if result["status"] == "sent":
        ctx.logger.info(f"[OK] Email sent to {result['to']} - {result['subject']}")
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
