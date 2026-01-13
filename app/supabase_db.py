"""
PostgreSQL Database Connection (Direct PG via psycopg2)
"""

import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager
from app.config import DATABASE_URL


@contextmanager
def get_connection():
    """
    Context manager for database connections.
    Automatically commits on success, rolls back on error.
    """
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_cursor(dict_cursor=True):
    """
    Context manager for database cursors.
    Uses RealDictCursor by default for dict-like row access.
    """
    with get_connection() as conn:
        cursor_factory = RealDictCursor if dict_cursor else None
        cursor = conn.cursor(cursor_factory=cursor_factory)
        try:
            yield cursor
        finally:
            cursor.close()


# =============================================================================
# EMPLOYEES TABLE
# =============================================================================
def get_all_employees() -> list[dict]:
    """Fetch all employees."""
    with get_cursor() as cur:
        cur.execute("SELECT id, name, email, role, clickup_user_id FROM employees")
        return [dict(row) for row in cur.fetchall()]


def get_employee_id_map() -> dict[str, str]:
    """Map clickup_user_id -> employee UUID."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT id, clickup_user_id FROM employees WHERE clickup_user_id IS NOT NULL"
        )
        return {str(row["clickup_user_id"]): str(row["id"]) for row in cur.fetchall()}


def upsert_employee(payload: dict) -> bool:
    """Upsert a single employee."""
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO employees (clickup_user_id, name, email, role)
            VALUES (%(clickup_user_id)s, %(name)s, %(email)s, %(role)s)
            ON CONFLICT (clickup_user_id) 
            DO UPDATE SET 
                name = EXCLUDED.name,
                email = EXCLUDED.email,
                role = EXCLUDED.role
            RETURNING id
        """,
            payload,
        )
        return cur.fetchone() is not None


# =============================================================================
# TASKS TABLE
# =============================================================================
def get_existing_task_ids() -> set[str]:
    """Get all non-deleted task IDs."""
    with get_cursor() as cur:
        cur.execute("SELECT clickup_task_id FROM tasks WHERE is_deleted = FALSE")
        return {row["clickup_task_id"] for row in cur.fetchall()}


def mark_tasks_deleted(task_ids: list[str], updated_at: str) -> None:
    """Mark tasks as deleted."""
    if not task_ids:
        return
    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE tasks 
            SET is_deleted = TRUE, updated_at = %s 
            WHERE clickup_task_id = ANY(%s)
        """,
            (updated_at, list(task_ids)),
        )


def bulk_upsert_tasks(payloads: list[dict]) -> int:
    """
    Bulk upsert tasks using PostgreSQL's INSERT ... ON CONFLICT.
    Returns count of upserted rows.
    """
    if not payloads:
        return 0

    # Insert each task individually to handle UUID array casting properly
    sql = """
        INSERT INTO tasks (
            clickup_task_id, title, description, type, status, status_type,
            priority, tags, summary, sprint_points, assignee_name, assignee_ids,
            employee_id, employee_ids, assigned_by, followers, space_id, space_name,
            folder_id, folder_name, list_id, list_name, date_created, date_updated,
            date_done, date_closed, start_date, due_date, time_estimate_minutes,
            start_time, end_time, tracked_minutes, archived, is_deleted, updated_at
        ) VALUES (
            %(clickup_task_id)s, %(title)s, %(description)s, %(type)s, %(status)s, %(status_type)s,
            %(priority)s, %(tags)s, %(summary)s, %(sprint_points)s, %(assignee_name)s, %(assignee_ids)s,
            %(employee_id)s, %(employee_ids)s::uuid[], %(assigned_by)s, %(followers)s, %(space_id)s, %(space_name)s,
            %(folder_id)s, %(folder_name)s, %(list_id)s, %(list_name)s, %(date_created)s, %(date_updated)s,
            %(date_done)s, %(date_closed)s, %(start_date)s, %(due_date)s, %(time_estimate_minutes)s,
            %(start_time)s, %(end_time)s, %(tracked_minutes)s, %(archived)s, %(is_deleted)s, %(updated_at)s
        )
        ON CONFLICT (clickup_task_id) 
        DO UPDATE SET
            title = EXCLUDED.title,
            description = EXCLUDED.description,
            type = EXCLUDED.type,
            status = EXCLUDED.status,
            status_type = EXCLUDED.status_type,
            priority = EXCLUDED.priority,
            tags = EXCLUDED.tags,
            summary = EXCLUDED.summary,
            sprint_points = EXCLUDED.sprint_points,
            assignee_name = EXCLUDED.assignee_name,
            assignee_ids = EXCLUDED.assignee_ids,
            employee_id = EXCLUDED.employee_id,
            employee_ids = EXCLUDED.employee_ids,
            assigned_by = EXCLUDED.assigned_by,
            followers = EXCLUDED.followers,
            space_id = EXCLUDED.space_id,
            space_name = EXCLUDED.space_name,
            folder_id = EXCLUDED.folder_id,
            folder_name = EXCLUDED.folder_name,
            list_id = EXCLUDED.list_id,
            list_name = EXCLUDED.list_name,
            date_created = EXCLUDED.date_created,
            date_updated = EXCLUDED.date_updated,
            date_done = EXCLUDED.date_done,
            date_closed = EXCLUDED.date_closed,
            start_date = EXCLUDED.start_date,
            due_date = EXCLUDED.due_date,
            time_estimate_minutes = EXCLUDED.time_estimate_minutes,
            start_time = EXCLUDED.start_time,
            end_time = EXCLUDED.end_time,
            tracked_minutes = EXCLUDED.tracked_minutes,
            archived = EXCLUDED.archived,
            is_deleted = EXCLUDED.is_deleted,
            updated_at = EXCLUDED.updated_at
    """

    with get_cursor() as cur:
        cur.executemany(sql, payloads)
        return len(payloads)


def get_tasks_by_employee_id(employee_id: str) -> list[dict]:
    """Fetch tasks where employee_id is in the employee_ids array."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT 
                clickup_task_id, title, description, status, status_type, type, archived,
                assigned_comment, assignee_name, assignee_ids, assigned_by, employee_ids,
                tags, priority, due_date, start_date, date_created, date_updated, date_done,
                date_closed, time_estimate_minutes, tracked_minutes, space_name, folder_name,
                list_name, followers, summary, sprint_points, in_progress_by, completed_by
            FROM tasks 
            WHERE %s::uuid = ANY(employee_ids)
        """,
            (employee_id,),
        )
        return [dict(row) for row in cur.fetchall()]


# =============================================================================
# TIME TRACKING (for time_sync.py if still needed)
# =============================================================================
def update_task_time(
    task_id: str,
    tracked_minutes: int,
    start_time: str | None,
    end_time: str | None,
    updated_at: str,
) -> None:
    """Update time tracking fields for a single task."""
    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE tasks 
            SET tracked_minutes = %s, start_time = %s, end_time = %s, updated_at = %s
            WHERE clickup_task_id = %s
        """,
            (tracked_minutes, start_time, end_time, updated_at, task_id),
        )
