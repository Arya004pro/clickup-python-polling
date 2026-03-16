"""Cron Report Trigger - 9:00 AM IST (3:30 AM UTC).

Rules:
- Sunday (IST): skip.
- Monday (IST): generate Saturday report.
- Tue-Sat (IST): generate yesterday report.
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from motia import FlowContext, cron

IST = timezone(timedelta(hours=5, minutes=30))

config = {
    "name": "CronReport9AM",
    "description": "Triggers morning catch-up report at 9:00 AM IST",
    "flows": ["clickup-daily-reports"],
    "triggers": [cron("0 30 3 * * *")],
    "enqueues": ["report::generate"],
}


async def handler(input_data: None, ctx: FlowContext[Any]) -> None:
    now_ist = datetime.now(IST)
    weekday_ist = now_ist.weekday()  # Monday=0 ... Sunday=6
    if weekday_ist == 6:
        ctx.logger.info("Cron 9AM skipped - Sunday holiday (IST)")
        return

    triggered_at_epoch_ms = int(time.time() * 1000)
    triggered_at_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "period": "yesterday",
        "schedule_label": "9AM Yesterday",
        "trigger_source": "cron_9am",
        "triggered_at_epoch_ms": triggered_at_epoch_ms,
        "triggered_at_iso": triggered_at_iso,
    }

    if weekday_ist == 0:
        saturday_date = (now_ist.date() - timedelta(days=2)).isoformat()
        payload.update(
            {
                "period": "custom",
                "custom_start": saturday_date,
                "custom_end": saturday_date,
                "schedule_label": "9AM Saturday (Monday Catch-up)",
            }
        )
        ctx.logger.info(
            f"Cron 9AM triggered - Monday catch-up, generating Saturday report ({saturday_date})"
        )
    else:
        ctx.logger.info("Cron 9AM triggered - generating yesterday's report")

    await ctx.enqueue(
        {
            "topic": "report::generate",
            "data": payload,
        }
    )
