"""Cron Report Trigger — 2:00 PM IST (8:30 AM UTC). Sends today's midday report."""

from typing import Any
from motia import FlowContext, cron

config = {
    "name": "CronReport2PM",
    "description": "Triggers today's midday space report at 2:00 PM IST",
    "flows": ["clickup-daily-reports"],
    "triggers": [cron("0 30 8 * * *")],
    "enqueues": ["report::generate"],
}


async def handler(input_data: None, ctx: FlowContext[Any]) -> None:
    ctx.logger.info("Cron 2PM triggered — generating today's midday report")
    await ctx.enqueue(
        {
            "topic": "report::generate",
            "data": {"period": "today", "schedule_label": "2PM Today"},
        }
    )
