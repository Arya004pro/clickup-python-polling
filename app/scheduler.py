import logging
import time
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from zoneinfo import ZoneInfo

from app.sync import sync_tasks_to_supabase
from app.clickup import (
    fetch_all_tasks_from_space,
    fetch_tasks_updated_since,
)
from app.config import CLICKUP_SPACE_ID

_scheduler = None
_last_sync_ms: int | None = None
_run_count = 0


# -------------------------------------------------
# Logging
# -------------------------------------------------
logger = logging.getLogger("scheduler")
logger.propagate = True
logger.setLevel(logging.INFO)

# -------------------------------------------------
# Scheduler state
# -------------------------------------------------
_scheduler = None
_last_sync_ms: int | None = None  # ClickUp uses ms timestamps


def scheduled_sync():
    """
    Phase-2 optimized sync:
    - Incremental sync normally
    - Full sync every 3rd run (~6 minutes)
    """
    global _last_sync_ms, _run_count

    logger.info("⏳ Scheduler triggered")

    try:
        do_full_sync = _last_sync_ms is None or _run_count % 3 == 0

        if do_full_sync:
            logger.info("🔄 FULL sync (deletion check enabled)")
            tasks = fetch_all_tasks_from_space(CLICKUP_SPACE_ID)

            synced = sync_tasks_to_supabase(
                tasks,
                full_sync=True,
            )
        else:
            logger.info(f"⚡ Incremental sync since {_last_sync_ms}")
            tasks = fetch_tasks_updated_since(
                CLICKUP_SPACE_ID,
                updated_after_ms=_last_sync_ms,
            )

            synced = sync_tasks_to_supabase(
                tasks,
                full_sync=False,
            )

        _last_sync_ms = int(time.time() * 1000)
        _run_count += 1

        logger.info(
            f"✅ Scheduler synced {synced} tasks "
            f"(run #{_run_count}, full={do_full_sync})"
        )

    except Exception:
        logger.error("❌ Scheduler sync failed", exc_info=True)


def start_scheduler():
    logger.info("🚀 Initializing scheduler")

    """
    Start scheduler safely (idempotent).
    """
    global _scheduler

    if _scheduler and _scheduler.running:
        logger.info("⚠️ Scheduler already running, skipping start")
        return

    _scheduler = BackgroundScheduler(
        jobstores={"default": MemoryJobStore()},
        executors={"default": ThreadPoolExecutor(max_workers=1)},
        timezone=ZoneInfo("Asia/Kolkata"),
    )

    _scheduler.add_job(
        scheduled_sync,
        trigger="interval",
        minutes=2,
        id="clickup_sync_job",
        replace_existing=True,
        max_instances=1,  # 🔒 prevent overlap
        coalesce=True,  # 🔁 skip missed runs
    )

    _scheduler.start()
    logger.info(f"🚀 Scheduler running = {_scheduler.running}")
    logger.info(f"📌 Jobs = {[job.id for job in _scheduler.get_jobs()]}")
