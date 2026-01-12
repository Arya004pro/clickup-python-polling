import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.memory import MemoryJobStore

from app.employee_sync import sync_employees_to_supabase
from app.sync import sync_tasks_to_supabase
from app.clickup import (
    fetch_all_tasks_from_team,
    fetch_all_tasks_updated_since_team,
    clear_space_cache,
)


# -------------------------------------------------
# Logging
# -------------------------------------------------
logger = logging.getLogger("scheduler")
logger.setLevel(logging.INFO)
logger.propagate = True


# -------------------------------------------------
# Scheduler state (SINGLE SOURCE OF TRUTH)
# -------------------------------------------------
_scheduler: BackgroundScheduler | None = None
_last_sync_ms: int | None = None  # ClickUp timestamps are ms
_run_count: int = 0


# -------------------------------------------------
# Job logic
# -------------------------------------------------
def scheduled_sync():
    """
    Stable scheduler logic
    """
    global _last_sync_ms, _run_count

    logger.info("⏳ Scheduler triggered")

    try:
        # -------------------------------------------------
        # 1️⃣ Sync employees FIRST
        # -------------------------------------------------
        emp_count = sync_employees_to_supabase()
        logger.info(f"👥 Synced {emp_count} employees")

        # -------------------------------------------------
        # 2️⃣ Task sync (ALL SPACES)
        # -------------------------------------------------
        do_full_sync = _last_sync_ms is None or _run_count % 12 == 0

        # Always clear cache to pick up new spaces/lists/tasks
        clear_space_cache()

        if do_full_sync:
            logger.info("🔄 FULL task sync (all spaces)")
            tasks = fetch_all_tasks_from_team()
            synced = sync_tasks_to_supabase(tasks, full_sync=True)
        else:
            logger.info("⚡ Incremental task sync (all spaces)")
            BUFFER_MS = 2 * 60 * 1000
            tasks = fetch_all_tasks_updated_since_team(
                updated_after_ms=_last_sync_ms - BUFFER_MS,
            )
            synced = sync_tasks_to_supabase(tasks, full_sync=False)

        # NOTE: Time tracking is now handled by sync_tasks_to_supabase
        # using fetch_all_time_entries_batch - no separate time sync needed

        # -------------------------------------------------
        # 3️⃣ Advance cursor SAFELY
        # -------------------------------------------------
        if tasks:
            newest_update = max(
                int(task["date_updated"]) for task in tasks if task.get("date_updated")
            )
            _last_sync_ms = newest_update

        _run_count += 1

        logger.info(
            f"✅ Scheduler synced {synced} tasks "
            f"(run #{_run_count}, full={do_full_sync})"
        )

    except Exception:
        logger.error("❌ Scheduler sync failed", exc_info=True)


# -------------------------------------------------
# Scheduler bootstrap
# -------------------------------------------------
def start_scheduler():
    """
    Start scheduler safely (idempotent).
    """
    global _scheduler

    logger.info("🚀 Initializing scheduler")

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
        minutes=0.75,  # every 45 seconds
        id="clickup_sync_job",
        replace_existing=True,
        max_instances=1,  #  no overlap
        coalesce=True,  #  skip missed runs
    )

    _scheduler.start()

    logger.info(f"🚀 Scheduler running = {_scheduler.running}")
    logger.info(f"📌 Jobs = {[job.id for job in _scheduler.get_jobs()]}")
