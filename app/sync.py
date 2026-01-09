from datetime import datetime, timezone
from app.supabase_db import supabase
from app.clickup import fetch_time_entries_for_task
from app.time_tracking import aggregate_time_entries


def _to_iso(dt):
    return dt.isoformat() if dt else None


def sync_tasks_to_supabase(tasks: list, *, full_sync: bool) -> int:
    """
    Sync ClickUp tasks + time tracking into Supabase.

    Rules:
    - Full sync → handle deletions
    - Incremental sync → NEVER mark deletions
    """
    if not tasks:
        return 0

    synced_count = 0
    now_utc = datetime.now(timezone.utc).isoformat()

    # =====================================================
    # 1. Detect deleted tasks (ONLY on FULL sync)
    # =====================================================
    if full_sync:
        incoming_ids = {task["id"] for task in tasks}

        existing_resp = (
            supabase.table("tasks")
            .select("clickup_task_id")
            .eq("is_deleted", False)
            .execute()
        )

        existing_ids = {row["clickup_task_id"] for row in (existing_resp.data or [])}

        deleted_ids = existing_ids - incoming_ids

        for task_id in deleted_ids:
            supabase.table("tasks").update(
                {
                    "is_deleted": True,
                    "updated_at": now_utc,
                }
            ).eq("clickup_task_id", task_id).execute()

    # =====================================================
    # 2. Upsert active / updated tasks
    # =====================================================
    for task in tasks:
        clickup_task_id = task["id"]

        status_obj = task.get("status", {})
        status_text = status_obj.get("status", "")
        status_type = status_obj.get("type", "")

        time_entries = fetch_time_entries_for_task(clickup_task_id)
        aggregated = aggregate_time_entries(time_entries)

        payload = {
            "clickup_task_id": clickup_task_id,
            "title": task.get("name", ""),
            "description": task.get("text_content", ""),
            "status": status_text,
            "status_type": status_type,
            "start_time": _to_iso(aggregated["start_time"]),
            "end_time": _to_iso(aggregated["end_time"]),
            "tracked_minutes": aggregated["tracked_minutes"],
            "is_deleted": False,
            "updated_at": now_utc,
        }

        supabase.table("tasks").upsert(payload, on_conflict="clickup_task_id").execute()

        synced_count += 1

    return synced_count
