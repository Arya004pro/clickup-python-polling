"""
PM Analytics Module for ClickUp MCP Server - FINAL REFACTOR
Features:
1. Deep Nesting Fix: Uses 'subtasks=true' to fetch all levels flattened.
2. Centralized Math: '_calculate_task_metrics' helper powers ALL reports.
3. Estimation Accuracy: Properly implemented using bottom-up sums.
4. Robust Status Logic: Identifies 'Shipped', 'Release', etc. as DONE.
5. Complete Toolset: Includes all analytics, breakdowns, and risk assessments.
"""

from __future__ import annotations

from fastmcp import FastMCP
import requests
import time
from datetime import datetime, timezone
from typing import List, Optional, Dict
from app.mcp.status_helpers import (
    get_current_week_dates,
    date_range_to_timestamps,
    filter_time_entries_by_date_range,
    is_valid_monday_sunday_range,
    parse_week_input,
    validate_week_dates,
)
from app.config import CLICKUP_API_TOKEN, BASE_URL

try:
    from app.config import CLICKUP_TEAM_ID
except ImportError:
    CLICKUP_TEAM_ID = None

# --- Standardized Status Logic ---
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
    "done": ["SHIPPED", "RELEASE", "COMPLETE", "DONE", "RESOLVED", "PROD", "QC CHECK"],
    "closed": ["CANCELLED", "CLOSED"],
}

STATUS_OVERRIDE_MAP = {
    s.upper(): cat for cat, statuses in STATUS_NAME_OVERRIDES.items() for s in statuses
}


def get_status_category(status_name: str, status_type: str = None) -> str:
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


# --- API & Data Helpers ---


def _headers() -> Dict[str, str]:
    return {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}


# --- Date/Timestamp Helpers ---


def date_to_timestamp_ms(date_str: str) -> int:
    """
    Convert YYYY-MM-DD date string to Unix timestamp in milliseconds.
    Time is set to 00:00:00 UTC.

    Args:
        date_str: Date in YYYY-MM-DD format

    Returns:
        Unix timestamp in milliseconds

    Example:
        date_to_timestamp_ms("2024-01-15") -> 1705276800000
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _api_call(method: str, endpoint: str, params: Optional[Dict] = None):
    url = f"{BASE_URL}{endpoint}"
    try:
        response = requests.request(method, url, headers=_headers(), params=params)
        return (
            (response.json(), None)
            if response.status_code == 200
            else (None, f"API Error {response.status_code}")
        )
    except Exception as e:
        return None, str(e)


def _get_team_id() -> str:
    if CLICKUP_TEAM_ID:
        return CLICKUP_TEAM_ID
    data, _ = _api_call("GET", "/team")
    return data["teams"][0]["id"] if data and data.get("teams") else "0"


def _resolve_to_list_ids(project: Optional[str], list_id: Optional[str]) -> List[str]:
    """
    Resolve project name to list IDs.
    Priority:
    1. Direct list_id if provided
    2. project_map.json mapped projects (handles folders/spaces)
    3. Live API search by name
    """
    if list_id:
        return [list_id]
    if not project:
        return []

    try:
        from .sync_mapping import db

        proj_lower = project.lower().strip()

        for alias, data in db.projects.items():
            alias_lower = alias.lower()
            stored_alias = data.get("alias", "").lower()

            if proj_lower in [alias_lower, stored_alias]:
                mapped_id = data["clickup_id"]
                mapped_type = data["clickup_type"]

                print(f"[DEBUG] Resolved '{project}' to {mapped_type} ID: {mapped_id}")

                if mapped_type == "list":
                    return [mapped_id]

                elif mapped_type == "folder":
                    resp, err = _api_call("GET", f"/folder/{mapped_id}/list")
                    if resp and resp.get("lists"):
                        list_ids = [lst["id"] for lst in resp["lists"]]
                        print(f"[DEBUG] Found {len(list_ids)} lists in folder")
                        return list_ids

                    structure = data.get("structure", {})
                    cached_lists = [
                        c["id"]
                        for c in structure.get("children", [])
                        if c.get("type") == "list"
                    ]
                    if cached_lists:
                        print(f"[DEBUG] Using {len(cached_lists)} lists from cache")
                        return cached_lists

                elif mapped_type == "space":
                    ids = []
                    resp, _ = _api_call("GET", f"/space/{mapped_id}/list")
                    if resp:
                        ids.extend([lst["id"] for lst in resp.get("lists", [])])

                    resp2, _ = _api_call("GET", f"/space/{mapped_id}/folder")
                    if resp2:
                        for f in resp2.get("folders", []):
                            ids.extend([lst["id"] for lst in f.get("lists", [])])

                    print(f"[DEBUG] Found {len(ids)} total lists in space")
                    return ids

    except Exception as e:
        print(f"[DEBUG] project_map.json lookup failed: {e}")

    print(f"[DEBUG] Falling back to API search for: {project}")
    team_id = _get_team_id()
    spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
    if not spaces_data:
        return []

    proj_lower = project.lower().strip()
    target_lists = []

    for space in spaces_data.get("spaces", []):
        if space["name"].lower() == proj_lower:
            s_lists, _ = _api_call("GET", f"/space/{space['id']}/list")
            if s_lists:
                target_lists.extend([lst["id"] for lst in s_lists.get("lists", [])])
            s_folders, _ = _api_call("GET", f"/space/{space['id']}/folder")
            if s_folders:
                for f in s_folders.get("folders", []):
                    target_lists.extend([lst["id"] for lst in f.get("lists", [])])
            return target_lists

        f_data, _ = _api_call("GET", f"/space/{space['id']}/folder")
        if f_data:
            for f in f_data.get("folders", []):
                if f["name"].lower() == proj_lower:
                    folder_lists = [lst["id"] for lst in f.get("lists", [])]
                    print(
                        f"[DEBUG] Found folder '{f['name']}' with {len(folder_lists)} lists"
                    )
                    return folder_lists

    print(f"[DEBUG] No matches found for '{project}'")
    return []


def _fetch_all_tasks(
    list_ids: List[str], base_params: Dict, include_archived: bool = True
) -> List[Dict]:
    """Fetch ALL tasks including nested subtasks and archived items."""
    all_tasks = []
    seen_ids = set()
    flags = [False, True] if include_archived else [False]

    for list_id in list_ids:
        for is_archived in flags:
            page = 0
            while True:
                params = {
                    **base_params,
                    "page": page,
                    "subtasks": "true",
                    "include_closed": "true",
                    "archived": str(is_archived).lower(),
                }
                data, error = _api_call("GET", f"/list/{list_id}/task", params=params)
                if error or not data:
                    break

                tasks = [t for t in data.get("tasks", []) if isinstance(t, dict)]
                if not tasks:
                    break

                for t in tasks:
                    if t.get("id") not in seen_ids:
                        seen_ids.add(t.get("id"))
                        all_tasks.append(t)

                if len(tasks) < 100:
                    break
                page += 1
    return all_tasks


def _calculate_task_metrics(all_tasks: List[Dict]) -> Dict[str, Dict[str, int]]:
    """Bottom-up time calculation engine."""
    task_map = {t["id"]: t for t in all_tasks}
    children_map = {}
    for t in all_tasks:
        pid = t.get("parent")
        if pid:
            children_map.setdefault(pid, []).append(t["id"])

    cache = {}

    def get_values(tid):
        if tid in cache:
            return cache[tid]
        task_obj = task_map.get(tid, {})
        if not task_obj:
            return (0, 0, 0, 0)

        api_tracked = int(task_obj.get("time_spent") or 0)
        api_est = int(task_obj.get("time_estimate") or 0)

        sum_child_tracked, sum_child_est = 0, 0
        for cid in children_map.get(tid, []):
            c_track, _, c_est, _ = get_values(cid)
            sum_child_tracked += c_track
            sum_child_est += c_est

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

    final_map = {}
    for tid, res in cache.items():
        final_map[tid] = {
            "tracked_total": res[0],
            "tracked_direct": res[1],
            "est_total": res[2],
            "est_direct": res[3],
        }
    return final_map


# --- Formatting Helpers ---


def _ms_to_readable(ms):
    return (
        datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if ms
        else "N/A"
    )


def _format_duration(ms):
    if not ms:
        return "0h 0m"
    mins = int(ms) // 60000
    return f"{mins // 60}h {mins % 60}m"


def _safe_int_from_dates(task: Dict, fields: List[str]) -> int:
    dates = []
    for f in fields:
        if val := task.get(f):
            try:
                dates.append(int(val))
            except Exception:
                pass
    return max(dates) if dates else 0


def _extract_status_name(task: Dict) -> str:
    """Safely extracts status name handling both dict and string formats."""
    status = task.get("status")
    if isinstance(status, dict):
        return status.get("status", "Unknown")
    return str(status) if status else "Unknown"


# --- Tools ---


def register_pm_analytics_tools(mcp: FastMCP):
    import uuid
    import threading

    JOBS: Dict[str, Dict] = {}

    @mcp.tool()
    def get_progress_since(
        since_date: str,
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        include_status_changes: bool = True,
        include_archived: bool = False,
    ) -> dict:
        """
        Get tasks completed or changed since date.
        Correctly identifies 'Shipped' as Done.
        Provides breakdown of subtasks vs main tasks.
        """
        try:
            if "T" not in since_date:
                since_date += "T00:00:00Z"
            since_ms = int(
                datetime.fromisoformat(since_date.replace("Z", "+00:00")).timestamp()
                * 1000
            )

            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": f"No context found for '{project or list_id}'"}

            tasks = _fetch_all_tasks(
                list_ids,
                {"date_updated_gt": since_ms},
                include_archived=include_archived,
            )
            completed, status_changes = [], []

            metrics = {
                "category_counts": {
                    "not_started": 0,
                    "active": 0,
                    "done": 0,
                    "closed": 0,
                    "unknown": 0,
                },
                "status_name_counts": {},
                "type_breakdown": {"main_tasks": 0, "subtasks": 0},
            }

            for t in tasks:
                status_obj = (
                    t.get("status", {}) if isinstance(t.get("status"), dict) else {}
                )
                status_name = _extract_status_name(t)
                cat = get_status_category(status_name, status_obj.get("type", ""))

                if cat in ["done", "closed"]:
                    done_date = (
                        t.get("date_closed")
                        or t.get("date_done")
                        or t.get("date_updated")
                    )
                    if done_date and int(done_date) >= since_ms:
                        completed.append(
                            {
                                "name": t.get("name"),
                                "status": status_name,
                                "completed_at": _ms_to_readable(done_date),
                                "is_subtask": bool(t.get("parent")),
                            }
                        )

                if include_status_changes:
                    if (upd := t.get("date_updated")) and int(upd) >= since_ms:
                        status_changes.append(
                            {
                                "name": t.get("name"),
                                "status": status_name,
                                "changed_at": _ms_to_readable(upd),
                            }
                        )

                    metrics["status_name_counts"][status_name] = (
                        metrics["status_name_counts"].get(status_name, 0) + 1
                    )

                    if cat in metrics["category_counts"]:
                        metrics["category_counts"][cat] += 1
                    else:
                        metrics["category_counts"]["unknown"] += 1

                    if t.get("parent"):
                        metrics["type_breakdown"]["subtasks"] += 1
                    else:
                        metrics["type_breakdown"]["main_tasks"] += 1

            return {
                "completed_tasks": completed,
                "total_completed": len(completed),
                "status_changes": status_changes if include_status_changes else None,
                "metrics": metrics,
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_time_tracking_report(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        group_by: str = "assignee",
        include_archived: bool = True,  # NEW: Include archived tasks
        status_filter: Optional[List[str]] = None,  # NEW: Optional status filter
    ) -> dict:
        """
        CRITICAL: This report includes ALL tasks from ALL statuses by default.

        Team member-wise time tracking report for a project (folder/space/list).
        Includes completed, in-progress, backlog, and all other statuses.

        Use Cases:
        - "team member wise time report for Luminique" → Shows ALL tasks across ALL statuses
        - "time tracking for Luminique" → Complete view including backlog, completed, cancelled

        Args:
            project: Project name (from project_map.json or space/folder name)
            list_id: Direct list ID (alternative to project)
            group_by: "assignee", "status", or "task"
            include_archived: Include archived tasks (default: True)
            status_filter: Optional list of status names to filter (default: all)

        Returns:
            Complete time report with all tasks included by default
        """
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": "No context found."}

            all_tasks = _fetch_all_tasks(
                list_ids,
                {},  # No date filters - get everything
                include_archived=include_archived,
            )

            print(f"[DEBUG] Fetched {len(all_tasks)} total tasks")

            if status_filter:
                filtered_tasks = []
                for t in all_tasks:
                    status_name = _extract_status_name(t)
                    if status_name in status_filter:
                        filtered_tasks.append(t)
                all_tasks = filtered_tasks
                print(f"[DEBUG] After status filter: {len(all_tasks)} tasks")

            metrics = _calculate_task_metrics(all_tasks)
            report = {}

            for t in all_tasks:
                m = metrics.get(t["id"], {})
                val_t = (
                    m.get("tracked_direct", 0)
                    if group_by == "assignee"
                    else m.get("tracked_total", 0)
                )
                val_e = (
                    m.get("est_direct", 0)
                    if group_by == "assignee"
                    else m.get("est_total", 0)
                )

                if val_t == 0 and val_e == 0:
                    continue

                keys = (
                    [u["username"] for u in t.get("assignees", [])] or ["Unassigned"]
                    if group_by == "assignee"
                    else [_extract_status_name(t)]
                )
                if group_by == "task":
                    keys = [t.get("name")]

                for k in keys:
                    r = report.setdefault(
                        k,
                        {
                            "tasks": 0,
                            "time_tracked": 0,
                            "time_estimate": 0,
                            "status_breakdown": {},  # NEW: Add status breakdown
                        },
                    )
                    r["tasks"] += 1
                    div = len(keys) if group_by == "assignee" else 1
                    r["time_tracked"] += val_t // div
                    r["time_estimate"] += val_e // div

                    status_name = _extract_status_name(t)
                    r["status_breakdown"][status_name] = (
                        r["status_breakdown"].get(status_name, 0) + 1
                    )

            formatted = {
                k: {
                    **v,
                    "human_tracked": _format_duration(v["time_tracked"]),
                    "human_est": _format_duration(v["time_estimate"]),
                    "efficiency": f"{round(v['time_tracked'] / v['time_estimate'] * 100)}%"
                    if v["time_estimate"] > 0
                    else "N/A",
                }
                for k, v in report.items()
            }

            return {
                "report": formatted,
                "total_tasks": len(all_tasks),
                "filters_applied": {
                    "include_archived": include_archived,
                    "status_filter": status_filter or "all statuses",
                },
            }
        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_time_tracking_report: {e}")
            print(traceback.format_exc())
            return {"error": str(e)}

    @mcp.tool()
    def get_task_time_breakdown(task_id: str) -> dict:
        """Detailed breakdown of a task tree."""
        try:
            task_data, err = _api_call(
                "GET", f"/task/{task_id}", params={"include_subtasks": "true"}
            )
            if err:
                return {"error": err}

            list_id = task_data["list"]["id"]
            all_list_tasks = _fetch_all_tasks([list_id], {})
            metrics_map = _calculate_task_metrics(all_list_tasks)

            task_map = {t["id"]: t for t in all_list_tasks}
            children_map = {}
            for t in all_list_tasks:
                if pid := t.get("parent"):
                    children_map.setdefault(pid, []).append(t["id"])

            tree_view = []

            def build_tree(tid, depth=0):
                t = task_map.get(tid)
                if not t:
                    return
                m = metrics_map.get(tid, {})

                tree_view.append(
                    {
                        "task": f"{'  ' * depth}{t.get('name')}",
                        "status": _extract_status_name(t),
                        "tracked_total": _format_duration(m.get("tracked_total", 0)),
                        "tracked_direct": _format_duration(m.get("tracked_direct", 0)),
                        "estimated": _format_duration(m.get("est_total", 0)),
                    }
                )
                for cid in children_map.get(tid, []):
                    build_tree(cid, depth + 1)

            build_tree(task_id)
            return {"root_task": task_data["name"], "breakdown_tree": tree_view}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_estimation_accuracy(
        project: Optional[str] = None, list_id: Optional[str] = None
    ) -> dict:
        """Analyze estimation vs actuals using robust metrics."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": "No context"}
            tasks = _fetch_all_tasks(list_ids, {})
            metrics = _calculate_task_metrics(tasks)

            est_total, spent_on_est, spent_unest = 0, 0, 0
            over, under, accurate = 0, 0, 0

            for t in tasks:
                m = metrics.get(t["id"], {})
                dt, de = m.get("tracked_direct", 0), m.get("est_direct", 0)

                if de > 0:
                    est_total += de
                    spent_on_est += dt
                    ratio = dt / de if de else 0
                    if dt == 0:
                        over += 1
                    elif ratio < 0.8:
                        over += 1
                    elif ratio > 1.2:
                        under += 1
                    else:
                        accurate += 1
                elif dt > 0:
                    spent_unest += dt

            return {
                "total_estimated": _format_duration(est_total),
                "spent_on_estimated": _format_duration(spent_on_est),
                "spent_unplanned": _format_duration(spent_unest),
                "accuracy_breakdown": {
                    "accurate": accurate,
                    "under_estimated": under,
                    "over_estimated": over,
                },
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_at_risk_tasks(
        project: Optional[str] = None, list_id: Optional[str] = None, risk_days: int = 3
    ) -> dict:
        """Find tasks overdue or due soon."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": "No context"}
            tasks = _fetch_all_tasks(list_ids, {})
            now = time.time() * 1000
            limit = now + (risk_days * 86400000)

            risks = []
            for t in tasks:
                status = (
                    t.get("status", {}) if isinstance(t.get("status"), dict) else {}
                )
                status_name = _extract_status_name(t)
                cat = get_status_category(status_name, status.get("type"))

                if cat in ["active", "not_started"]:
                    if due := t.get("due_date"):
                        due = int(due)
                        if due < now:
                            risks.append(
                                {
                                    "name": t["name"],
                                    "risk": "Overdue",
                                    "due": _ms_to_readable(due),
                                }
                            )
                        elif due <= limit:
                            risks.append(
                                {
                                    "name": t["name"],
                                    "risk": "Due Soon",
                                    "due": _ms_to_readable(due),
                                }
                            )

            return {"at_risk_count": len(risks), "tasks": risks}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_stale_tasks(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        stale_days: int = 7,
    ) -> dict:
        """Find tasks with no updates."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": "No context"}
            tasks = _fetch_all_tasks(list_ids, {})
            now = time.time() * 1000
            cutoff = now - (stale_days * 86400000)
            stale = []

            for t in tasks:
                status_name = _extract_status_name(t)
                cat = get_status_category(status_name)
                if cat not in ["done", "closed"]:
                    updated = int(t.get("date_updated") or 0)
                    if updated < cutoff:
                        stale.append(
                            {"name": t["name"], "last_update": _ms_to_readable(updated)}
                        )

            return {"stale_count": len(stale), "tasks": stale}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_untracked_tasks(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        status_filter: str = "in_progress",
    ) -> dict:
        """Find tasks with zero logged time."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": "No context"}
            tasks = _fetch_all_tasks(list_ids, {})
            metrics = _calculate_task_metrics(tasks)
            untracked = []

            for t in tasks:
                status_obj = (
                    t.get("status", {}) if isinstance(t.get("status"), dict) else {}
                )
                status_name = _extract_status_name(t)
                cat = get_status_category(status_name, status_obj.get("type"))

                check = (status_filter == "all") or (
                    status_filter == "in_progress" and cat == "active"
                )

                if check:
                    if metrics.get(t["id"], {}).get("tracked_direct", 0) == 0:
                        untracked.append({"name": t["name"], "status": status_name})

            return {"count": len(untracked), "tasks": untracked}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_inactive_assignees(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        inactive_days: int = 3,
    ) -> dict:
        """Identify inactive team members."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": "No context"}
            tasks = _fetch_all_tasks(list_ids, {})
            now = time.time() * 1000
            cutoff = now - (inactive_days * 86400000)
            activity_map = {}

            for t in tasks:
                last_act = _safe_int_from_dates(t, ["date_updated", "date_closed"])
                for u in t.get("assignees", []):
                    name = u["username"]
                    if name not in activity_map:
                        activity_map[name] = 0
                    activity_map[name] = max(activity_map[name], last_act)

            inactive = [
                {"user": k, "last_active": _ms_to_readable(v)}
                for k, v in activity_map.items()
                if v < cutoff
            ]
            return {"inactive_count": len(inactive), "users": inactive}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_status_summary(
        project: Optional[str] = None, list_id: Optional[str] = None
    ) -> dict:
        """Summary of task statuses."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": "No context"}
            tasks = _fetch_all_tasks(list_ids, {})
            counts = {}
            categories = {
                "not_started": 0,
                "active": 0,
                "done": 0,
                "closed": 0,
                "other": 0,
            }

            for t in tasks:
                status_obj = (
                    t.get("status", {}) if isinstance(t.get("status"), dict) else {}
                )
                name = _extract_status_name(t)
                cat = get_status_category(name, status_obj.get("type"))

                counts[name] = counts.get(name, 0) + 1
                if cat in categories:
                    categories[cat] += 1
                else:
                    categories["other"] += 1

            return {"total": len(tasks), "by_status": counts, "by_category": categories}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_space_time_report(
        space_name: Optional[str] = None,
        space_id: Optional[str] = None,
        group_by: str = "assignee",
        include_archived: bool = True,
    ) -> dict:
        """
        Time tracking report for entire SPACE (all folders and lists).

        Args:
            space_name: Space name
            space_id: Direct space ID
            group_by: assignee, folder, or status
            include_archived: Include archived tasks
        """
        try:
            if not space_id and not space_name:
                return {"error": "Provide either space_name or space_id"}

            if not space_id:
                team_id = _get_team_id()
                spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
                if not spaces_data:
                    return {"error": "Failed to fetch spaces"}

                for space in spaces_data.get("spaces", []):
                    if space["name"].lower() == space_name.lower():
                        space_id = space["id"]
                        break

                if not space_id:
                    return {"error": f"Space '{space_name}' not found"}

            list_ids = []

            resp, _ = _api_call("GET", f"/space/{space_id}/list")
            if resp:
                list_ids.extend([lst["id"] for lst in resp.get("lists", [])])

            resp2, _ = _api_call("GET", f"/space/{space_id}/folder")
            if resp2:
                for folder in resp2.get("folders", []):
                    list_ids.extend([lst["id"] for lst in folder.get("lists", [])])

            print(f"[DEBUG] Found {len(list_ids)} lists in space")

            all_tasks = _fetch_all_tasks(
                list_ids, {}, include_archived=include_archived
            )
            print(f"[DEBUG] Fetched {len(all_tasks)} tasks from space")

            metrics = _calculate_task_metrics(all_tasks)

            report = {}
            for t in all_tasks:
                m = metrics.get(t["id"], {})
                val_t = (
                    m.get("tracked_direct", 0)
                    if group_by == "assignee"
                    else m.get("tracked_total", 0)
                )
                val_e = (
                    m.get("est_direct", 0)
                    if group_by == "assignee"
                    else m.get("est_total", 0)
                )

                if val_t == 0 and val_e == 0:
                    continue

                if group_by == "assignee":
                    keys = [u["username"] for u in t.get("assignees", [])] or [
                        "Unassigned"
                    ]
                elif group_by == "folder":
                    folder_name = t.get("folder", {}).get("name") or "Folderless"
                    keys = [folder_name]
                else:  # status
                    keys = [_extract_status_name(t)]

                for k in keys:
                    r = report.setdefault(
                        k, {"tasks": 0, "time_tracked": 0, "time_estimate": 0}
                    )
                    r["tasks"] += 1
                    div = len(keys) if group_by == "assignee" else 1
                    r["time_tracked"] += val_t // div
                    r["time_estimate"] += val_e // div

            formatted = {
                k: {
                    **v,
                    "human_tracked": _format_duration(v["time_tracked"]),
                    "human_est": _format_duration(v["time_estimate"]),
                    "efficiency": f"{round(v['time_tracked'] / v['time_estimate'] * 100)}%"
                    if v["time_estimate"] > 0
                    else "N/A",
                }
                for k, v in report.items()
            }

            return {
                "space_name": space_name or "Unknown",
                "space_id": space_id,
                "report": formatted,
                "total_tasks": len(all_tasks),
                "grouped_by": group_by,
            }

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_space_time_report: {e}")
            print(traceback.format_exc())
            return {"error": str(e)}

    @mcp.tool()
    def get_space_folder_team_report(
        space_name: Optional[str] = None,
        space_id: Optional[str] = None,
        include_archived: bool = True,
    ) -> dict:
        """
        Hierarchical time report: Space > Folder > Team Member.

        Args:
            space_name: Space name
            space_id: Direct space ID
            include_archived: Include archived tasks
        """
        try:
            if not space_id and not space_name:
                return {"error": "Provide either space_name or space_id"}

            if not space_id:
                team_id = _get_team_id()
                spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
                if not spaces_data:
                    return {"error": "Failed to fetch spaces"}

                for space in spaces_data.get("spaces", []):
                    if space["name"].lower() == space_name.lower():
                        space_id = space["id"]
                        space_name = space["name"]
                        break

                if not space_id:
                    return {"error": f"Space '{space_name}' not found"}

            folders_report = []

            resp, _ = _api_call("GET", f"/space/{space_id}/folder")
            folders = resp.get("folders", []) if resp else []

            for folder in folders:
                folder_id = folder["id"]
                folder_name = folder["name"]
                list_ids = [lst["id"] for lst in folder.get("lists", [])]

                if not list_ids:
                    continue

                folder_tasks = _fetch_all_tasks(
                    list_ids, {}, include_archived=include_archived
                )
                metrics = _calculate_task_metrics(folder_tasks)

                team_report = {}
                for t in folder_tasks:
                    m = metrics.get(t["id"], {})
                    val_t = m.get("tracked_direct", 0)
                    val_e = m.get("est_direct", 0)

                    if val_t == 0 and val_e == 0:
                        continue

                    assignees = [u["username"] for u in t.get("assignees", [])] or [
                        "Unassigned"
                    ]
                    for member in assignees:
                        r = team_report.setdefault(
                            member, {"tasks": 0, "time_tracked": 0, "time_estimate": 0}
                        )
                        r["tasks"] += 1
                        r["time_tracked"] += val_t // len(assignees)
                        r["time_estimate"] += val_e // len(assignees)

                formatted_team = {
                    member: {
                        **data,
                        "human_tracked": _format_duration(data["time_tracked"]),
                        "human_est": _format_duration(data["time_estimate"]),
                    }
                    for member, data in team_report.items()
                }

                folders_report.append(
                    {
                        "folder_name": folder_name,
                        "folder_id": folder_id,
                        "total_tasks": len(folder_tasks),
                        "team_breakdown": formatted_team,
                    }
                )

            resp, _ = _api_call("GET", f"/space/{space_id}/list")
            folderless_lists = resp.get("lists", []) if resp else []

            if folderless_lists:
                list_ids = [lst["id"] for lst in folderless_lists]
                folderless_tasks = _fetch_all_tasks(
                    list_ids, {}, include_archived=include_archived
                )
                metrics = _calculate_task_metrics(folderless_tasks)

                team_report = {}
                for t in folderless_tasks:
                    m = metrics.get(t["id"], {})
                    val_t = m.get("tracked_direct", 0)
                    val_e = m.get("est_direct", 0)

                    if val_t == 0 and val_e == 0:
                        continue

                    assignees = [u["username"] for u in t.get("assignees", [])] or [
                        "Unassigned"
                    ]
                    for member in assignees:
                        r = team_report.setdefault(
                            member, {"tasks": 0, "time_tracked": 0, "time_estimate": 0}
                        )
                        r["tasks"] += 1
                        r["time_tracked"] += val_t // len(assignees)
                        r["time_estimate"] += val_e // len(assignees)

                formatted_team = {
                    member: {
                        **data,
                        "human_tracked": _format_duration(data["time_tracked"]),
                        "human_est": _format_duration(data["time_estimate"]),
                    }
                    for member, data in team_report.items()
                }

                folders_report.append(
                    {
                        "folder_name": "Folderless Lists",
                        "folder_id": "folderless",
                        "total_tasks": len(folderless_tasks),
                        "team_breakdown": formatted_team,
                    }
                )

            return {
                "space_name": space_name,
                "space_id": space_id,
                "folders": folders_report,
                "total_folders": len(folders_report),
            }

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_space_folder_team_report: {e}")
            print(traceback.format_exc())
            return {"error": str(e)}

    @mcp.tool()
    def get_weekly_time_report(
        report_type: str = "team_member",
        project: Optional[str] = None,
        space_name: Optional[str] = None,
        list_id: Optional[str] = None,
        week_selector: Optional[
            str
        ] = None,  # e.g., "current_week", "last_week", "2-weeks", "month", or custom "YYYY-MM-DD to YYYY-MM-DD"
        week_start: Optional[str] = None,  # YYYY-MM-DD format
        week_end: Optional[str] = None,  # YYYY-MM-DD format
        allow_multi_week: bool = False,  # Enable multi-week ranges
        async_job: bool = False,  # If True, run report in background and return job id
        job_id: Optional[str] = None,  # Internal use when running as background job
    ) -> dict:
        """
        Weekly/Multi-week time report based on time entry intervals.

        Args:
            report_type: team_member, space, or space_folder_team
            project: Project name (for team_member)
            space_name: Space name (for space types)
            list_id: Direct list ID
            week_selector: current, previous, N-weeks-ago, YYYY-MM-DD, YYYY-WNN (or multi-week: 2-weeks, last-2-weeks, month)
            week_start, week_end: YYYY-MM-DD format (explicit dates)
            allow_multi_week: Enable multi-week ranges
            async_job: Run in background (recommended for 300+ tasks)

        Returns:
            Report with time tracked from time entries within date range
        """
        try:
            import sys
            from app.clickup import fetch_all_time_entries_batch

            # Early acknowledgment
            print("⏳ Processing time report request - this may take 1-3 minutes...")
            sys.stdout.flush()

            if async_job:
                jid = job_id or str(uuid.uuid4())
                JOBS[jid] = {"status": "queued", "result": None, "error": None}

                def _bg():
                    try:
                        JOBS[jid]["status"] = "running"
                        res = get_weekly_time_report(
                            report_type=report_type,
                            project=project,
                            space_name=space_name,
                            list_id=list_id,
                            week_selector=week_selector,
                            week_start=week_start,
                            week_end=week_end,
                            allow_multi_week=allow_multi_week,
                            async_job=False,
                            job_id=jid,
                        )
                        JOBS[jid]["result"] = res
                        JOBS[jid]["status"] = "finished"
                    except Exception as e:
                        JOBS[jid]["error"] = str(e)
                        JOBS[jid]["status"] = "failed"

                th = threading.Thread(target=_bg, daemon=True)
                th.start()
                return {
                    "job_id": jid,
                    "status": "started",
                    "message": "Report running in background. Use get_weekly_time_report_status(job_id) to poll.",
                }

            if job_id:
                JOBS.setdefault(job_id, {})["status"] = "running"

            if week_start and week_end:
                # Priority 1: Explicit dates provided - validate and use them
                try:
                    validate_week_dates(
                        week_start, week_end, allow_multi_week=allow_multi_week
                    )
                    print(f"[DEBUG] Using explicit dates: {week_start} to {week_end}")
                    sys.stdout.flush()
                except ValueError as e:
                    return {
                        "error": f"Invalid week dates: {str(e)}",
                        "hint": "week_start must be Monday, week_end must be Sunday"
                        + (
                            " (multi-week allowed if allow_multi_week=True)"
                            if allow_multi_week
                            else ""
                        ),
                    }
            elif week_selector:
                # Priority 2: Use smart week selector
                try:
                    week_start, week_end = parse_week_input(
                        week_selector, allow_multi_week=allow_multi_week
                    )
                    print(
                        f"[DEBUG] Parsed '{week_selector}' → {week_start} to {week_end}"
                    )
                    sys.stdout.flush()
                except ValueError as e:
                    return {
                        "error": f"Invalid week_selector: {str(e)}",
                        "hint": "Supported: 'current', 'previous', 'N-weeks-ago', 'YYYY-MM-DD', 'YYYY-WNN'"
                        + (
                            " + multi-week: 'N-weeks', 'last-N-weeks', 'month'"
                            if allow_multi_week
                            else ""
                        ),
                        "examples": [
                            "current",
                            "previous",
                            "2-weeks-ago",
                            "2026-01-15",
                            "2026-W03",
                        ]
                        + (
                            ["2-weeks", "last-2-weeks", "month"]
                            if allow_multi_week
                            else []
                        ),
                    }
            else:
                # Priority 3: Default to current week
                week_start, week_end = get_current_week_dates()
                print(
                    f"[DEBUG] Using default (current week): {week_start} to {week_end}"
                )
                sys.stdout.flush()

            start_ms, end_ms = date_range_to_timestamps(week_start, week_end)

            from datetime import datetime

            start_date = datetime.strptime(week_start, "%Y-%m-%d")
            end_date = datetime.strptime(week_end, "%Y-%m-%d")
            num_weeks = ((end_date - start_date).days + 1) // 7

            print(
                f"[DEBUG] Report period: {week_start} to {week_end} ({num_weeks} week(s))"
            )
            print(f"[DEBUG] Timestamp range: {start_ms} to {end_ms}")
            sys.stdout.flush()

            if report_type == "space":
                if not space_name:
                    return {"error": "space_name required for space report"}

                # Get space report with ALL tasks (we'll filter time entries, not tasks)
                print(f"[PROGRESS] Step 1/5: Resolving space '{space_name}'...")
                sys.stdout.flush()

                team_id = _get_team_id()
                spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
                if not spaces_data:
                    return {"error": "Failed to fetch spaces"}

                space_id = None
                for space in spaces_data.get("spaces", []):
                    if space["name"].lower() == space_name.lower():
                        space_id = space["id"]
                        break

                if not space_id:
                    return {"error": f"Space '{space_name}' not found"}

                print("[PROGRESS] Step 2/5: Fetching space structure...")
                sys.stdout.flush()

                list_ids = []
                resp, _ = _api_call("GET", f"/space/{space_id}/list")
                if resp:
                    list_ids.extend([lst["id"] for lst in resp.get("lists", [])])
                resp2, _ = _api_call("GET", f"/space/{space_id}/folder")
                if resp2:
                    for folder in resp2.get("folders", []):
                        list_ids.extend([lst["id"] for lst in folder.get("lists", [])])

                print(
                    f"[PROGRESS] Step 3/5: Fetching tasks from {len(list_ids)} lists..."
                )
                sys.stdout.flush()

                all_tasks = _fetch_all_tasks(list_ids, {}, include_archived=True)

                print(f"[PROGRESS] Found {len(all_tasks)} total tasks in space")
                sys.stdout.flush()

                if len(all_tasks) >= 300 and not job_id:
                    print(
                        f"⚡ AUTO-ASYNC: {len(all_tasks)} tasks detected. Switching to background mode to prevent timeout."
                    )
                    sys.stdout.flush()
                    # Recursively call with async_job=True
                    return get_weekly_time_report(
                        report_type=report_type,
                        space_name=space_name,
                        week_selector=week_selector,
                        week_start=week_start,
                        week_end=week_end,
                        allow_multi_week=allow_multi_week,
                        async_job=True,
                    )

                # Fetch time entries for all tasks
                print(
                    f"[PROGRESS] Step 4/5: Fetching time entries for {len(all_tasks)} tasks..."
                )
                print(
                    "[PROGRESS] This may take a few minutes for large datasets. Please wait..."
                )
                sys.stdout.flush()

                task_ids = [t["id"] for t in all_tasks]
                time_entries_map = fetch_all_time_entries_batch(task_ids)

                print(
                    "[PROGRESS] Step 5/5: Processing time entries and building report..."
                )
                sys.stdout.flush()

                metrics = _calculate_task_metrics(all_tasks)

                report = {}
                tasks_with_time_in_range = 0

                for t in all_tasks:
                    task_id = t["id"]
                    time_entries = time_entries_map.get(task_id, [])

                    if not time_entries:
                        continue

                    # Filter time entries by date range
                    time_in_range, filtered_intervals = (
                        filter_time_entries_by_date_range(
                            time_entries, start_ms, end_ms
                        )
                    )

                    if time_in_range == 0:
                        continue

                    tasks_with_time_in_range += 1

                    # Split time among assignees and also add estimated time
                    assignees = [u["username"] for u in t.get("assignees", [])] or [
                        "Unassigned"
                    ]
                    time_per_assignee = time_in_range // len(assignees)

                    # Pull task-level direct estimate (0 if absent)
                    est_direct = metrics.get(task_id, {}).get("est_direct", 0)
                    est_per_assignee = est_direct // len(assignees) if est_direct else 0

                    for member in assignees:
                        r = report.setdefault(
                            member,
                            {
                                "tasks": 0,
                                "time_tracked": 0,
                                "time_estimate": 0,
                                "intervals": [],
                            },
                        )
                        r["tasks"] += 1
                        r["time_tracked"] += time_per_assignee
                        r["time_estimate"] += est_per_assignee
                        r["intervals"].extend(filtered_intervals)

                formatted = {
                    member: {
                        "tasks": data["tasks"],
                        "time_tracked": data["time_tracked"],
                        "human_tracked": _format_duration(data["time_tracked"]),
                        "time_estimate": data.get("time_estimate", 0),
                        "human_est": _format_duration(data.get("time_estimate", 0)),
                        "intervals_count": len(data["intervals"]),
                    }
                    for member, data in report.items()
                }

                return {
                    "report_type": "space_weekly",
                    "space_name": space_name,
                    "week_start": week_start,
                    "week_end": week_end,
                    "num_weeks": num_weeks,
                    "period_description": f"{num_weeks} week(s) from {week_start} to {week_end}",
                    "report": formatted,
                    "total_tasks_in_space": len(all_tasks),
                    "tasks_with_time_in_range": tasks_with_time_in_range,
                }

            elif report_type == "team_member":
                if not project and not list_id:
                    return {"error": "Provide project or list_id"}

                print("[PROGRESS] Step 1/5: Resolving project/list...")
                sys.stdout.flush()

                list_ids = _resolve_to_list_ids(project, list_id)
                if not list_ids:
                    return {"error": "No context found"}

                print(
                    f"[PROGRESS] Step 2/5: Fetching tasks from {len(list_ids)} lists..."
                )
                sys.stdout.flush()

                all_tasks = _fetch_all_tasks(list_ids, {}, include_archived=True)

                print(f"[PROGRESS] Found {len(all_tasks)} total tasks")
                sys.stdout.flush()

                if len(all_tasks) >= 300 and not job_id:
                    print(
                        f"⚡ AUTO-ASYNC: {len(all_tasks)} tasks detected. Switching to background mode to prevent timeout."
                    )
                    sys.stdout.flush()
                    # Recursively call with async_job=True
                    return get_weekly_time_report(
                        report_type=report_type,
                        project=project,
                        list_id=list_id,
                        week_selector=week_selector,
                        week_start=week_start,
                        week_end=week_end,
                        allow_multi_week=allow_multi_week,
                        async_job=True,
                    )

                # Fetch time entries for all tasks
                print(
                    f"[PROGRESS] Step 3/5: Fetching time entries for {len(all_tasks)} tasks..."
                )
                print(
                    "[PROGRESS] This may take a few minutes for large datasets. Please wait..."
                )
                sys.stdout.flush()

                task_ids = [t["id"] for t in all_tasks]
                time_entries_map = fetch_all_time_entries_batch(task_ids)

                print("[PROGRESS] Step 4/5: Processing time entries...")
                sys.stdout.flush()

                metrics = _calculate_task_metrics(all_tasks)

                report = {}
                tasks_with_time_in_range = 0

                for t in all_tasks:
                    task_id = t["id"]
                    time_entries = time_entries_map.get(task_id, [])

                    if not time_entries:
                        continue

                    # Filter time entries by date range
                    time_in_range, filtered_intervals = (
                        filter_time_entries_by_date_range(
                            time_entries, start_ms, end_ms
                        )
                    )

                    if time_in_range == 0:
                        continue

                    tasks_with_time_in_range += 1

                    # Split time among assignees and also add estimated time
                    assignees = [u["username"] for u in t.get("assignees", [])] or [
                        "Unassigned"
                    ]
                    time_per_assignee = time_in_range // len(assignees)

                    # Pull task-level direct estimate (0 if absent)
                    est_direct = metrics.get(task_id, {}).get("est_direct", 0)
                    est_per_assignee = est_direct // len(assignees) if est_direct else 0

                    for member in assignees:
                        r = report.setdefault(
                            member,
                            {
                                "tasks": 0,
                                "time_tracked": 0,
                                "time_estimate": 0,
                                "intervals": [],
                            },
                        )
                        r["tasks"] += 1
                        r["time_tracked"] += time_per_assignee
                        r["time_estimate"] += est_per_assignee
                        r["intervals"].extend(filtered_intervals)

                formatted = {
                    member: {
                        "tasks": data["tasks"],
                        "time_tracked": data["time_tracked"],
                        "human_tracked": _format_duration(data["time_tracked"]),
                        "time_estimate": data.get("time_estimate", 0),
                        "human_est": _format_duration(data.get("time_estimate", 0)),
                        "intervals_count": len(data["intervals"]),
                    }
                    for member, data in report.items()
                }

                print("[PROGRESS] Step 5/5: Report complete!")
                sys.stdout.flush()

                return {
                    "report_type": "team_member_weekly",
                    "project": project or "Direct list",
                    "week_start": week_start,
                    "week_end": week_end,
                    "num_weeks": num_weeks,
                    "period_description": f"{num_weeks} week(s) from {week_start} to {week_end}",
                    "report": formatted,
                    "total_tasks": len(all_tasks),
                    "tasks_with_time_in_range": tasks_with_time_in_range,
                }

            else:
                return {"error": f"Invalid report_type: {report_type}"}

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_weekly_time_report: {e}")
            print(traceback.format_exc())
            return {"error": str(e)}

    @mcp.tool()
    def get_weekly_time_report_status(job_id: str) -> dict:
        """Return status for an async weekly report job."""
        j = JOBS.get(job_id)
        if not j:
            return {"error": "job_id not found"}
        return {"job_id": job_id, "status": j.get("status"), "error": j.get("error")}

    @mcp.tool()
    def get_weekly_time_report_result(job_id: str) -> dict:
        """Return result for finished async weekly report job."""
        j = JOBS.get(job_id)
        if not j:
            return {"error": "job_id not found"}
        if j.get("status") != "finished":
            return {"status": j.get("status"), "message": "Result not ready"}
        return {"status": "finished", "result": j.get("result")}

    @mcp.tool()
    def get_space_weekly_report(
        space_name: str,
        week_selector: Optional[str] = None,
        week_start: Optional[str] = None,
        week_end: Optional[str] = None,
        allow_multi_week: bool = False,
        async_job: bool = False,
    ) -> dict:
        """Space weekly time report with async support.

        Args:
            space_name: ClickUp space name
            week_selector: current, previous, N-weeks-ago, YYYY-MM-DD, YYYY-WNN
            week_start, week_end: YYYY-MM-DD format
            allow_multi_week: Enable multi-week ranges
            async_job: Run in background (use for 300+ tasks)

        Returns:
            Report dict or job_id if async mode
        """
        return get_weekly_time_report(
            report_type="space",
            space_name=space_name,
            week_selector=week_selector,
            week_start=week_start,
            week_end=week_end,
            allow_multi_week=allow_multi_week,
            async_job=async_job,
        )

    @mcp.tool()
    def get_weekly_time_report_detailed(
        report_type: str = "team_member",
        project: Optional[str] = None,
        space_name: Optional[str] = None,
        list_id: Optional[str] = None,
        week_selector: Optional[str] = None,
        week_start: Optional[str] = None,
        week_end: Optional[str] = None,
        timezone_offset: str = "+05:30",  # IST by default
        group_by_date: bool = True,  # Group intervals by date for cleaner output
    ) -> dict:

        try:
            from app.clickup import fetch_all_time_entries_batch
            from app.mcp.status_helpers import (
                format_time_entry_interval,
                format_duration_simple,
                group_intervals_by_date,
            )

            # ================================================================
            # STEP 1: Calculate week boundaries (same as original)
            # ================================================================
            if week_start and week_end:
                is_valid, error_msg = is_valid_monday_sunday_range(week_start, week_end)
                if not is_valid:
                    return {"error": f"Invalid week dates: {error_msg}"}
            elif week_selector:
                try:
                    week_start, week_end = parse_week_input(week_selector)
                except ValueError as e:
                    return {"error": f"Invalid week_selector: {str(e)}"}
            else:
                week_start, week_end = get_current_week_dates()

            start_ms, end_ms = date_range_to_timestamps(week_start, week_end)

            print(
                f"[DEBUG] Detailed report: {week_start} to {week_end} ({timezone_offset})"
            )

            # ================================================================
            # STEP 2: Resolve lists based on report type
            # ================================================================
            if report_type == "team_member":
                if not project and not list_id:
                    return {
                        "error": "Provide project or list_id for team_member report"
                    }
                list_ids = _resolve_to_list_ids(project, list_id)
                if not list_ids:
                    return {"error": "No lists found for project/list_id"}

            elif report_type == "space":
                if not space_name:
                    return {"error": "space_name required for space report"}

                team_id = _get_team_id()
                spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
                if not spaces_data:
                    return {"error": "Failed to fetch spaces"}

                space_id = None
                for space in spaces_data.get("spaces", []):
                    if space["name"].lower() == space_name.lower():
                        space_id = space["id"]
                        break

                if not space_id:
                    return {"error": f"Space '{space_name}' not found"}

                list_ids = []
                resp, _ = _api_call("GET", f"/space/{space_id}/list")
                if resp:
                    list_ids.extend([lst["id"] for lst in resp.get("lists", [])])

                resp2, _ = _api_call("GET", f"/space/{space_id}/folder")
                if resp2:
                    for folder in resp2.get("folders", []):
                        list_ids.extend([lst["id"] for lst in folder.get("lists", [])])

                if not list_ids:
                    return {"error": f"No lists found in space '{space_name}'"}
            else:
                return {
                    "error": f"Invalid report_type: '{report_type}'. Use 'team_member' or 'space'"
                }

            # ================================================================
            # STEP 3: Fetch all tasks and their time entries
            # ================================================================
            all_tasks = _fetch_all_tasks(list_ids, {}, include_archived=True)
            print(f"[DEBUG] Found {len(all_tasks)} total tasks")

            if not all_tasks:
                return {
                    "week_start": week_start,
                    "week_end": week_end,
                    "report": {},
                    "message": "No tasks found",
                }

            task_ids = [t["id"] for t in all_tasks]
            print(f"[DEBUG] Fetching time entries for {len(task_ids)} tasks...")
            time_entries_map = fetch_all_time_entries_batch(task_ids)

            # ================================================================
            # STEP 4: Build detailed report by person
            # ================================================================
            report = {}

            for task in all_tasks:
                task_id = task["id"]
                task_name = task.get("name", "Unnamed Task")
                task_url = task.get("url", f"https://app.clickup.com/t/{task_id}")
                task_status = task.get("status", {})
                status_name = (
                    task_status.get("status", "Unknown")
                    if isinstance(task_status, dict)
                    else "Unknown"
                )

                time_entries = time_entries_map.get(task_id, [])
                if not time_entries:
                    continue

                task_intervals_in_range = []
                task_time_in_range = 0

                for entry in time_entries:
                    for interval in entry.get("intervals", []):
                        interval_start = interval.get("start")
                        if not interval_start:
                            continue

                        try:
                            interval_start = int(interval_start)
                        except (ValueError, TypeError):
                            continue

                        if start_ms <= interval_start <= end_ms:
                            duration = interval.get("time", 0)
                            task_time_in_range += int(duration)

                            formatted_interval = format_time_entry_interval(
                                interval, timezone_offset
                            )
                            task_intervals_in_range.append(formatted_interval)

                if task_time_in_range == 0:
                    continue

                assignees = [u["username"] for u in task.get("assignees", [])] or [
                    "Unassigned"
                ]

                for member in assignees:
                    if member not in report:
                        report[member] = {
                            "summary": {
                                "total_time_tracked_ms": 0,
                                "total_tasks": 0,
                                "total_intervals": 0,
                            },
                            "tasks": [],
                        }

                    if group_by_date and task_intervals_in_range:
                        intervals_by_date = group_intervals_by_date(
                            task_intervals_in_range
                        )
                    else:
                        intervals_by_date = None

                    task_entry = {
                        "task_id": task_id,
                        "task_name": task_name,
                        "task_url": task_url,
                        "task_status": status_name,
                        "time_on_task_ms": task_time_in_range,
                        "time_on_task": format_duration_simple(task_time_in_range),
                        "intervals_count": len(task_intervals_in_range),
                    }

                    if intervals_by_date:
                        task_entry["by_date"] = intervals_by_date
                    else:
                        task_entry["time_entries"] = task_intervals_in_range

                    report[member]["tasks"].append(task_entry)

                    report[member]["summary"]["total_time_tracked_ms"] += (
                        task_time_in_range
                    )
                    report[member]["summary"]["total_tasks"] += 1
                    report[member]["summary"]["total_intervals"] += len(
                        task_intervals_in_range
                    )

            # ================================================================
            # STEP 5: Format summaries and sort tasks
            # ================================================================
            for member, data in report.items():
                data["summary"]["total_time_tracked"] = format_duration_simple(
                    data["summary"]["total_time_tracked_ms"]
                )

                data["tasks"].sort(key=lambda x: x["time_on_task_ms"], reverse=True)

            return {
                "report_type": f"{report_type}_detailed",
                "week_start": week_start,
                "week_end": week_end,
                "timezone": timezone_offset,
                "report": report,
                "total_people": len(report),
                "note": "Time entries are filtered by when they were logged, not when tasks were updated",
            }

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_weekly_time_report_detailed: {e}")
            print(traceback.format_exc())
            return {"error": str(e), "traceback": traceback.format_exc()}

    # ============================================================================
    # CONVENIENCE WRAPPER: Get task list for a specific person
    # ============================================================================

    @mcp.tool()
    def get_person_tasks_with_time(
        person_name: str,
        project: Optional[str] = None,
        space_name: Optional[str] = None,
        week_selector: Optional[str] = None,
        week_start: Optional[str] = None,
        week_end: Optional[str] = None,
    ) -> dict:
        """
        Get tasks a person worked on with timestamps (wrapper for detailed report).

        Args:
            person_name: Team member name
            project: Project name
            space_name: Space name
            week_selector: Week selector (previous, 2-weeks-ago, etc.)
            week_start, week_end: YYYY-MM-DD format

        Returns:
            Filtered report for specified person's tasks
        """
        if project:
            report_type = "team_member"
        elif space_name:
            report_type = "space"
        else:
            return {"error": "Provide either project or space_name"}

        full_report = get_weekly_time_report_detailed(
            report_type=report_type,
            project=project,
            space_name=space_name,
            week_selector=week_selector,
            week_start=week_start,
            week_end=week_end,
        )

        if "error" in full_report:
            return full_report

        person_data = full_report.get("report", {}).get(person_name)

        if not person_data:
            available_people = list(full_report.get("report", {}).keys())
            return {
                "error": f"Person '{person_name}' not found in report",
                "available_people": available_people,
                "hint": "Check the spelling or try one of the available names",
            }

        return {
            "person_name": person_name,
            "week_start": full_report["week_start"],
            "week_end": full_report["week_end"],
            "summary": person_data["summary"],
            "tasks": person_data["tasks"],
        }

    @mcp.tool()
    def get_task_status_distribution(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        include_archived: bool = True,
    ) -> dict:
        """
        Get actual task status distribution.

        Args:
            project: Project name
            list_id: Direct list ID
            include_archived: Include archived tasks

        Returns:
            Distribution of tasks across statuses
        """
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": "No context found"}

            all_tasks = _fetch_all_tasks(
                list_ids, {}, include_archived=include_archived
            )

            status_counts = {}
            category_counts = {
                "not_started": 0,
                "active": 0,
                "done": 0,
                "closed": 0,
                "other": 0,
            }

            for t in all_tasks:
                status_obj = (
                    t.get("status", {}) if isinstance(t.get("status"), dict) else {}
                )
                status_name = _extract_status_name(t)
                cat = get_status_category(status_name, status_obj.get("type"))

                status_counts[status_name] = status_counts.get(status_name, 0) + 1

                if cat in category_counts:
                    category_counts[cat] += 1
                else:
                    category_counts["other"] += 1

            return {
                "project": project or "Direct list",
                "total_tasks": len(all_tasks),
                "status_distribution": status_counts,
                "category_distribution": category_counts,
                "include_archived": include_archived,
            }

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_task_status_distribution: {e}")
            print(traceback.format_exc())
            return {"error": str(e)}

    @mcp.tool()
    def get_space_time_report_comprehensive(
        space_name: Optional[str] = None,
        space_id: Optional[str] = None,
        period_type: str = "this_week",
        custom_start: Optional[str] = None,
        custom_end: Optional[str] = None,
        rolling_days: Optional[int] = None,
        group_by: str = "assignee",
        include_archived: bool = True,
        async_job: bool = False,
        job_id: Optional[str] = None,
    ) -> dict:
        """
        Time tracking report for a SPACE with period filters.

        Args:
            space_name: Name of the ClickUp space (e.g., "JewelleryOS")
            space_id: Direct space ID
            period_type: today, yesterday, this_week, last_week, this_month, last_month, this_year, last_30_days, rolling, custom
            custom_start, custom_end: YYYY-MM-DD format for custom period
            rolling_days: Number of days (1-365) for rolling period
            group_by: assignee, folder, or status
            include_archived: Include archived tasks
            async_job: Run in background (auto-triggers at 500+ tasks)
            job_id: Internal use for background jobs

        Returns:
            Report with time tracked, estimates, task counts grouped by assignee/folder/status (or job_id if async)

        Example:
            get_space_time_report_comprehensive(space_name="JewelleryOS", period_type="this_week", group_by="assignee")
        """
        try:
            from app.mcp.status_helpers import parse_time_period_filter
            from app.clickup import fetch_all_time_entries_batch
            import sys
            import threading
            import uuid

            print(
                "⌛ Processing space report - this may take 1-3 minutes for large spaces..."
            )
            sys.stdout.flush()

            if async_job:
                jid = job_id or str(uuid.uuid4())
                JOBS[jid] = {"status": "queued", "result": None, "error": None}

                def _bg():
                    try:
                        JOBS[jid]["status"] = "running"
                        res = get_space_time_report_comprehensive(
                            space_name=space_name,
                            space_id=space_id,
                            period_type=period_type,
                            custom_start=custom_start,
                            custom_end=custom_end,
                            rolling_days=rolling_days,
                            group_by=group_by,
                            include_archived=include_archived,
                            async_job=False,
                            job_id=jid,
                        )
                        JOBS[jid]["result"] = res
                        JOBS[jid]["status"] = "finished"
                    except Exception as e:
                        JOBS[jid]["error"] = str(e)
                        JOBS[jid]["status"] = "failed"

                th = threading.Thread(target=_bg, daemon=True)
                th.start()
                return {
                    "job_id": jid,
                    "status": "started",
                    "message": "Space report running in background. Use get_weekly_time_report_status(job_id) to check status.",
                }

            if job_id:
                JOBS.setdefault(job_id, {})["status"] = "running"

            if not space_id and not space_name:
                return {"error": "Provide either space_name or space_id"}

            # Parse the time period to get date range
            try:
                start_date, end_date = parse_time_period_filter(
                    period_type=period_type,
                    custom_start=custom_start,
                    custom_end=custom_end,
                    rolling_days=rolling_days,
                )
            except ValueError as e:
                return {"error": f"Invalid period specification: {str(e)}"}

            print(
                f"[DEBUG] Period: {period_type}, Date range: {start_date} to {end_date}"
            )

            if not space_id:
                team_id = _get_team_id()
                spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
                if not spaces_data:
                    return {"error": "Failed to fetch spaces"}

                for space in spaces_data.get("spaces", []):
                    if space["name"].lower() == space_name.lower():
                        space_id = space["id"]
                        space_name = space["name"]  # Use exact name from API
                        break

                if not space_id:
                    return {"error": f"Space '{space_name}' not found"}

            # Get all lists in space
            list_ids = []

            # Folderless lists
            resp, _ = _api_call("GET", f"/space/{space_id}/list")
            if resp:
                list_ids.extend([lst["id"] for lst in resp.get("lists", [])])

            # Lists from folders
            resp2, _ = _api_call("GET", f"/space/{space_id}/folder")
            folder_map = {}  # Map list_id to folder_name for grouping
            if resp2:
                for folder in resp2.get("folders", []):
                    folder_name = folder.get("name", "Unknown Folder")
                    for lst in folder.get("lists", []):
                        list_ids.append(lst["id"])
                        folder_map[lst["id"]] = folder_name

            if not list_ids:
                return {"error": f"No lists found in space '{space_name}'"}

            print(f"[DEBUG] Found {len(list_ids)} lists in space")

            # Fetch all tasks
            all_tasks = _fetch_all_tasks(
                list_ids, {}, include_archived=include_archived
            )
            print(f"[DEBUG] Fetched {len(all_tasks)} tasks from space")

            if not all_tasks:
                return {
                    "space_name": space_name,
                    "space_id": space_id,
                    "period_type": period_type,
                    "start_date": start_date,
                    "end_date": end_date,
                    "message": "No tasks found in this space",
                    "report": {},
                }

            if len(all_tasks) >= 500 and not job_id:
                print(
                    f"⚡ AUTO-ASYNC: {len(all_tasks)} tasks detected. Switching to background mode to prevent timeout."
                )
                sys.stdout.flush()
                return get_space_time_report_comprehensive(
                    space_name=space_name,
                    space_id=space_id,
                    period_type=period_type,
                    custom_start=custom_start,
                    custom_end=custom_end,
                    rolling_days=rolling_days,
                    group_by=group_by,
                    include_archived=include_archived,
                    async_job=True,
                )

            task_ids = [t["id"] for t in all_tasks]
            print(f"[DEBUG] Fetching time entries for {len(task_ids)} tasks...")
            time_entries_map = fetch_all_time_entries_batch(task_ids)

            # Convert date range to timestamps
            start_ms, end_ms = date_range_to_timestamps(start_date, end_date)

            from app.mcp.status_helpers import (
                filter_time_entries_by_user_and_date_range,
            )

            report = {}
            metrics = _calculate_task_metrics(all_tasks)

            for task in all_tasks:
                task_id = task["id"]
                task_time_entries = time_entries_map.get(task_id, [])

                m = metrics.get(task_id, {})
                est_direct = m.get("est_direct", 0)

                if group_by == "assignee":
                    user_time_map = filter_time_entries_by_user_and_date_range(
                        task_time_entries, start_ms, end_ms
                    )

                    for username, time_tracked in user_time_map.items():
                        if username not in report:
                            report[username] = {
                                "tasks": 0,
                                "time_tracked": 0,
                                "time_estimate": 0,
                            }
                        report[username]["tasks"] += 1
                        report[username]["time_tracked"] += time_tracked
                        report[username]["time_estimate"] += est_direct

                elif group_by == "folder":
                    total_time_ms, filtered_intervals = (
                        filter_time_entries_by_date_range(
                            task_time_entries, start_ms, end_ms
                        )
                    )
                    list_id = task.get("list", {}).get("id")
                    folder_name = folder_map.get(list_id, "Folderless")

                    if total_time_ms > 0:
                        if folder_name not in report:
                            report[folder_name] = {
                                "tasks": 0,
                                "time_tracked": 0,
                                "time_estimate": 0,
                            }
                        report[folder_name]["tasks"] += 1
                        report[folder_name]["time_tracked"] += total_time_ms
                        report[folder_name]["time_estimate"] += est_direct
                else:  # status
                    total_time_ms, filtered_intervals = (
                        filter_time_entries_by_date_range(
                            task_time_entries, start_ms, end_ms
                        )
                    )
                    status_name = _extract_status_name(task)

                    if total_time_ms > 0:
                        if status_name not in report:
                            report[status_name] = {
                                "tasks": 0,
                                "time_tracked": 0,
                                "time_estimate": 0,
                                "intervals_count": 0,
                            }
                        report[status_name]["tasks"] += 1
                        report[status_name]["time_tracked"] += total_time_ms
                        report[status_name]["time_estimate"] += est_direct
                        report[status_name]["intervals_count"] += len(
                            filtered_intervals
                        )

            # Format the report with ONLY human-readable values
            formatted = {
                key: {
                    "tasks": value["tasks"],
                    "time_tracked": _format_duration(value["time_tracked"]),
                    "time_estimate": _format_duration(value["time_estimate"]),
                }
                for key, value in report.items()
            }

            total_tracked = sum(v["time_tracked"] for v in report.values())
            total_estimate = sum(v["time_estimate"] for v in report.values())

            return {
                "period": f"{start_date} to {end_date}",
                "space": space_name,
                "group_by": group_by,
                "total_time_tracked": _format_duration(total_tracked),
                "total_time_estimate": _format_duration(total_estimate),
                "note": "Only includes tasks/subtasks worked on during the date range. Estimates use task-level values to prevent double-counting.",
                "report": formatted,
            }

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_space_time_report_comprehensive: {e}")
            print(traceback.format_exc())
            return {"error": str(e)}

    @mcp.tool()
    def get_folder_time_report_comprehensive(
        folder_name: Optional[str] = None,
        folder_id: Optional[str] = None,
        space_name: Optional[str] = None,
        period_type: str = "this_week",
        custom_start: Optional[str] = None,
        custom_end: Optional[str] = None,
        rolling_days: Optional[int] = None,
        group_by: str = "assignee",
        include_archived: bool = True,
        async_job: bool = False,
        job_id: Optional[str] = None,
    ) -> dict:
        """
        Time tracking report for a FOLDER with period filters.

        Args:
            folder_name: Folder name (requires space_name)
            folder_id: Direct folder ID
            space_name: Space name (required with folder_name)
            period_type: today, yesterday, this_week, last_week, this_month, last_month, this_year, last_30_days, rolling, custom
            custom_start, custom_end: YYYY-MM-DD format for custom period
            rolling_days: Number of days (1-365) for rolling period
            group_by: assignee, list, or status
            include_archived: Include archived tasks
            async_job: Run in background (auto-triggers at 500+ tasks)
            job_id: Internal use for background jobs

        Returns:
            Report with time tracked, estimates, task counts grouped by assignee/list/status (or job_id if async)

        Example:
            get_folder_time_report_comprehensive(folder_name="Luminique", space_name="JewelleryOS", period_type="this_week")
        """
        try:
            from app.mcp.status_helpers import parse_time_period_filter
            from app.clickup import fetch_all_time_entries_batch
            import sys
            import threading
            import uuid

            print(
                "⌛ Processing folder report - this may take 1-3 minutes for large folders..."
            )
            sys.stdout.flush()

            if async_job:
                jid = job_id or str(uuid.uuid4())
                JOBS[jid] = {"status": "queued", "result": None, "error": None}

                def _bg():
                    try:
                        JOBS[jid]["status"] = "running"
                        res = get_folder_time_report_comprehensive(
                            folder_name=folder_name,
                            folder_id=folder_id,
                            space_name=space_name,
                            period_type=period_type,
                            custom_start=custom_start,
                            custom_end=custom_end,
                            rolling_days=rolling_days,
                            group_by=group_by,
                            include_archived=include_archived,
                            async_job=False,
                            job_id=jid,
                        )
                        JOBS[jid]["result"] = res
                        JOBS[jid]["status"] = "finished"
                    except Exception as e:
                        JOBS[jid]["error"] = str(e)
                        JOBS[jid]["status"] = "failed"

                th = threading.Thread(target=_bg, daemon=True)
                th.start()
                return {
                    "job_id": jid,
                    "status": "started",
                    "message": "Folder report running in background. Use get_weekly_time_report_status(job_id) to check status.",
                }

            if job_id:
                JOBS.setdefault(job_id, {})["status"] = "running"

            if not folder_id and not folder_name:
                return {"error": "Provide either folder_name or folder_id"}

            if folder_name and not space_name:
                return {"error": "space_name is required when using folder_name"}

            try:
                start_date, end_date = parse_time_period_filter(
                    period_type=period_type,
                    custom_start=custom_start,
                    custom_end=custom_end,
                    rolling_days=rolling_days,
                )
            except ValueError as e:
                return {"error": f"Invalid period specification: {str(e)}"}

            print(
                f"[DEBUG] Period: {period_type}, Date range: {start_date} to {end_date}"
            )

            if not folder_id:
                team_id = _get_team_id()
                spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
                if not spaces_data:
                    return {"error": "Failed to fetch spaces"}

                space_id = None
                for space in spaces_data.get("spaces", []):
                    if space["name"].lower() == space_name.lower():
                        space_id = space["id"]
                        break

                if not space_id:
                    return {"error": f"Space '{space_name}' not found"}

                folders_resp, _ = _api_call("GET", f"/space/{space_id}/folder")
                if not folders_resp:
                    return {"error": f"Failed to fetch folders in space '{space_name}'"}

                for folder in folders_resp.get("folders", []):
                    if folder["name"].lower() == folder_name.lower():
                        folder_id = folder["id"]
                        folder_name = folder["name"]  # Use exact name from API
                        break

                if not folder_id:
                    return {
                        "error": f"Folder '{folder_name}' not found in space '{space_name}'"
                    }

            lists_resp, _ = _api_call("GET", f"/folder/{folder_id}/list")
            if not lists_resp:
                return {"error": "Failed to fetch lists in folder"}

            list_ids = []
            list_map = {}  # Map list_id to list_name for grouping
            for lst in lists_resp.get("lists", []):
                list_id = lst["id"]
                list_ids.append(list_id)
                list_map[list_id] = lst.get("name", "Unnamed List")

            if not list_ids:
                return {
                    "error": f"No lists found in folder '{folder_name or folder_id}'"
                }

            print(f"[DEBUG] Found {len(list_ids)} lists in folder")

            all_tasks = _fetch_all_tasks(
                list_ids, {}, include_archived=include_archived
            )
            print(f"[DEBUG] Fetched {len(all_tasks)} tasks from folder")

            if not all_tasks:
                return {
                    "folder_name": folder_name or "Unknown",
                    "folder_id": folder_id,
                    "period_type": period_type,
                    "start_date": start_date,
                    "end_date": end_date,
                    "message": "No tasks found in this folder",
                    "report": {},
                }

            if len(all_tasks) >= 500 and not job_id:
                print(
                    f"⚡ AUTO-ASYNC: {len(all_tasks)} tasks detected. Switching to background mode to prevent timeout."
                )
                sys.stdout.flush()
                return get_folder_time_report_comprehensive(
                    folder_name=folder_name,
                    folder_id=folder_id,
                    space_name=space_name,
                    period_type=period_type,
                    custom_start=custom_start,
                    custom_end=custom_end,
                    rolling_days=rolling_days,
                    group_by=group_by,
                    include_archived=include_archived,
                    async_job=True,
                )

            task_ids = [t["id"] for t in all_tasks]
            print(f"[DEBUG] Fetching time entries for {len(task_ids)} tasks...")
            time_entries_map = fetch_all_time_entries_batch(task_ids)

            # Convert date range to timestamps
            start_ms, end_ms = date_range_to_timestamps(start_date, end_date)

            from app.mcp.status_helpers import (
                filter_time_entries_by_user_and_date_range,
            )

            report = {}
            metrics = _calculate_task_metrics(all_tasks)

            for task in all_tasks:
                task_id = task["id"]
                task_time_entries = time_entries_map.get(task_id, [])

                m = metrics.get(task_id, {})
                est_direct = m.get("est_direct", 0)

                if group_by == "assignee":
                    user_time_map = filter_time_entries_by_user_and_date_range(
                        task_time_entries, start_ms, end_ms
                    )

                    for username, time_tracked in user_time_map.items():
                        if username not in report:
                            report[username] = {
                                "tasks": 0,
                                "time_tracked": 0,
                                "time_estimate": 0,
                            }
                        report[username]["tasks"] += 1
                        report[username]["time_tracked"] += time_tracked
                        report[username]["time_estimate"] += est_direct

                elif group_by == "list":
                    total_time_ms, filtered_intervals = (
                        filter_time_entries_by_date_range(
                            task_time_entries, start_ms, end_ms
                        )
                    )
                    list_id = task.get("list", {}).get("id")
                    list_name = list_map.get(list_id, "Unknown List")

                    if total_time_ms > 0:
                        if list_name not in report:
                            report[list_name] = {
                                "tasks": 0,
                                "time_tracked": 0,
                                "time_estimate": 0,
                            }
                        report[list_name]["tasks"] += 1
                        report[list_name]["time_tracked"] += total_time_ms
                        report[list_name]["time_estimate"] += est_direct
                else:  # status
                    total_time_ms, filtered_intervals = (
                        filter_time_entries_by_date_range(
                            task_time_entries, start_ms, end_ms
                        )
                    )
                    status_name = _extract_status_name(task)

                    if total_time_ms > 0:
                        if status_name not in report:
                            report[status_name] = {
                                "tasks": 0,
                                "time_tracked": 0,
                                "time_estimate": 0,
                                "intervals_count": 0,
                            }
                        report[status_name]["tasks"] += 1
                        report[status_name]["time_tracked"] += total_time_ms
                        report[status_name]["time_estimate"] += est_direct
                        report[status_name]["intervals_count"] += len(
                            filtered_intervals
                        )

            # Format the report with ONLY human-readable values
            formatted = {
                key: {
                    "tasks": value["tasks"],
                    "time_tracked": _format_duration(value["time_tracked"]),
                    "time_estimate": _format_duration(value["time_estimate"]),
                }
                for key, value in report.items()
            }

            total_tracked = sum(v["time_tracked"] for v in report.values())
            total_estimate = sum(v["time_estimate"] for v in report.values())

            return {
                "period": f"{start_date} to {end_date}",
                "folder": folder_name or "Unknown",
                "space": space_name,
                "group_by": group_by,
                "total_time_tracked": _format_duration(total_tracked),
                "total_time_estimate": _format_duration(total_estimate),
                "note": "Only includes tasks/subtasks worked on during the date range. Estimates use task-level values to prevent double-counting.",
                "report": formatted,
            }

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_folder_time_report_comprehensive: {e}")
            print(traceback.format_exc())
            return {"error": str(e)}
