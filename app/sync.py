from datetime import datetime, timezone
from app.supabase_db import supabase
from app.clickup import fetch_time_entries_for_task
from app.time_tracking import aggregate_time_entries


# -------------------------------------------------
# Helpers
# -------------------------------------------------
def get_employee_id_map() -> dict[str, str]:
    """
    Map clickup_user_id (str) -> employees.id (UUID)
    """
    resp = supabase.table("employees").select("id, clickup_user_id").execute()

    return {
        str(row["clickup_user_id"]): row["id"]
        for row in (resp.data or [])
        if row.get("clickup_user_id")
    }


def _to_iso(dt):
    return dt.isoformat() if dt else None


# -------------------------------------------------
# Main Sync
# -------------------------------------------------
def sync_tasks_to_supabase(tasks: list, *, full_sync: bool) -> int:
    """
    Sync ClickUp tasks + interval-based time tracking into Supabase.

    Rules:
    - Full sync → handle deletions
    - Incremental sync → NEVER mark deletions
    - Responsibility is derived from ASSIGNEE, not time entries
    """

    if not tasks:
        return 0

    employee_map = get_employee_id_map()
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

        # ----------------------------
        # Status
        # ----------------------------
        status_obj = task.get("status", {})
        status_text = status_obj.get("status", "")
        status_type = status_obj.get("type", "")  # open / custom / closed

        # ----------------------------
        # Assignee resolution (ROBUST & CORRECT)
        # ----------------------------
        employee_id = None
        assignee_name = None

        assignees = task.get("assignees") or []

        user = None
        if assignees:
            user = assignees[0]  # primary assignee
        elif task.get("assignee"):
            user = task["assignee"]
        elif task.get("creator"):
            user = task["creator"]

        if user:
            clickup_user_id = str(user.get("id"))
            employee_id = employee_map.get(clickup_user_id)
            assignee_name = user.get("username")

        # ----------------------------
        # Assigned by (task creator)
        # ----------------------------
        assigned_by = None
        creator = task.get("creator")

        if creator:
            assigned_by = creator.get("username")

        # ----------------------------
        # Responsibility tracking (FIXED)
        # ----------------------------
        in_progress_by = None
        completed_by = None

        if status_type in ("open", "custom"):
            in_progress_by = employee_id
        elif status_type == "closed":
            completed_by = employee_id

        # ----------------------------
        # Time tracking (for metrics ONLY)
        # ----------------------------
        time_entries = fetch_time_entries_for_task(clickup_task_id)
        aggregated = aggregate_time_entries(time_entries)

        # ----------------------------
        # Payload
        # ----------------------------
        payload = {
            "clickup_task_id": clickup_task_id,
            "title": task.get("name", ""),
            "description": task.get("text_content", ""),
            "status": status_text,
            "status_type": status_type,
            # Ownership & accountability
            "employee_id": employee_id,  # assignee
            "assignee_name": assignee_name,
            "assigned_by": assigned_by,
            "in_progress_by": in_progress_by,
            "completed_by": completed_by,
            # Time
            "start_time": _to_iso(aggregated["start_time"]),
            "end_time": _to_iso(aggregated["end_time"]),
            "tracked_minutes": aggregated["tracked_minutes"],
            "is_deleted": False,
            "updated_at": now_utc,
        }

        supabase.table("tasks").upsert(
            payload,
            on_conflict="clickup_task_id",
        ).execute()

        synced_count += 1

    return synced_count
