"""
Daily Sync - Fetch tasks updated on the current date and populate daily_syncs table
"""

from app.supabase_db import db


def sync_daily_updated_tasks():
    """
    Sync tasks that were updated on the current date to daily_syncs table.
    Only includes tasks with date_updated = today.
    """
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
    from datetime import datetime

    today = datetime.now(IST).date()
    print(f"🔄 Fetching tasks updated on {today}")

    # Define the specific columns to sync (only the 26 user-specified columns)
    cols = [
        "clickup_task_id",
        "title",  # Task name
        "description",
        "status",
        "tags",
        "priority",
        "start_times",  # start time
        "end_times",  # end time
        "tracked_minutes",  # tracked min
        "status_type",
        "type",
        "assignee_name",
        "assigned_by",
        "due_date",
        "assigned_comment",
        "date_created",
        "date_closed",
        "time_estimate_minutes",  # time estimate
        "start_date",
        "space_name",
        "folder_name",
        "list_name",
        "followers",
        "summary",
        "sprint_points",
        "dependencies",
        "last_status_change",
        "clickup_user_id",
    ]
    select_cols = ", ".join(cols[:-1]) + ", employees.clickup_user_id"
    with db() as cur:
        cur.execute(
            f"SELECT {select_cols} FROM tasks LEFT JOIN employees ON tasks.employee_id = employees.id WHERE last_status_change::date = %s AND is_deleted = FALSE",
            (today,),
        )
        tasks = [dict(row) for row in cur.fetchall()]
    # Convert assignee_name in tasks to PostgreSQL array for robust filtering
    for t in tasks:
        assignees = t.get("assignee_name")
        # Always output a list (or None) for assignee_name, never a string
        if not assignees or assignees == "{}":
            t["assignee_name"] = None
        elif isinstance(assignees, list):
            names = [str(a).strip() for a in assignees if str(a).strip()]
            t["assignee_name"] = names if names else None
        elif isinstance(assignees, str):
            s = assignees.strip()
            if s.startswith("{") and s.endswith("}"):
                s = s[1:-1]
            s = s.replace('"', "")
            names = [n.strip() for n in s.split(",") if n.strip()]
            t["assignee_name"] = names if names else None
        else:
            t["assignee_name"] = (
                [str(assignees).strip()] if str(assignees).strip() else None
            )

    # Build SQL for insert
    def get_placeholder(c):
        if c in ("start_times", "end_times", "assignee_name"):
            return f"%({c})s::text[]"
        return f"%({c})s"

    placeholders = ", ".join(get_placeholder(c) for c in cols)
    sql = f"INSERT INTO daily_syncs ({', '.join(cols)}) VALUES ({placeholders})"

    # Prepare payloads for only tasks updated today (already filtered)
    payloads = []
    for task in tasks:
        payload = {col: task.get(col) for col in cols}
        # Already converted above, just copy
        payloads.append(payload)

    # Clear existing data and insert
    with db() as cur:
        cur.execute("DELETE FROM daily_syncs")
        from psycopg2.extras import execute_batch

        execute_batch(cur, sql, payloads)

    print(f"✅ Inserted {len(payloads)} tasks updated today into daily_syncs")

    return len(payloads)
