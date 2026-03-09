"""Cron Report Trigger — 9:00 AM IST (3:30 AM UTC). Sends yesterday's report."""

from typing import Any
from motia import FlowContext, cron

config = {
    "name": "CronReport9AM",
    "description": "Triggers yesterday's space report at 9:00 AM IST",
    "flows": ["clickup-daily-reports"],
    "triggers": [cron("0 30 3 * * *")],
    "enqueues": ["report::generate"],
}


async def handler(input_data: None, ctx: FlowContext[Any]) -> None:
    ctx.logger.info("Cron 9AM triggered — generating yesterday's report")
    await ctx.enqueue(
        {
            "topic": "report::generate",
            "data": {"period": "yesterday", "schedule_label": "9AM Yesterday"},
        }
    )
