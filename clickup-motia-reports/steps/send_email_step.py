"""Send Email Step - queue-triggered, formats and sends the report email.

Receives markdown reports from the generate step, builds
an HTML email with attachment(s), and sends via Resend API.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import requests
from motia import FlowContext, queue


config = {
    "name": "SendReportEmail",
    "description": "Renders markdown reports as HTML email + attachments and sends via Resend API",
    "flows": ["clickup-daily-reports"],
    "triggers": [
        queue("report::send-email"),
    ],
    "enqueues": [],
}

_EMAIL_DEDUP_LOCK = asyncio.Lock()
_CRON_EMAIL_DEDUP_WINDOW_S = 3600
_MANUAL_EMAIL_DEDUP_WINDOW_S = 120


def _build_email_html(reports_markdown, schedule_label):
    html_sections = []

    for r in reports_markdown:
        name = r.get("space") or "Report"
        markdown = r.get("markdown") or ""
        error = r.get("error")

        if error:
            html_sections.append(f"<h3>{name}</h3><p><b>Error:</b> {error}</p>")
        else:
            html_sections.append(
                f"<h3>{name}</h3><pre style='white-space:pre-wrap'>{markdown}</pre>"
            )

    html_body = f"""
    <h2>📊 ClickUp Report — {schedule_label}</h2>
    {''.join(html_sections)}
    """

    return html_body


def _send_email_resend(subject, html_body, to_email, ctx):
    from steps.config import RESEND_API_KEY, EMAIL_FROM

    if not RESEND_API_KEY:
        ctx.logger.error("RESEND_API_KEY not configured.")
        return {"status": "error", "error": "Missing RESEND_API_KEY"}

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": EMAIL_FROM,
            "to": [to_email],
            "subject": subject,
            "html": html_body,
        },
        timeout=30,
    )

    if response.status_code >= 300:
        return {
            "status": "error",
            "error": f"Resend API error: {response.text}",
        }

    return {
        "status": "sent",
        "to": to_email,
        "subject": subject,
    }


async def handler(input_data: dict, ctx: FlowContext[Any]) -> None:
    """Format and send the report email."""
    from steps.config import SMTP_TO

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

    html_body = _build_email_html(reports_markdown, schedule_label)

    subject = f"📊 ClickUp Report — {schedule_label}"

    result = _send_email_resend(
        subject=subject,
        html_body=html_body,
        to_email=SMTP_TO,
        ctx=ctx,
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