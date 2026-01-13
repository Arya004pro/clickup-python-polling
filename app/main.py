"""
FastAPI Application - ClickUp to PostgreSQL Sync
"""

from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.logging_config import setup_logging
from app.clickup import fetch_all_tasks_from_team, clear_space_cache
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
)


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
    clear_space_cache()
    tasks = fetch_all_tasks_from_team()
    return {
        "status": "success",
        "tasks_synced": sync_tasks_to_supabase(tasks, full_sync=True),
    }


@app.get("/sync/employees", tags=["Sync"])
def sync_employees():
    """Sync employees from ClickUp."""
    return {"employees_synced": sync_employees_to_supabase()}


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
