from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.supabase_db import (
    get_employee_id_map,
    get_existing_task_ids,
    mark_tasks_deleted,
    bulk_upsert_tasks,
)
from app.clickup import (
    fetch_all_time_entries_batch,
    fetch_all_spaces,
    fetch_assigned_comments_batch,
)
from app.time_tracking import aggregate_time_entries


# -------------------------------------------------
# Helpers
# -------------------------------------------------
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


# -------------------------------------------------
# Location Map (Dynamic - All Spaces)
# -------------------------------------------------
def get_list_location_map_for_space(space_id: str) -> dict:
    """
    Maps list_id -> space / folder / list info for a single space.
    """
    from app.clickup import _get, BASE_URL

    location_map = {}

    # Space
    space_resp = _get(f"{BASE_URL}/space/{space_id}")
    space_name = space_resp.get("name")

    # Folder lists
    folders_resp = _get(f"{BASE_URL}/space/{space_id}/folder")
    for folder in folders_resp.get("folders", []):
        for lst in folder.get("lists", []):
            location_map[lst["id"]] = {
                "space_id": space_id,
                "space_name": space_name,
                "folder_id": folder["id"],
                "folder_name": folder["name"],
                "list_id": lst["id"],
                "list_name": lst["name"],
            }

    # Standalone lists
    lists_resp = _get(f"{BASE_URL}/space/{space_id}/list")
    for lst in lists_resp.get("lists", []):
        location_map[lst["id"]] = {
            "space_id": space_id,
            "space_name": space_name,
            "folder_id": None,
            "folder_name": "None",
            "list_id": lst["id"],
            "list_name": lst["name"],
        }

    return location_map


def get_all_list_location_map() -> dict:
    """
    Build location map for ALL spaces in the team.
    Called fresh each sync to pick up new spaces.
    """
    location_map = {}
    spaces = fetch_all_spaces()

    for space in spaces:
        space_map = get_list_location_map_for_space(space["id"])
        location_map.update(space_map)

    return location_map


# -------------------------------------------------
# Main Sync
# -------------------------------------------------
def sync_tasks_to_supabase(tasks: list, *, full_sync: bool) -> int:
    if not tasks:
        return 0

    employee_map = get_employee_id_map()
    now_utc = datetime.now(timezone.utc).isoformat()

    # =====================================================
    # 0. Build location map for ALL spaces (fresh each sync)
    # =====================================================
    list_location_map = get_all_list_location_map()

    # =====================================================
    # 1. Detect deleted tasks (FULL sync only)
    # =====================================================
    if full_sync:
        incoming_ids = {task["id"] for task in tasks}
        existing_ids = get_existing_task_ids()
        deleted_ids = existing_ids - incoming_ids

        if deleted_ids:
            mark_tasks_deleted(list(deleted_ids), now_utc)

    # =====================================================
    # 2. Batch fetch time entries (concurrent)
    # =====================================================
    print(f"⏱️  Fetching time entries for {len(tasks)} tasks...")
    task_ids = [task["id"] for task in tasks]
    time_entries_map = fetch_all_time_entries_batch(task_ids)
    print("✅ Time entries fetched")

    # =====================================================
    # 2b. Batch fetch assigned comments (concurrent)
    # =====================================================
    print(f"💬 Fetching assigned comments for {len(tasks)} tasks...")
    assigned_comment_map = fetch_assigned_comments_batch(task_ids)
    print("✅ Assigned comments fetched")

    # =====================================================
    # 3. Build payloads
    # =====================================================
    payloads = []
    for task in tasks:
        clickup_task_id = task["id"]

        # ----------------------------
        # Status
        # ----------------------------
        status_obj = task.get("status", {})
        status_text = status_obj.get("status", "")
        status_type = status_obj.get("type", "")

        # ----------------------------
        # Task Type (CORRECT)
        # ----------------------------
        # ClickUp uses custom_item_id to distinguish item types:
        #   0 or None = task
        #   1 = milestone
        #   2 = form response
        #   3 = meeting note
        # Fallback to explicit type field or milestone flag if present.
        CUSTOM_ITEM_TYPE_MAP = {
            0: "task",
            1: "milestone",
            2: "form response",
            3: "meeting note",
        }

        custom_item_id = task.get("custom_item_id")
        raw_type = task.get("type") or task.get("custom_type") or task.get("task_type")

        if custom_item_id is not None and custom_item_id in CUSTOM_ITEM_TYPE_MAP:
            task_type = CUSTOM_ITEM_TYPE_MAP[custom_item_id]
        elif task.get("is_milestone"):
            task_type = "milestone"
        elif raw_type:
            task_type = raw_type
        else:
            task_type = "task"

        # ----------------------------
        # Dates
        # ----------------------------
        date_created = (
            datetime.fromtimestamp(int(task["date_created"]) / 1000, tz=timezone.utc)
            if task.get("date_created")
            else None
        )

        date_updated = (
            datetime.fromtimestamp(int(task["date_updated"]) / 1000, tz=timezone.utc)
            if task.get("date_updated")
            else None
        )

        date_done = (
            datetime.fromtimestamp(int(task["date_done"]) / 1000, tz=timezone.utc)
            if task.get("date_done")
            else None
        )

        date_closed = (
            datetime.fromtimestamp(int(task["date_closed"]) / 1000, tz=timezone.utc)
            if status_type == "closed" and task.get("date_closed")
            else None
        )

        # ----------------------------
        # Location
        # ----------------------------
        location = list_location_map.get(task.get("list", {}).get("id"), {})

        # ----------------------------
        # Priority
        # ----------------------------
        priority = (
            task.get("priority", {}).get("priority") if task.get("priority") else None
        )

        # ----------------------------
        # Time Estimate
        # ----------------------------
        time_estimate_minutes = (
            int(task["time_estimate"]) // 60000 if task.get("time_estimate") else None
        )

        # ----------------------------
        # Assignees (MULTI)
        # ----------------------------
        assignees = task.get("assignees") or []

        assignee_ids = [str(a.get("id")) for a in assignees if a.get("id") is not None]
        assignee_names = [
            a.get("username") for a in assignees if isinstance(a.get("username"), str)
        ]

        assignee_name = ", ".join(assignee_names) if assignee_names else None
        assignee_ids_str = ", ".join(assignee_ids) if assignee_ids else None

        employee_ids = [
            employee_map[str(a.get("id"))]
            for a in assignees
            if a.get("id") is not None and str(a.get("id")) in employee_map
        ]
        # Preserve existing single-employee linkage for backwards compatibility
        employee_id = employee_ids[0] if employee_ids else None

        # ----------------------------
        # Assigned by
        # ----------------------------
        assigned_by = task.get("creator", {}).get("username")

        # ----------------------------
        # Followers
        # ----------------------------
        followers = None
        watchers = task.get("watchers") or []
        follower_names = [
            w.get("username") for w in watchers if isinstance(w.get("username"), str)
        ]
        if follower_names:
            followers = ", ".join(follower_names)

        # ----------------------------
        # Dates (start / due)
        # ----------------------------
        start_date = clickup_ms_to_local_date(task.get("start_date"))
        due_date = clickup_ms_to_local_date(task.get("due_date"))

        # ----------------------------
        # Time tracking (from batch)
        # ----------------------------
        time_entries = time_entries_map.get(clickup_task_id, [])
        aggregated = aggregate_time_entries(time_entries)

        # ----------------------------
        # Tags
        # ----------------------------
        tags = (
            ", ".join(t.get("name") for t in (task.get("tags") or []) if t.get("name"))
            or None
        )

        # ----------------------------
        # Custom Fields (Summary) + Sprint Points
        # ----------------------------
        summary = None
        for field in task.get("custom_fields", []):
            field_name = (field.get("name") or "").lower()
            if field_name == "summary":
                summary = str(field.get("value")) if field.get("value") else None

        # Sprint points can be in task.points (native) or custom field
        sprint_points = None
        if task.get("points") is not None:
            try:
                sprint_points = int(float(task["points"]))
            except (ValueError, TypeError):
                sprint_points = None
        else:
            # Fallback: check custom fields
            for field in task.get("custom_fields", []):
                field_name = (field.get("name") or "").lower()
                if field_name in (
                    "sprint points",
                    "sprint_points",
                    "points",
                    "story points",
                ):
                    val = field.get("value")
                    if val is not None:
                        try:
                            sprint_points = int(float(val))
                        except (ValueError, TypeError):
                            pass
                    break

        # ----------------------------
        # Assigned Comment
        # ----------------------------
        assigned_comment = assigned_comment_map.get(clickup_task_id)

        # ----------------------------
        # Payload
        # ----------------------------
        payload = {
            "clickup_task_id": clickup_task_id,
            "title": task.get("name"),
            "description": task.get("text_content"),
            "type": task_type,
            "status": status_text,
            "status_type": status_type,
            "priority": priority,
            "tags": tags,
            "summary": summary,
            "sprint_points": sprint_points,
            "assigned_comment": assigned_comment,
            "assignee_name": assignee_name,
            "assignee_ids": assignee_ids_str,
            "employee_id": employee_id,
            "employee_ids": employee_ids or None,
            "assigned_by": assigned_by,
            "followers": followers,
            "space_id": location.get("space_id"),
            "space_name": location.get("space_name"),
            "folder_id": location.get("folder_id"),
            "folder_name": location.get("folder_name"),
            "list_id": location.get("list_id"),
            "list_name": location.get("list_name"),
            "date_created": _to_iso(date_created),
            "date_updated": _to_iso(date_updated),
            "date_done": _to_iso(date_done),
            "date_closed": _to_iso(date_closed),
            "start_date": start_date,
            "due_date": due_date,
            "time_estimate_minutes": time_estimate_minutes,
            "start_time": _to_iso(aggregated["start_time"]),
            "end_time": _to_iso(aggregated["end_time"]),
            "tracked_minutes": aggregated["tracked_minutes"],
            "archived": task.get("archived", False),
            "is_deleted": False,
            "updated_at": now_utc,
        }
        payloads.append(payload)

    # =====================================================
    # 4. Bulk upsert to PostgreSQL
    # =====================================================
    print(f"💾 Upserting {len(payloads)} tasks to database...")
    bulk_upsert_tasks(payloads)
    print("✅ Sync complete")

    return len(payloads)
