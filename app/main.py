"""
FastAPI Application - ClickUp to PostgreSQL Sync
"""

from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.logging_config import setup_logging
from app.clickup import fetch_all_tasks_from_team
from app.sync import sync_tasks_to_supabase
from app.scheduler import start_scheduler
from app.employee_sync import sync_employees_to_supabase
from app.supabase_db import (
    get_all_employees,
    get_tasks_by_employee_id,
    get_all_tasks,
    get_task_by_id,
    get_tasks_with_time,
    get_tasks_with_comments,
    get_daily_sync_tasks,
)
from app.daily_sync import sync_daily_updated_tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    start_scheduler()
    yield


app = FastAPI(title="ClickUp Sync API", lifespan=lifespan)


# -------------------------------------------------
# Sync Endpoints
# -------------------------------------------------
@app.get("/sync/tasks", tags=["Sync"])
def sync_tasks():
    """Trigger full sync manually."""
    import logging
    from app import scheduler

    logger = logging.getLogger("scheduler")
    if scheduler._sync_in_progress:
        logger.info("⏳ Sync already in progress, manual trigger skipped.")
        return {"status": "skipped", "reason": "Sync already in progress"}
    scheduler._sync_in_progress = True
    try:
        tasks = fetch_all_tasks_from_team()
        synced_count = sync_tasks_to_supabase(tasks, full_sync=True)
        result = {
            "status": "success",
            "tasks_synced": synced_count,
        }
        # Update scheduler state to prevent immediate full sync
        if tasks:
            scheduler._initial_sync_done = True
            scheduler._last_sync_ms = max(
                int(task["date_updated"]) for task in tasks if task.get("date_updated")
            )
    except Exception as e:
        logger.error("❌ Manual sync failed", exc_info=True)
        result = {"status": "error", "reason": str(e)}
    finally:
        scheduler._sync_in_progress = False
    return result


@app.get("/sync/employees", tags=["Sync"])
def sync_employees():
    """Sync employees from ClickUp."""
    return {"employees_synced": sync_employees_to_supabase()}


@app.get("/sync/daily", tags=["Sync"])
def sync_daily():
    """Sync tasks updated today to daily_syncs table."""
    try:
        tasks_synced = sync_daily_updated_tasks()
        return {"status": "success", "tasks_synced": tasks_synced}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


# -------------------------------------------------
# Employees
# -------------------------------------------------
@app.get("/employees", tags=["Employees"])
def list_employees():
    """Get all employees."""
    employees = get_all_employees()
    return {"count": len(employees), "employees": employees}


# -------------------------------------------------
# Tasks
# -------------------------------------------------
@app.get("/tasks", tags=["Tasks"])
def list_tasks(limit: int = 100, offset: int = 0):
    """Get all tasks with pagination."""
    tasks = get_all_tasks(limit, offset)
    return {"count": len(tasks), "limit": limit, "offset": offset, "tasks": tasks}


@app.get("/tasks/by-employee", tags=["Tasks"])
def tasks_by_employee(employee_id: str):
    """Get tasks assigned to an employee."""
    tasks = get_tasks_by_employee_id(employee_id)
    return {"employee_id": employee_id, "count": len(tasks), "tasks": tasks}


@app.get("/tasks/with-time", tags=["Tasks"])
def tasks_with_time():
    """Get tasks that have tracked time."""
    tasks = get_tasks_with_time()
    return {"count": len(tasks), "tasks": tasks}


@app.get("/tasks/with-comments", tags=["Tasks"])
def tasks_with_comments():
    """Get tasks with assigned comments."""
    tasks = get_tasks_with_comments()
    return {"count": len(tasks), "tasks": tasks}


@app.get("/tasks/{task_id}", tags=["Tasks"])
def get_task(task_id: str):
    """Get a single task by ID."""
    task = get_task_by_id(task_id)
    return task if task else {"error": "Task not found"}


# -------------------------------------------------
# Daily Sync
# -------------------------------------------------
@app.get("/daily-sync", tags=["Daily Sync"])
def list_daily_sync_tasks():
    """Get all tasks from daily sync table."""
    tasks = get_daily_sync_tasks()
    return {"count": len(tasks), "tasks": tasks}


@app.get("/dependencies/{task_id}", tags=["Tasks"])
def get_dependencies(task_id: str):
    """Get dependencies for a single task by ID."""
    task = get_task_by_id(task_id)
    if not task:
        return {"error": "Task not found"}
    return {"task_id": task_id, "dependencies": task.get("dependencies")}
