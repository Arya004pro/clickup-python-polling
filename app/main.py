from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.logging_config import setup_logging
from app.clickup import (
    fetch_all_tasks_from_team,
    fetch_all_spaces,
    fetch_time_entries_for_task,
    clear_space_cache,
)
from app.time_tracking import aggregate_time_entries
from app.sync import sync_tasks_to_supabase
from app.scheduler import start_scheduler
from app.employee_sync import sync_employees_to_supabase
from app.supabase_db import get_all_employees, get_tasks_by_employee_id


# -------------------------------------------------
# Application lifespan (startup / shutdown)
# -------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    setup_logging()
    start_scheduler()
    yield
    # Shutdown
    # Scheduler stops automatically on process exit


app = FastAPI(lifespan=lifespan)


# -------------------------------------------------
# Debug / test endpoints (kept intentionally)
# -------------------------------------------------
@app.get("/test/spaces")
def test_spaces():
    """
    List all spaces in the team.
    """
    clear_space_cache()
    spaces = fetch_all_spaces()
    return {
        "total_spaces": len(spaces),
        "spaces": [{"id": s["id"], "name": s["name"]} for s in spaces],
    }


@app.get("/test/tasks")
def test_tasks():
    """
    Fetch all tasks from ALL spaces (debug endpoint).
    """
    clear_space_cache()
    tasks = fetch_all_tasks_from_team()
    return {
        "total_tasks": len(tasks),
        "sample": tasks[:2],
    }


@app.get("/test/time")
def test_time_aggregation():
    """
    Test time aggregation logic with dummy data.
    """
    sample = [
        {"start": 1700000000000, "end": 1700003600000, "duration": 3600000},
        {"start": 1700007200000, "end": 1700010800000, "duration": 3600000},
    ]
    return aggregate_time_entries(sample)


@app.get("/test/tasks-with-time")
def test_tasks_with_time():
    """
    Fetch a few tasks and show aggregated time data.
    """
    tasks = fetch_all_tasks_from_team()
    results = []

    for task in tasks[:5]:  # safety limit
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


# -------------------------------------------------
# Sync endpoints
# -------------------------------------------------
@app.get("/sync/tasks")
def sync_tasks():
    """
    Trigger full sync manually (ALL spaces).
    """
    clear_space_cache()
    tasks = fetch_all_tasks_from_team()
    count = sync_tasks_to_supabase(tasks, full_sync=True)
    return {
        "status": "success",
        "tasks_synced": count,
    }


@app.get("/test/sync")
def test_sync():
    """
    Debug sync endpoint.
    """
    tasks = fetch_all_tasks_from_team()
    count = sync_tasks_to_supabase(tasks, full_sync=True)
    return {
        "tasks_fetched": len(tasks),
        "tasks_synced": count,
    }


@app.get("/test/time/{task_id}")
def test_time_for_task(task_id: str):
    """
    Fetch and aggregate time entries for a single task.
    """
    entries = fetch_time_entries_for_task(task_id)
    aggregated = aggregate_time_entries(entries)

    return {
        "task_id": task_id,
        "entries_count": len(entries),
        "aggregated": aggregated,
        "sample_entries": entries[:3],
    }


@app.get("/sync/employees")
def sync_employees():
    count = sync_employees_to_supabase()
    return {"employees_synced": count}


@app.get("/employees", tags=["Employees"])
def get_employees():
    """
    Fetch all employees from database.
    """
    employees = get_all_employees()
    return {"count": len(employees), "employees": employees}


@app.get("/tasks/by-employee", tags=["Tasks"])
def get_tasks_by_employee(employee_id: str):
    """
    Fetch tasks assigned to a specific employee.
    """
    tasks = get_tasks_by_employee_id(employee_id)
    return {
        "employee_id": employee_id,
        "tasks_count": len(tasks),
        "tasks": tasks,
    }
