from datetime import datetime, timezone

from app.supabase_db import supabase
from app.clickup import fetch_time_entries_for_task
from app.time_tracking import aggregate_time_entries


def _to_iso(dt):
    """Convert datetime → ISO string for Supabase"""
    return dt.isoformat() if dt else None


def sync_tasks_to_supabase(tasks: list) -> int:
    """
    Sync ClickUp tasks + interval-based time tracking into Supabase.
    Safe for polling (idempotent).
    """
    synced_count = 0

    for task in tasks:
        clickup_task_id = task["id"]

        # Fetch time entries for this task
        time_entries = fetch_time_entries_for_task(clickup_task_id)

        aggregated = aggregate_time_entries(time_entries)

        data = {
            "clickup_task_id": clickup_task_id,
            "title": task.get("name", ""),
            "description": task.get("text_content", ""),
            "status": task.get("status", {}).get("status", ""),
            "start_time": _to_iso(aggregated["start_time"]),
            "end_time": _to_iso(aggregated["end_time"]),
            "tracked_minutes": aggregated["tracked_minutes"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        response = (
            supabase.table("tasks")
            .upsert(data, on_conflict="clickup_task_id")
            .execute()
        )

        if response.data:
            synced_count += 1
        else:
            print("❌ Supabase error:", response.error)

    return synced_count
