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
    ]

    # Get tasks updated today with only the specified columns
    select_cols = ", ".join(cols)
    with db() as cur:
        cur.execute(
            f"SELECT {select_cols} FROM tasks WHERE date_updated = %s AND is_deleted = FALSE",
            (today,),
        )
        tasks = [dict(row) for row in cur.fetchall()]

    print(f"📊 Found {len(tasks)} tasks updated today")

    if not tasks:
        print("ℹ️  No tasks were updated today")
        return 0

    # Build SQL for insert
    def get_placeholder(c):
        if c in ("start_times", "end_times"):
            return f"%({c})s::text[]"
        return f"%({c})s"

    placeholders = ", ".join(get_placeholder(c) for c in cols)
    sql = f"INSERT INTO daily_syncs ({', '.join(cols)}) VALUES ({placeholders})"

    # Prepare payloads
    payloads = []
    for task in tasks:
        payload = {col: task.get(col) for col in cols}
        # Convert date_created and date_closed to IST full timestamp if present
        for ts_col in ["date_created", "date_closed"]:
            if payload.get(ts_col):
                try:
                    # Parse as datetime and convert to IST
                    dt = datetime.fromisoformat(str(payload[ts_col]))
                    payload[ts_col] = dt.astimezone(IST).isoformat()
                except Exception:
                    pass
        payloads.append(payload)

    # Clear existing data and insert
    with db() as cur:
        cur.execute("DELETE FROM daily_syncs")
        from psycopg2.extras import execute_batch

        execute_batch(cur, sql, payloads)

    print(f"✅ Inserted {len(payloads)} tasks updated today into daily_syncs")

    return len(payloads)
