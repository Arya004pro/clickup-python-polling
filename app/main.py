from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.clickup import (
    fetch_all_tasks_from_space,
    fetch_time_entries_for_task,
)
from app.time_tracking import aggregate_time_entries
from app.sync import sync_tasks_to_supabase
from app.config import CLICKUP_SPACE_ID
from app.scheduler import start_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ===== Startup =====
    start_scheduler()
    yield
    # ===== Shutdown =====
    # (Scheduler auto-stops on process exit)


app = FastAPI(lifespan=lifespan)

print(">>> SPACE_ID =", CLICKUP_SPACE_ID)


@app.get("/test/tasks")
def test_tasks():
    tasks = fetch_all_tasks_from_space(CLICKUP_SPACE_ID)
    return {"total_tasks": len(tasks), "sample": tasks[:2]}


@app.get("/test/time")
def test_time():
    sample = [
        {"start": 1700000000000, "end": 1700003600000, "duration": 3600000},
        {"start": 1700007200000, "end": 1700010800000, "duration": 3600000},
    ]
    return aggregate_time_entries(sample)


@app.get("/test/tasks-with-time")
def test_tasks_with_time():
    tasks = fetch_all_tasks_from_space(CLICKUP_SPACE_ID)
    results = []

    for task in tasks[:5]:  # limit for safety
        task_id = task["id"]

        time_entries = fetch_time_entries_for_task(task_id)
        aggregated = aggregate_time_entries(time_entries)

        results.append(
            {
                "task_id": task_id,
                "task_name": task.get("name"),
                "tracked_minutes": aggregated["tracked_minutes"],
                "start_time": aggregated["start_time"],
                "end_time": aggregated["end_time"],
            }
        )

    return results


@app.get("/sync/tasks")
def sync_tasks():
    tasks = fetch_all_tasks_from_space(CLICKUP_SPACE_ID)
    count = sync_tasks_to_supabase(tasks)
    return {"status": "success", "tasks_synced": count}


@app.get("/test/sync")
def test_sync():
    tasks = fetch_all_tasks_from_space(CLICKUP_SPACE_ID)
    count = sync_tasks_to_supabase(tasks)
    return {"tasks_fetched": len(tasks), "tasks_synced": count}


@app.get("/test/time/{task_id}")
def test_time_for_task(task_id: str):
    entries = fetch_time_entries_for_task(task_id)
    aggregated = aggregate_time_entries(entries)

    return {
        "task_id": task_id,
        "entries_count": len(entries),
        "aggregated": aggregated,
        "sample_entries": entries[:3],
    }
