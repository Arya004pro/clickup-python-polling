from apscheduler.schedulers.background import BackgroundScheduler
from app.clickup import fetch_all_tasks_from_space
from app.sync import sync_tasks_to_supabase
from app.config import CLICKUP_SPACE_ID
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler")
scheduler = BackgroundScheduler()


def scheduled_sync():
    logger.info("⏳ Scheduler triggered")
    try:
        tasks = fetch_all_tasks_from_space(CLICKUP_SPACE_ID)
        count = sync_tasks_to_supabase(tasks)
        logger.info(f"Scheduler synced {count} tasks")
    except Exception as e:
        logger.error(f"Scheduler sync failed: {e}")


def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        scheduled_sync,
        trigger="interval",
        minutes=2,   # ⏱ adjust later (1–5 min recommended)
        id="clickup_sync_job",
        replace_existing=True
    )
    scheduler.start()
