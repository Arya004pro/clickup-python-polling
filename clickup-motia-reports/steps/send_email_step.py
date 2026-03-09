"""
Send Email Step — queue-triggered, formats and sends the report email.

Receives OpenRouter-generated markdown reports from the generate step, builds
an HTML email (same styling as localhost page) with a combined .md attachment,
and sends via Gmail SMTP.
"""

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


async def handler(input_data: dict, ctx: FlowContext[Any]) -> None:
    """Format and send the report email."""
    from steps.config import SMTP_HOST, SMTP_PORT, SMTP_EMAIL, SMTP_PASSWORD, SMTP_TO
    from steps.email_sender import send_report_email

    # New format: list of {"space": str, "markdown": str|None, "error": str|None}
    reports_markdown = input_data.get("reports_markdown", [])
    schedule_label = input_data.get("schedule_label", "Report")

    if not SMTP_EMAIL or not SMTP_PASSWORD:
        ctx.logger.error(
            "SMTP credentials not configured! Set SMTP_EMAIL and SMTP_PASSWORD env vars."
        )
        return

    ok_count = sum(
        1 for r in reports_markdown if not r.get("error") and r.get("markdown")
    )
    err_count = sum(1 for r in reports_markdown if r.get("error"))
    ctx.logger.info(
        f"Sending email — {len(reports_markdown)} spaces "
        f"({ok_count} OK, {err_count} errors), label={schedule_label}"
    )

    result = send_report_email(
        reports_markdown=reports_markdown,
        schedule_label=schedule_label,
        smtp_host=SMTP_HOST,
        smtp_port=SMTP_PORT,
        smtp_email=SMTP_EMAIL,
        smtp_password=SMTP_PASSWORD,
        to_email=SMTP_TO,
    )

    if result["status"] == "sent":
        ctx.logger.info(f"[OK] Email sent to {result['to']} — {result['subject']}")
    else:
        ctx.logger.error(f"[FAIL] Email failed: {result.get('error')}")

    # Store result in state for observability
    await ctx.state.set("email_results", schedule_label, result)
