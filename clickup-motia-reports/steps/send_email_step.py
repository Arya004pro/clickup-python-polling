"""Send Email Step - queue-triggered, formats and sends the report email.

Receives markdown reports from the generate step and sends an HTML summary email via SMTP.
"""

import asyncio
import smtplib
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any

from motia import FlowContext, queue


config = {
    "name": "SendReportEmail",
    "description": "Renders markdown reports as HTML and sends via SMTP",
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

    return f"<h2>ClickUp Report - {schedule_label}</h2>{''.join(html_sections)}"


def _send_email_smtp(subject, html_body, to_email, ctx):
    from steps.config import EMAIL_FROM, SMTP_EMAIL, SMTP_HOST, SMTP_PASSWORD, SMTP_PORT

    missing = []
    if not SMTP_HOST:
        missing.append("SMTP_HOST")
    if not SMTP_PORT:
        missing.append("SMTP_PORT")
    if not SMTP_EMAIL:
        missing.append("SMTP_EMAIL")
    if not SMTP_PASSWORD:
        missing.append("SMTP_PASSWORD")
    if not EMAIL_FROM:
        missing.append("EMAIL_FROM")
    if not to_email:
        missing.append("SMTP_TO")

    if missing:
        return {
            "status": "error",
            "error": f"Missing SMTP config: {', '.join(missing)}",
        }

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = to_email
    msg.set_content("This report email requires an HTML-capable mail client.")
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)
    except Exception as exc:
        return {"status": "error", "error": f"SMTP send failed: {exc}"}

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

    subject = f"ClickUp Report - {schedule_label}"

    result = _send_email_smtp(
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
