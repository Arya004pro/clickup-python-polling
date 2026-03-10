"""Cron Report Trigger - 9:00 AM IST (3:30 AM UTC). Sends yesterday's report."""

import time
from datetime import datetime, timezone
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
    triggered_at_epoch_ms = int(time.time() * 1000)
    triggered_at_iso = datetime.now(timezone.utc).isoformat()
    ctx.logger.info("Cron 9AM triggered - generating yesterday's report")
    await ctx.enqueue(
        {
            "topic": "report::generate",
            "data": {
                "period": "yesterday",
                "schedule_label": "9AM Yesterday",
                "trigger_source": "cron_9am",
                "triggered_at_epoch_ms": triggered_at_epoch_ms,
                "triggered_at_iso": triggered_at_iso,
            },
        }
    )
