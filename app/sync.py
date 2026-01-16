"""
Task Sync - ClickUp to PostgreSQL
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from app.supabase_db import (
    get_employee_id_map,
    get_existing_task_ids,
    mark_tasks_deleted,
    bulk_upsert_tasks,
    get_all_task_ids,
    bulk_update_comments,
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
TYPE_MAP = {0: "task", 1: "milestone", 2: "meeting notes", 3: "form response", 4: "meeting note"}


def _ms_to_dt(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc) if ms else None


def _ms_to_date(ms):
    return _ms_to_dt(ms).astimezone(IST).date().isoformat() if ms else None


def _to_iso(dt):
    return dt.isoformat() if dt else None


def get_location_map():
    """Build list_id -> location info."""
    loc = {}
    for space in fetch_all_spaces():
        sid, sname = space["id"], space["name"]
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


def _get_custom_field(task, name):
    """Get custom field value by name."""
    for f in task.get("custom_fields", []):
        if (f.get("name") or "").lower() == name.lower() and f.get("value"):
            return f["value"]
    return None


def _get_sprint_points(task):
    """Get sprint points from native field or custom field."""
    if task.get("points"):
        try:
            return int(float(task["points"]))
        except Exception:
            pass
    val = (
        _get_custom_field(task, "sprint points")
        or _get_custom_field(task, "points")
        or _get_custom_field(task, "story points")
    )
    if val:
        try:
            return int(float(val))
        except Exception:
            pass
    return None


def sync_tasks_to_supabase(tasks, *, full_sync):
    if not tasks:
        return 0

    emp_map, loc_map = get_employee_id_map(), get_location_map()
    now = datetime.now(timezone.utc).isoformat()
    task_ids = [t["id"] for t in tasks]

    # Deleted detection (full sync)
    if full_sync:
        deleted = get_existing_task_ids() - set(task_ids)
        if deleted:
            mark_tasks_deleted(list(deleted), now)

    # Batch fetch
    print(f"⏱️  Fetching time entries for {len(tasks)} tasks...")
    time_map = fetch_all_time_entries_batch(task_ids)
    print(f"💬 Fetching comments for {len(tasks)} tasks...")
    comment_map = fetch_assigned_comments_batch(task_ids)

    # Build payloads
    payloads = []
    for t in tasks:
        tid, status, loc = (
            t["id"],
            t.get("status", {}),
            loc_map.get(t.get("list", {}).get("id"), {}),
        )
        assignees = t.get("assignees") or []
        agg = aggregate_time_entries(time_map.get(tid, []))

        assignee_ids = [str(a["id"]) for a in assignees if a.get("id")]
        assignee_names = [a["username"] for a in assignees if a.get("username")]
        employee_ids = [
            emp_map[str(a["id"])] for a in assignees if str(a.get("id")) in emp_map
        ]

        # Improved type mapping for tasks
        custom_item_id = t.get("custom_item_id")
        type_val = None

        # Prefer explicit type field if present and matches known types
        t_type = (t.get("type") or "").strip().lower()
        if t_type == "meeting note":
            type_val = "meeting note"
        elif t_type in ["form", "form response"]:
            type_val = "form response"
        elif t.get("is_milestone"):
            type_val = "milestone"
        elif custom_item_id in TYPE_MAP:
            type_val = TYPE_MAP[custom_item_id]
        else:
            type_val = t.get("type") or "task"

        payloads.append(
            {
                "clickup_task_id": tid,
                "title": t.get("name"),
                "description": t.get("text_content"),
                "type": type_val,
                "status": status.get("status", ""),
                "status_type": status.get("type", ""),
                "priority": (t.get("priority") or {}).get("priority"),
                "tags": ", ".join(
                    x["name"] for x in (t.get("tags") or []) if x.get("name")
                )
                or None,
                "summary": _get_custom_field(t, "summary"),
                "sprint_points": _get_sprint_points(t),
                "assigned_comment": comment_map.get(tid),
                "assignee_name": ", ".join(assignee_names) or None,
                "assignee_ids": ", ".join(assignee_ids) or None,
                "employee_id": employee_ids[0] if employee_ids else None,
                "employee_ids": employee_ids or None,
                "assigned_by": (t.get("creator") or {}).get("username"),
                "followers": ", ".join(
                    w["username"]
                    for w in (t.get("watchers") or [])
                    if w.get("username")
                )
                or None,
                **loc,
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
                "start_times": agg["start_times"] or None,
                "end_times": agg["end_times"] or None,
                "tracked_minutes": agg["tracked_minutes"],
                "archived": t.get("archived", False),
                "is_deleted": False,
                "updated_at": now,
            }
        )

    print(f"💾 Upserting {len(payloads)} tasks...")
    bulk_upsert_tasks(payloads)
    print("✅ Sync complete")

    # Incremental: refresh comments for other tasks
    if not full_sync:
        remaining = [tid for tid in get_all_task_ids() if tid not in task_ids]
        if remaining:
            print(f"💬 Refreshing comments for {len(remaining)} other tasks...")
            bulk_update_comments(fetch_assigned_comments_batch(remaining), now)

    return len(payloads)
