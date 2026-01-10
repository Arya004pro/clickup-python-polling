from datetime import datetime, timezone
from app.supabase_db import supabase
from app.clickup import fetch_time_entries_for_task
from app.time_tracking import aggregate_time_entries
from zoneinfo import ZoneInfo
from app.config import CLICKUP_SPACE_ID


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


IST = ZoneInfo("Asia/Kolkata")


def clickup_ms_to_local_date(ms: int | None):
    if not ms:
        return None

    return (
        datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        .astimezone(IST)
        .date()
        .isoformat()
    )


def get_list_location_map(space_id: str) -> dict:
    """
    Correctly maps list_id -> space / folder / list info
    """
    from app.clickup import _get, BASE_URL

    location_map = {}

    # ---------- Space ----------
    space_resp = _get(f"{BASE_URL}/space/{space_id}")
    space_name = space_resp.get("name")

    # ---------- Folder lists ----------
    folders_resp = _get(f"{BASE_URL}/space/{space_id}/folder")
    for folder in folders_resp.get("folders", []):
        folder_id = folder["id"]
        folder_name = folder["name"]

        for lst in folder.get("lists", []):
            location_map[lst["id"]] = {
                "space_id": space_id,
                "space_name": space_name,
                "folder_id": folder_id,
                "folder_name": folder_name,
                "list_id": lst["id"],
                "list_name": lst["name"],
            }

    # ---------- Standalone lists (NO folder) ----------
    lists_resp = _get(f"{BASE_URL}/space/{space_id}/list")
    for lst in lists_resp.get("lists", []):
        location_map[lst["id"]] = {
            "space_id": space_id,
            "space_name": space_name,
            "folder_id": None,
            "folder_name": "hidden",
            "list_id": lst["id"],
            "list_name": lst["name"],
        }

    return location_map


list_location_map = get_list_location_map(CLICKUP_SPACE_ID)


# -------------------------------------------------
# Main Sync (STABLE – PRIORITY VERSION)
# -------------------------------------------------
def sync_tasks_to_supabase(tasks: list, *, full_sync: bool) -> int:
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
        # Task Type
        # ----------------------------
        if task.get("milestone"):
            task_type = "milestone"
        elif task.get("form_id"):
            task_type = "form_response"
        else:
            task_type = "task"

        # ----------------------------
        # Dates from ClickUp (ms)
        # ----------------------------
        date_created = None
        date_updated = None
        date_closed = None
        date_done = None

        if task.get("date_created"):
            date_created = datetime.fromtimestamp(
                int(task["date_created"]) / 1000,
                tz=timezone.utc,
            )

        if task.get("date_updated"):
            date_updated = datetime.fromtimestamp(
                int(task["date_updated"]) / 1000,
                tz=timezone.utc,
            )

        # Closed = system closed only
        if status_type == "closed" and task.get("date_closed"):
            date_closed = datetime.fromtimestamp(
                int(task["date_closed"]) / 1000,
                tz=timezone.utc,
            )

        # Done = custom "Done" status
        # Done = ClickUp-provided date_done
        if task.get("date_done"):
            date_done = datetime.fromtimestamp(
                int(task["date_done"]) / 1000,
                tz=timezone.utc,
            )

        # ----------------------------
        # Location / List (FIXED)
        # ----------------------------
        space_id = None
        space_name = None
        folder_id = None
        folder_name = None
        list_id = None
        list_name = None

        task_list = task.get("list")
        if task_list:
            list_id = task_list.get("id")

            location = list_location_map.get(list_id)
            if location:
                list_name = location["list_name"]
                folder_id = location["folder_id"]
                folder_name = location["folder_name"]
                space_id = location["space_id"]
                space_name = location["space_name"]

        # ----------------------------
        # Priority
        # ----------------------------
        priority_obj = task.get("priority")
        priority = priority_obj.get("priority") if priority_obj else None

        # ----------------------------
        # Time Estimate (ms → minutes)
        # ----------------------------
        time_estimate_minutes = None
        time_estimate_ms = task.get("time_estimate")

        if time_estimate_ms:
            time_estimate_minutes = int(time_estimate_ms) // 60000

        # ----------------------------
        # Assignee
        # ----------------------------
        employee_id = None
        assignee_name = None

        assignees = task.get("assignees") or []
        user = None

        if assignees:
            user = assignees[0]
        elif task.get("assignee"):
            user = task["assignee"]
        elif task.get("creator"):
            user = task["creator"]

        if user:
            clickup_user_id = str(user.get("id"))
            employee_id = employee_map.get(clickup_user_id)
            assignee_name = user.get("username")

        # ----------------------------
        # Assigned by
        # ----------------------------
        assigned_by = None
        creator = task.get("creator")
        if creator:
            assigned_by = creator.get("username")

        # ----------------------------
        # Followers
        # ----------------------------
        followers = None
        watchers = task.get("watchers") or []

        if watchers:
            follower_names = [
                str(w.get("username"))
                for w in watchers
                if isinstance(w.get("username"), str)
            ]
            followers = ", ".join(follower_names) if follower_names else None

        # ----------------------------
        # Start Date (DATE ONLY)
        # ----------------------------
        start_date = None
        start_date_ms = task.get("start_date")

        if start_date_ms:
            start_date = clickup_ms_to_local_date(task.get("start_date"))

        # ----------------------------
        # Due Date (DATE ONLY)
        # ----------------------------
        due_date = None
        due_date_ms = task.get("due_date")
        if due_date_ms:
            due_date = clickup_ms_to_local_date(task.get("due_date"))

        # ----------------------------
        # Time tracking
        # ----------------------------
        time_entries = fetch_time_entries_for_task(clickup_task_id)
        aggregated = aggregate_time_entries(time_entries)

        # ----------------------------
        # Responsibility (STRICT)
        # ----------------------------
        in_progress_by = None
        completed_by = None

        if time_entries:
            first_user = time_entries[0].get("user", {})
            in_progress_by = employee_map.get(str(first_user.get("id")))

            if status_type == "closed":
                last_user = time_entries[-1].get("user", {})
                completed_by = employee_map.get(str(last_user.get("id")))

        # ----------------------------
        # Tags
        # ----------------------------
        tags_list = task.get("tags") or []
        tags_names = [t.get("name") for t in tags_list if t.get("name")]
        tags_str = ", ".join(tags_names) if tags_names else None

        # ----------------------------
        # Summary
        # ----------------------------
        summary = None

        for field in task.get("custom_fields", []) or []:
            if field.get("name") == "Summary":
                value = field.get("value")

                # 🔒 force safe string
                if isinstance(value, (dict, list)):
                    summary = str(value)
                else:
                    summary = value

                break

        # ----------------------------
        # Payload
        # ----------------------------
        payload = {
            "clickup_task_id": clickup_task_id,
            "title": task.get("name", ""),
            "description": task.get("text_content", ""),
            "tags": tags_str,
            "status": status_text,
            "task_type": task_type,
            "status_type": status_type,
            "space_id": space_id,
            "space_name": space_name,
            "folder_id": folder_id,
            "folder_name": folder_name,
            "summary": summary,
            "list_id": list_id,
            "list_name": list_name,
            "date_created": _to_iso(date_created),
            "start_date": start_date,
            "date_updated": _to_iso(date_updated),
            "date_done": _to_iso(date_done),
            "date_closed": _to_iso(date_closed),
            "priority": priority,
            "time_estimate_minutes": time_estimate_minutes,
            "employee_id": employee_id,
            "assignee_name": assignee_name,
            "assigned_by": assigned_by,
            "followers": followers,
            "in_progress_by": in_progress_by,
            "completed_by": completed_by,
            "due_date": due_date,
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
