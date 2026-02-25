"""
Shared ClickUp helpers for MCP Server — Single Source of Truth.

Eliminates duplication of status logic, task metrics calculation,
and formatting across pm_analytics, task_management, project_intelligence,
and project_configuration modules.
"""

from typing import Dict, List, Optional


# ============================================================================
# STATUS CONFIGURATION (Single Source of Truth)
# ============================================================================

STATUS_NAME_OVERRIDES = {
    "not_started": [
        "BACKLOG",
        "QUEUED",
        "QUEUE",
        "IN QUEUE",
        "TO DO",
        "TO-DO",
        "PENDING",
        "OPEN",
        "IN PLANNING",
    ],
    "active": [
        "SCOPING",
        "IN DESIGN",
        "DEV",
        "IN DEVELOPMENT",
        "DEVELOPMENT",
        "REVIEW",
        "IN REVIEW",
        "TESTING",
        "QA",
        "BUG",
        "BLOCKED",
        "WAITING",
        "STAGING DEPLOY",
        "READY FOR DEVELOPMENT",
        "READY FOR PRODUCTION",
        "IN PROGRESS",
        "ON HOLD",
    ],
    "done": [
        "SHIPPED",
        "RELEASE",
        "COMPLETE",
        "DONE",
        "RESOLVED",
        "PROD",
        "QC CHECK",
    ],
    "closed": ["CANCELLED", "CLOSED"],
}

STATUS_OVERRIDE_MAP = {
    s.upper(): cat for cat, statuses in STATUS_NAME_OVERRIDES.items() for s in statuses
}


def get_status_category(status_name: str, status_type: str = None) -> str:
    """Determine status category from name and/or ClickUp type."""
    if not status_name:
        return "other"
    if cat := STATUS_OVERRIDE_MAP.get(status_name.upper()):
        return cat
    if status_type:
        type_map = {
            "open": "not_started",
            "done": "done",
            "closed": "closed",
            "custom": "active",
        }
        return type_map.get(status_type.lower(), "other")
    return "other"


def extract_status_name(task: Dict) -> str:
    """Safely extract status name from task object."""
    status = task.get("status")
    if isinstance(status, dict):
        return status.get("status", "Unknown")
    return str(status) if status else "Unknown"


def extract_status_type(task: Dict) -> Optional[str]:
    """Extract status type from task object."""
    status = task.get("status")
    if isinstance(status, dict):
        return status.get("type")
    return None


# ============================================================================
# FORMATTING HELPERS
# ============================================================================


def format_duration(ms) -> str:
    """Format milliseconds to 'Xh Ym' style."""
    if not ms:
        return "0h 0m"
    mins = int(ms) // 60000
    return f"{mins // 60}h {mins % 60}m"


def format_duration_short(ms: int) -> str:
    """Short format: '3h 4m' or '45m' or '0m'."""
    if not ms:
        return "0m"
    total_minutes = int(ms) // 60000
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours > 0 and minutes > 0:
        return f"{hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h 0m"
    return f"{minutes}m"


def format_duration_verbose(ms: int) -> str:
    """Verbose format for task detail display."""
    if not ms:
        return "0 min"
    seconds = int(ms) // 1000
    if seconds < 60:
        return f"{seconds} sec"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr {minutes % 60} min"
    days = hours // 24
    return f"{days} d {hours % 24} hr"


def ms_to_date(ms) -> str:
    """Convert ms timestamp to YYYY-MM-DD."""
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if ms
        else "N/A"
    )


def format_assignees(assignees) -> list:
    """Extract usernames from assignee list."""
    return [a.get("username") for a in (assignees or []) if a.get("username")]


def safe_get(obj, *keys):
    """Safely navigate nested dicts."""
    for key in keys:
        obj = obj.get(key) if isinstance(obj, dict) else None
        if obj is None:
            return None
    return obj


# ============================================================================
# TASK METRICS ENGINE (Bottom-Up Calculation)
# ============================================================================


def calculate_task_metrics(all_tasks: List[Dict]) -> Dict[str, Dict[str, int]]:
    """
    Bottom-up time calculation engine.
    Returns: { task_id: { tracked_total, tracked_direct, est_total, est_direct } }
    """
    task_map = {t["id"]: t for t in all_tasks}
    children_map: Dict[str, List[str]] = {}
    for t in all_tasks:
        pid = t.get("parent")
        if pid:
            children_map.setdefault(pid, []).append(t["id"])

    cache: Dict[str, tuple] = {}

    def get_values(tid):
        if tid in cache:
            return cache[tid]
        task_obj = task_map.get(tid)
        if not task_obj:
            return (0, 0, 0, 0)

        api_tracked = int(task_obj.get("time_spent") or 0)
        api_est = int(task_obj.get("time_estimate") or 0)

        sum_child_tracked = sum_child_est = 0
        for cid in children_map.get(tid, []):
            ct, _, ce, _ = get_values(cid)
            sum_child_tracked += ct
            sum_child_est += ce

        direct_tracked = (
            max(0, api_tracked - sum_child_tracked)
            if api_tracked >= sum_child_tracked
            else api_tracked
        )
        direct_est = (
            max(0, api_est - sum_child_est) if api_est >= sum_child_est else api_est
        )

        res = (
            direct_tracked + sum_child_tracked,
            direct_tracked,
            direct_est + sum_child_est,
            direct_est,
        )
        cache[tid] = res
        return res

    for tid in task_map:
        get_values(tid)

    return {
        tid: {
            "tracked_total": r[0],
            "tracked_direct": r[1],
            "est_total": r[2],
            "est_direct": r[3],
        }
        for tid, r in cache.items()
    }


# ============================================================================
# MISSING PARENT FETCHER (Cross-list accuracy)
# ============================================================================


def fetch_missing_parents(all_tasks: List[Dict]) -> List[Dict]:
    """
    Fetch parent tasks that live in different lists.
    Ensures cross-list time rollups are accurate.
    """
    from .api_client import client

    existing_ids = {t["id"] for t in all_tasks}
    missing = {
        t.get("parent")
        for t in all_tasks
        if t.get("parent") and t["parent"] not in existing_ids
    }

    if not missing:
        return all_tasks

    extended = all_tasks.copy()
    for pid in missing:
        data, err = client.get(f"/task/{pid}", params={"include_subtasks": "true"})
        if data and not err and data["id"] not in existing_ids:
            existing_ids.add(data["id"])
            extended.append(data)

            # Fetch grandparent (1 level up) if needed
            gp = data.get("parent")
            if gp and gp not in existing_ids:
                gp_data, _ = client.get(
                    f"/task/{gp}", params={"include_subtasks": "true"}
                )
                if gp_data and gp_data["id"] not in existing_ids:
                    existing_ids.add(gp_data["id"])
                    extended.append(gp_data)

    return extended
