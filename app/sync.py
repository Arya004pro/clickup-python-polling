"""
Task Sync - ClickUp to PostgreSQL
"""

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict
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


def _ms_to_dt(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc) if ms else None


def _ms_to_date(ms):
    return _ms_to_dt(ms).astimezone(IST).date().isoformat() if ms else None


def _to_iso(dt):
    return dt.isoformat() if dt else None


def _ms_to_ist_iso(ms):
    """
    Convert ClickUp ms timestamp → IST ISO string
    """
    if not ms:
        return None
    return (
        datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        .astimezone(IST)
        .isoformat()
    )


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

    import time
    import logging

    logger = logging.getLogger("sync-profile")
    t0 = time.perf_counter()
    emp_map, loc_map = get_employee_id_map(), get_location_map()
    now = datetime.now(IST).isoformat()
    task_ids = [t["id"] for t in tasks]

    # Deleted detection (full sync)
    if full_sync:
        deleted = get_existing_task_ids() - set(task_ids)
        if deleted:
            mark_tasks_deleted(list(deleted), now)

    # Batch fetch time entries and comments
    if full_sync:
        print(f"🔄 Full sync: fetching time entries for {len(task_ids)} tasks")
        time_map = fetch_all_time_entries_batch(task_ids)
    else:
        print(f"⚡ Incremental sync: skipping time entries for {len(task_ids)} tasks")
        time_map = {tid: [] for tid in task_ids}  # Empty for incremental

    t1 = time.perf_counter()
    logger.info(f"[PROFILE] Pre-fetch setup: {t1 - t0:.2f}s")

    # Batch fetch
    t2 = time.perf_counter()
    time_map = fetch_all_time_entries_batch(task_ids)
    t3 = time.perf_counter()
    logger.info(f"[PROFILE] Time entry fetch: {t3 - t2:.2f}s for {len(task_ids)} tasks")

    t4 = time.perf_counter()
    comment_map = fetch_assigned_comments_batch(task_ids)
    t5 = time.perf_counter()
    logger.info(f"[PROFILE] Comment fetch: {t5 - t4:.2f}s for {len(task_ids)} tasks")

    # Step 1: Create a map of task IDs to names from the current batch.
    task_id_to_name_map = {t["id"]: t.get("name", t["id"]) for t in tasks}

    # Step 2: Pre-process all tasks to build a complete, two-way dependency map.
    dependency_strings_map = defaultdict(list)
    dependency_type_map = {
        1: ("blocking", "waiting on"),
        2: ("related to", "related to"),
        3: ("linked to", "linked from"),
        4: ("custom", "custom"),
    }
    for task in tasks:
        # This logic assumes `dependencies` means "other tasks that depend on this task".
        for dep in task.get("dependencies", []):
            dependent_task_id = dep.get("task_id")
            other_task_id = dep.get("depends_on") or dep.get("task_id")

            if not dependent_task_id or not other_task_id:
                continue

            dependent_task_name = task_id_to_name_map.get(
                dependent_task_id, dependent_task_id
            )
            other_task_name = task_id_to_name_map.get(other_task_id, other_task_id)

            dep_type = dep.get("type")
            if dep_type not in dependency_type_map:
                continue

            dep_strings = dependency_type_map[dep_type]
            dependency_strings_map[other_task_id].append(
                f"{dep_strings[0]} '{dependent_task_name}'"
            )
            dependency_strings_map[dependent_task_id].append(
                f"{dep_strings[1]} '{other_task_name}'"
            )

    t6 = time.perf_counter()
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

        # Determine task type
        t_type = (t.get("type") or "").strip().lower()
        if t.get("is_milestone"):
            type_val = "milestone"
        elif t_type == "meeting note":
            type_val = "meeting note"
        elif t_type in ["form", "form response"]:
            type_val = "form response"
        else:
            type_val = t.get("type") or "task"

        dep_strings = sorted(list(set(dependency_strings_map.get(tid, []))))

        # Extract last_status_change from status_history if available, else fallback to date_updated
        last_status_change = None
        status_history = t.get("status_history") or t.get("history")
        if status_history and isinstance(status_history, list):
            # Find the most recent status change event
            last_event = max(status_history, key=lambda e: e.get("date", 0))
            last_status_change = _ms_to_ist_iso(last_event.get("date"))
        if not last_status_change:
            last_status_change = _ms_to_ist_iso(t.get("date_updated"))
        # Always ensure date_created is ISO string with timezone
        date_created = t.get("date_created")
        if date_created:
            date_created = _to_iso(_ms_to_dt(date_created).astimezone(IST))
        else:
            date_created = None
        recurring_field = t.get("recurring")
        is_recurring = isinstance(recurring_field, list) and len(recurring_field) > 0

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
                # Store all timestamps in IST with full time
                "date_created": date_created,
                "date_updated": _to_iso(
                    _ms_to_dt(t.get("date_updated")).astimezone(IST)
                )
                if t.get("date_updated")
                else None,
                "date_done": _to_iso(_ms_to_dt(t.get("date_done")).astimezone(IST))
                if t.get("date_done")
                else None,
                "date_closed": _to_iso(_ms_to_dt(t.get("date_closed")).astimezone(IST))
                if status.get("type") == "closed" and t.get("date_closed")
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
                "is_recurring": is_recurring,
                "updated_at": _ms_to_ist_iso(t.get("date_updated")),
                "last_status_change": last_status_change,
                "dependencies": json.dumps(dep_strings) if dep_strings else None,
            }
        )

    # Upsert tasks
    bulk_upsert_tasks(payloads)
    # print(f"💾 Upserting {len(payloads)} tasks...")
    t7 = time.perf_counter()
    logger.info(f"[PROFILE] Payload build: {t7 - t6:.2f}s for {len(payloads)} tasks")
    upsert_start = time.perf_counter()
    bulk_upsert_tasks(payloads)
    upsert_end = time.perf_counter()
    logger.info(
        f"[PROFILE] Bulk upsert: {upsert_end - upsert_start:.2f}s for {len(payloads)} tasks"
    )

    # Incremental: refresh comments for other tasks
    if not full_sync:
        remaining = [tid for tid in get_all_task_ids() if tid not in task_ids]
        if remaining:
            bulk_update_comments(fetch_assigned_comments_batch(remaining), now)

    return len(payloads)
