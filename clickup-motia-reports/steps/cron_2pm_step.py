"""Cron Report Trigger - 2:00 PM IST (8:30 AM UTC). Sends today's midday report."""

import time
from datetime import datetime, timezone
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
    triggered_at_epoch_ms = int(time.time() * 1000)
    triggered_at_iso = datetime.now(timezone.utc).isoformat()
    ctx.logger.info("Cron 2PM triggered - generating today's midday report")
    await ctx.enqueue(
        {
            "topic": "report::generate",
            "data": {
                "period": "today",
                "schedule_label": "2PM Today",
                "trigger_source": "cron_2pm",
                "triggered_at_epoch_ms": triggered_at_epoch_ms,
                "triggered_at_iso": triggered_at_iso,
            },
        }
    )
