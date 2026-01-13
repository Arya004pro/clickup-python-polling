"""
PostgreSQL Database Layer (psycopg2)
"""

import psycopg2
from psycopg2.extras import RealDictCursor, execute_batch
from contextlib import contextmanager
from app.config import DATABASE_URL


@contextmanager
def db():
    """Database connection with auto-commit."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn.cursor(cursor_factory=RealDictCursor)
        conn.commit()
    except:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- Employees ---
def get_all_employees():
    with db() as cur:
        cur.execute("SELECT id, name, email, role, clickup_user_id FROM employees")
        return [dict(r) for r in cur.fetchall()]


def get_employee_id_map():
    with db() as cur:
        cur.execute(
            "SELECT id, clickup_user_id FROM employees WHERE clickup_user_id IS NOT NULL"
        )
        return {str(r["clickup_user_id"]): str(r["id"]) for r in cur.fetchall()}


def upsert_employee(data):
    with db() as cur:
        cur.execute(
            """
            INSERT INTO employees (clickup_user_id, name, email, role)
            VALUES (%(clickup_user_id)s, %(name)s, %(email)s, %(role)s)
            ON CONFLICT (clickup_user_id) DO UPDATE SET name=EXCLUDED.name, email=EXCLUDED.email, role=EXCLUDED.role
        """,
            data,
        )
    return True


# --- Tasks ---
TASK_COLS = [
    "clickup_task_id",
    "title",
    "description",
    "type",
    "status",
    "status_type",
    "priority",
    "tags",
    "summary",
    "sprint_points",
    "assigned_comment",
    "assignee_name",
    "assignee_ids",
    "employee_id",
    "employee_ids",
    "assigned_by",
    "followers",
    "space_id",
    "space_name",
    "folder_id",
    "folder_name",
    "list_id",
    "list_name",
    "date_created",
    "date_updated",
    "date_done",
    "date_closed",
    "start_date",
    "due_date",
    "time_estimate_minutes",
    "start_times",
    "end_times",
    "tracked_minutes",
    "archived",
    "is_deleted",
    "updated_at",
]


def _get_placeholder(c):
    if c == "employee_ids":
        return "%(employee_ids)s::uuid[]"
    if c in ("start_times", "end_times"):
        return f"%({c})s::text[]"
    return f"%({c})s"


def get_existing_task_ids():
    with db() as cur:
        cur.execute("SELECT clickup_task_id FROM tasks WHERE is_deleted = FALSE")
        return {r["clickup_task_id"] for r in cur.fetchall()}


def get_all_task_ids():
    with db() as cur:
        cur.execute("SELECT clickup_task_id FROM tasks WHERE is_deleted = FALSE")
        return [r["clickup_task_id"] for r in cur.fetchall()]


def mark_tasks_deleted(ids, updated_at):
    if not ids:
        return
    with db() as cur:
        cur.execute(
            "UPDATE tasks SET is_deleted=TRUE, updated_at=%s WHERE clickup_task_id=ANY(%s)",
            (updated_at, list(ids)),
        )


def bulk_upsert_tasks(payloads):
    if not payloads:
        return 0
    placeholders = ", ".join(_get_placeholder(c) for c in TASK_COLS)
    updates = ", ".join(
        f"{c}=EXCLUDED.{c}" for c in TASK_COLS if c != "clickup_task_id"
    )
    sql = f"INSERT INTO tasks ({', '.join(TASK_COLS)}) VALUES ({placeholders}) ON CONFLICT (clickup_task_id) DO UPDATE SET {updates}"
    with db() as cur:
        execute_batch(cur, sql, payloads)
    return len(payloads)


def bulk_update_comments(comment_map, updated_at):
    if not comment_map:
        return 0
    with db() as cur:
        for task_id, comment in comment_map.items():
            cur.execute(
                "UPDATE tasks SET assigned_comment=%s, updated_at=%s WHERE clickup_task_id=%s",
                (comment, updated_at, task_id),
            )
    return len(comment_map)


# --- Task Queries ---
def get_tasks_by_employee_id(employee_id):
    with db() as cur:
        cur.execute(
            "SELECT * FROM tasks WHERE %s::uuid = ANY(employee_ids) AND is_deleted = FALSE",
            (employee_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def get_all_tasks(limit=100, offset=0):
    with db() as cur:
        cur.execute(
            "SELECT * FROM tasks WHERE is_deleted = FALSE ORDER BY date_updated DESC NULLS LAST LIMIT %s OFFSET %s",
            (limit, offset),
        )
        return [dict(r) for r in cur.fetchall()]


def get_task_by_id(task_id):
    with db() as cur:
        cur.execute("SELECT * FROM tasks WHERE clickup_task_id = %s", (task_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_tasks_with_time():
    with db() as cur:
        cur.execute("""SELECT clickup_task_id, title, status, assignee_name, start_times, end_times, tracked_minutes, assigned_comment
                       FROM tasks WHERE tracked_minutes > 0 AND is_deleted = FALSE ORDER BY tracked_minutes DESC""")
        return [dict(r) for r in cur.fetchall()]


def get_tasks_with_comments():
    with db() as cur:
        cur.execute("""SELECT clickup_task_id, title, status, assignee_name, assigned_comment
                       FROM tasks WHERE assigned_comment IS NOT NULL AND is_deleted = FALSE ORDER BY date_updated DESC""")
        return [dict(r) for r in cur.fetchall()]
