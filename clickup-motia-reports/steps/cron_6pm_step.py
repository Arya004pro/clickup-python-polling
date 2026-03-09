"""Cron Report Trigger — 6:00 PM IST (12:30 PM UTC). Sends today's EOD report."""

from typing import Any
from motia import FlowContext, cron

config = {
    "name": "CronReport6PM",
    "description": "Triggers today's end-of-day space report at 6:00 PM IST",
    "flows": ["clickup-daily-reports"],
    "triggers": [cron("0 30 12 * * *")],
    "enqueues": ["report::generate"],
}


async def handler(input_data: None, ctx: FlowContext[Any]) -> None:
    ctx.logger.info("Cron 6PM triggered — generating today's EOD report")
    await ctx.enqueue(
        {
            "topic": "report::generate",
            "data": {"period": "today", "schedule_label": "6PM Today"},
        }
    )
