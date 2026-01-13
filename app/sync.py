"""
Task Sync Logic - ClickUp to PostgreSQL
"""

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
    _get,
    BASE_URL,
)
from app.time_tracking import aggregate_time_entries

IST = ZoneInfo("Asia/Kolkata")

# Type mapping: custom_item_id -> type name
TYPE_MAP = {0: "task", 1: "milestone", 2: "form response", 3: "meeting note"}


def _ms_to_dt(ms):
    """Convert ClickUp milliseconds to datetime."""
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc) if ms else None


def _ms_to_date(ms):
    """Convert ClickUp milliseconds to IST date string."""
    return _ms_to_dt(ms).astimezone(IST).date().isoformat() if ms else None


def _to_iso(dt):
    return dt.isoformat() if dt else None


# -----------------------------------------------------------------------------
# Location Map
# -----------------------------------------------------------------------------
def get_location_map():
    """Build list_id -> location info for all spaces."""
    loc = {}
    for space in fetch_all_spaces():
        sid, sname = space["id"], space["name"]
        # Folder lists
        for folder in _get(f"{BASE_URL}/space/{sid}/folder").get("folders", []):
            for lst in folder.get("lists", []):
                loc[lst["id"]] = {
                    "space_id": sid,
                    "space_name": sname,
                    "folder_id": folder["id"],
                    "folder_name": folder["name"],
                    "list_id": lst["id"],
                    "list_name": lst["name"],
                }
        # Standalone lists
        for lst in _get(f"{BASE_URL}/space/{sid}/list").get("lists", []):
            loc[lst["id"]] = {
                "space_id": sid,
                "space_name": sname,
                "folder_id": None,
                "folder_name": "None",
                "list_id": lst["id"],
                "list_name": lst["name"],
            }
    return loc


# -----------------------------------------------------------------------------
# Main Sync
# -----------------------------------------------------------------------------
def sync_tasks_to_supabase(tasks, *, full_sync):
    if not tasks:
        return 0

    emp_map = get_employee_id_map()
    loc_map = get_location_map()
    now = datetime.now(timezone.utc).isoformat()

    # Deleted tasks detection (full sync only)
    if full_sync:
        deleted = get_existing_task_ids() - {t["id"] for t in tasks}
        if deleted:
            mark_tasks_deleted(list(deleted), now)

    # Batch fetch time entries & comments
    task_ids = [t["id"] for t in tasks]
    print(f"⏱️  Fetching time entries for {len(tasks)} tasks...")
    time_map = fetch_all_time_entries_batch(task_ids)
    print("✅ Time entries fetched")
    print(f"💬 Fetching assigned comments for {len(tasks)} tasks...")
    comment_map = fetch_assigned_comments_batch(task_ids)
    print("✅ Assigned comments fetched")

    # Build payloads
    payloads = []
    for t in tasks:
        tid = t["id"]
        status = t.get("status", {})
        loc = loc_map.get(t.get("list", {}).get("id"), {})
        assignees = t.get("assignees") or []

        # Task type from custom_item_id
        task_type = TYPE_MAP.get(
            t.get("custom_item_id"),
            "milestone" if t.get("is_milestone") else t.get("type") or "task",
        )

        # Assignee processing
        assignee_ids = [str(a["id"]) for a in assignees if a.get("id")]
        assignee_names = [a["username"] for a in assignees if a.get("username")]
        employee_ids = [
            emp_map[str(a["id"])] for a in assignees if str(a.get("id")) in emp_map
        ]

        # Time tracking
        agg = aggregate_time_entries(time_map.get(tid, []))

        # Sprint points (native or custom field)
        sprint_points = None
        if t.get("points"):
            try:
                sprint_points = int(float(t["points"]))
            except (ValueError, TypeError):
                pass
        else:
            for f in t.get("custom_fields", []):
                if (f.get("name") or "").lower() in (
                    "sprint points",
                    "points",
                    "story points",
                ) and f.get("value"):
                    try:
                        sprint_points = int(float(f["value"]))
                    except (ValueError, TypeError):
                        pass
                    break

        # Summary from custom field
        summary = next(
            (
                str(f["value"])
                for f in t.get("custom_fields", [])
                if (f.get("name") or "").lower() == "summary" and f.get("value")
            ),
            None,
        )

        # Tags & followers
        tags = (
            ", ".join(x["name"] for x in (t.get("tags") or []) if x.get("name")) or None
        )
        followers = (
            ", ".join(
                w["username"] for w in (t.get("watchers") or []) if w.get("username")
            )
            or None
        )

        payloads.append(
            {
                "clickup_task_id": tid,
                "title": t.get("name"),
                "description": t.get("text_content"),
                "type": task_type,
                "status": status.get("status", ""),
                "status_type": status.get("type", ""),
                "priority": (t.get("priority") or {}).get("priority"),
                "tags": tags,
                "summary": summary,
                "sprint_points": sprint_points,
                "assigned_comment": comment_map.get(tid),
                "assignee_name": ", ".join(assignee_names) or None,
                "assignee_ids": ", ".join(assignee_ids) or None,
                "employee_id": employee_ids[0] if employee_ids else None,
                "employee_ids": employee_ids or None,
                "assigned_by": (t.get("creator") or {}).get("username"),
                "followers": followers,
                "space_id": loc.get("space_id"),
                "space_name": loc.get("space_name"),
                "folder_id": loc.get("folder_id"),
                "folder_name": loc.get("folder_name"),
                "list_id": loc.get("list_id"),
                "list_name": loc.get("list_name"),
                "date_created": _to_iso(_ms_to_dt(t.get("date_created"))),
                "date_updated": _to_iso(_ms_to_dt(t.get("date_updated"))),
                "date_done": _to_iso(_ms_to_dt(t.get("date_done"))),
                "date_closed": _to_iso(_ms_to_dt(t.get("date_closed")))
                if status.get("type") == "closed"
                else None,
                "start_date": _ms_to_date(t.get("start_date")),
                "due_date": _ms_to_date(t.get("due_date")),
                "time_estimate_minutes": int(t["time_estimate"]) // 60000
                if t.get("time_estimate")
                else None,
                "start_time": _to_iso(agg["start_time"]),
                "end_time": _to_iso(agg["end_time"]),
                "tracked_minutes": agg["tracked_minutes"],
                "archived": t.get("archived", False),
                "is_deleted": False,
                "updated_at": now,
            }
        )

    print(f"💾 Upserting {len(payloads)} tasks to database...")
    bulk_upsert_tasks(payloads)
    print("✅ Sync complete")
    return len(payloads)
