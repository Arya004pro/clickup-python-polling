"""
PM Analytics Module for ClickUp MCP Server - FINAL REFACTOR
Features:
1. Deep Nesting Fix: Uses 'subtasks=true' to fetch all levels flattened.
2. Centralized Math: '_calculate_task_metrics' helper powers ALL reports.
3. Estimation Accuracy: Properly implemented using bottom-up sums.
4. Robust Status Logic: Identifies 'Shipped', 'Release', etc. as DONE.
5. Complete Toolset: Includes all analytics, breakdowns, and risk assessments.
"""

from fastmcp import FastMCP
import requests
import time
from datetime import datetime, timezone
from typing import List, Optional, Dict
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
    # 1. Check Overrides (Project Specific naming conventions)
    if cat := STATUS_OVERRIDE_MAP.get(status_name.upper()):
        return cat
    # 2. Check ClickUp Internal Type
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

    # 1. Check project_map.json FIRST (handles folder/space mapped projects)
    try:
        from .sync_mapping import db

        proj_lower = project.lower().strip()

        # Check both alias and name fields
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
                    # Get all lists in this folder
                    resp, err = _api_call("GET", f"/folder/{mapped_id}/list")
                    if resp and resp.get("lists"):
                        list_ids = [lst["id"] for lst in resp["lists"]]
                        print(f"[DEBUG] Found {len(list_ids)} lists in folder")
                        return list_ids

                    # Fallback: use cached structure from mapping
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
                    # Get folderless lists
                    resp, _ = _api_call("GET", f"/space/{mapped_id}/list")
                    if resp:
                        ids.extend([lst["id"] for lst in resp.get("lists", [])])

                    # Get lists from folders
                    resp2, _ = _api_call("GET", f"/space/{mapped_id}/folder")
                    if resp2:
                        for f in resp2.get("folders", []):
                            ids.extend([lst["id"] for lst in f.get("lists", [])])

                    print(f"[DEBUG] Found {len(ids)} total lists in space")
                    return ids

    except Exception as e:
        print(f"[DEBUG] project_map.json lookup failed: {e}")

    # 2. Fall back to live API search by name
    print(f"[DEBUG] Falling back to API search for: {project}")
    team_id = _get_team_id()
    spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
    if not spaces_data:
        return []

    proj_lower = project.lower().strip()
    target_lists = []

    for space in spaces_data.get("spaces", []):
        if space["name"].lower() == proj_lower:
            # Space match - get all lists in space
            s_lists, _ = _api_call("GET", f"/space/{space['id']}/list")
            if s_lists:
                target_lists.extend([lst["id"] for lst in s_lists.get("lists", [])])
            s_folders, _ = _api_call("GET", f"/space/{space['id']}/folder")
            if s_folders:
                for f in s_folders.get("folders", []):
                    target_lists.extend([lst["id"] for lst in f.get("lists", [])])
            return target_lists

        # Folder match check
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
    if list_id:
        return [list_id]
    if not project:
        return []

    # 1. Check project_map.json FIRST (handles folder/space mapped projects)
    try:
        from .sync_mapping import db

        proj_lower = project.lower().strip()
        for alias, data in db.projects.items():
            alias_name = data.get("alias", alias)
            if alias.lower() == proj_lower or alias_name.lower() == proj_lower:
                mapped_id = data["clickup_id"]
                mapped_type = data["clickup_type"]

                if mapped_type == "list":
                    return [mapped_id]
                elif mapped_type == "folder":
                    # Get all lists in this folder from API
                    resp, _ = _api_call("GET", f"/folder/{mapped_id}/list")
                    if resp and resp.get("lists"):
                        return [lst["id"] for lst in resp["lists"]]
                    # Fallback: use cached structure from mapping
                    structure = data.get("structure", {})
                    return [
                        c["id"]
                        for c in structure.get("children", [])
                        if c.get("type") == "list"
                    ]
                elif mapped_type == "space":
                    ids = []
                    resp, _ = _api_call("GET", f"/space/{mapped_id}/list")
                    if resp:
                        ids.extend([lst["id"] for lst in resp.get("lists", [])])
                    resp2, _ = _api_call("GET", f"/space/{mapped_id}/folder")
                    if resp2:
                        for f in resp2.get("folders", []):
                            ids.extend([lst["id"] for lst in f.get("lists", [])])
                    return ids
    except ImportError:
        pass  # sync_mapping not available, fall through to API search

    # 2. Fall back to live API search by name
    team_id = _get_team_id()
    spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
    if not spaces_data:
        return []

    proj_lower = project.lower().strip()
    target_lists = []

    for space in spaces_data.get("spaces", []):
        if space["name"].lower() == proj_lower:
            # Space match - get all lists in space
            s_lists, _ = _api_call("GET", f"/space/{space['id']}/list")
            if s_lists:
                target_lists.extend([lst["id"] for lst in s_lists.get("lists", [])])
            s_folders, _ = _api_call("GET", f"/space/{space['id']}/folder")
            if s_folders:
                for f in s_folders.get("folders", []):
                    target_lists.extend([lst["id"] for lst in f.get("lists", [])])
            return target_lists

        # Folder match check
        f_data, _ = _api_call("GET", f"/space/{space['id']}/folder")
        if f_data:
            for f in f_data.get("folders", []):
                if f["name"].lower() == proj_lower:
                    return [lst["id"] for lst in f.get("lists", [])]
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
        return "0 min"
    mins = int(ms) // 60000
    return f"{mins // 60}h {mins % 60}m"


def _hours_decimal(ms):
    return round(int(ms or 0) / 3600000, 2)


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

            # Detailed Breakdown Counters
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

                # Check completion
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

                # Status Changes & Counts
                if include_status_changes:
                    if (upd := t.get("date_updated")) and int(upd) >= since_ms:
                        status_changes.append(
                            {
                                "name": t.get("name"),
                                "status": status_name,
                                "changed_at": _ms_to_readable(upd),
                            }
                        )

                    # Update metrics
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

            # Fetch ALL tasks (including archived) by default for complete reports
            all_tasks = _fetch_all_tasks(
                list_ids,
                {},  # No date filters - get everything
                include_archived=include_archived,
            )

            print(f"[DEBUG] Fetched {len(all_tasks)} total tasks")

            # Apply status filter if provided
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
                # Assignee view = Direct Time. Task view = Total (Rolled up) Time.
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

                    # Track status distribution
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

            # Fetch context to build the tree
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
        Time tracking report for an entire SPACE (all folders and lists).

        Args:
            space_name: Space name (e.g., "JewelleryOS")
            space_id: Direct space ID
            group_by: "assignee", "folder", or "status"
            include_archived: Include archived tasks

        Returns:
            Comprehensive space-level time report
        """
        try:
            # Resolve space
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

            # Get all lists in space
            list_ids = []

            # Folderless lists
            resp, _ = _api_call("GET", f"/space/{space_id}/list")
            if resp:
                list_ids.extend([lst["id"] for lst in resp.get("lists", [])])

            # Lists from folders
            resp2, _ = _api_call("GET", f"/space/{space_id}/folder")
            if resp2:
                for folder in resp2.get("folders", []):
                    list_ids.extend([lst["id"] for lst in folder.get("lists", [])])

            print(f"[DEBUG] Found {len(list_ids)} lists in space")

            # Fetch all tasks
            all_tasks = _fetch_all_tasks(
                list_ids, {}, include_archived=include_archived
            )
            print(f"[DEBUG] Fetched {len(all_tasks)} tasks from space")

            metrics = _calculate_task_metrics(all_tasks)

            # Build report
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

                # Determine grouping key
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
        Hierarchical time report: Space > Folder > Team Member breakdown.

        Args:
            space_name: Space name
            space_id: Direct space ID
            include_archived: Include archived tasks

        Returns:
            Nested report showing folder-level breakdowns with team member details
        """
        try:
            # Resolve space
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

            # Get folder structure
            folders_report = []

            # Get folders
            resp, _ = _api_call("GET", f"/space/{space_id}/folder")
            folders = resp.get("folders", []) if resp else []

            for folder in folders:
                folder_id = folder["id"]
                folder_name = folder["name"]
                list_ids = [lst["id"] for lst in folder.get("lists", [])]

                if not list_ids:
                    continue

                # Fetch tasks for this folder
                folder_tasks = _fetch_all_tasks(
                    list_ids, {}, include_archived=include_archived
                )
                metrics = _calculate_task_metrics(folder_tasks)

                # Build team member breakdown
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

                # Format team report
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

            # Handle folderless lists
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
        week_start: Optional[str] = None,  # YYYY-MM-DD format
        week_end: Optional[str] = None,  # YYYY-MM-DD format
    ) -> dict:
        """
        Weekly time tracking report wrapper.

        Args:
            report_type: "team_member", "space", or "space_folder_team"
            project: Project name (for team_member type)
            space_name: Space name (for space/space_folder_team types)
            list_id: Direct list ID
            week_start: Week start date (YYYY-MM-DD), defaults to current week Monday
            week_end: Week end date (YYYY-MM-DD), defaults to current week Sunday

        Returns:
            Weekly filtered time report based on date_updated
        """
        try:
            from datetime import datetime, timedelta

            # Calculate week boundaries if not provided
            if not week_start:
                today = datetime.now()
                monday = today - timedelta(days=today.weekday())
                week_start = monday.strftime("%Y-%m-%d")

            if not week_end:
                week_start_dt = datetime.strptime(week_start, "%Y-%m-%d")
                sunday = week_start_dt + timedelta(days=6)
                week_end = sunday.strftime("%Y-%m-%d")

            # Convert to milliseconds
            week_start_ms = int(
                datetime.strptime(week_start, "%Y-%m-%d").timestamp() * 1000
            )
            week_end_ms = int(
                datetime.strptime(week_end, "%Y-%m-%d").timestamp() * 1000
            )
            week_end_ms += 86400000  # Add 1 day to include end date

            print(f"[DEBUG] Weekly report: {week_start} to {week_end}")

            # Resolve context based on report type
            if report_type == "space":
                if not space_name:
                    return {"error": "space_name required for space report"}

                # Get space report with date filter
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

                # Get all lists in space
                list_ids = []
                resp, _ = _api_call("GET", f"/space/{space_id}/list")
                if resp:
                    list_ids.extend([lst["id"] for lst in resp.get("lists", [])])
                resp2, _ = _api_call("GET", f"/space/{space_id}/folder")
                if resp2:
                    for folder in resp2.get("folders", []):
                        list_ids.extend([lst["id"] for lst in folder.get("lists", [])])

                # Fetch tasks with date filter
                all_tasks = _fetch_all_tasks(
                    list_ids, {"date_updated_gt": week_start_ms}, include_archived=True
                )

                # Filter by end date
                all_tasks = [
                    t for t in all_tasks if int(t.get("date_updated", 0)) <= week_end_ms
                ]

                print(f"[DEBUG] Found {len(all_tasks)} tasks in date range")

                # Calculate metrics
                metrics = _calculate_task_metrics(all_tasks)

                # Build report
                report = {}
                for t in all_tasks:
                    m = metrics.get(t["id"], {})
                    val_t = m.get("tracked_direct", 0)
                    val_e = m.get("est_direct", 0)

                    if val_t == 0 and val_e == 0:
                        continue

                    assignees = [u["username"] for u in t.get("assignees", [])] or [
                        "Unassigned"
                    ]
                    for member in assignees:
                        r = report.setdefault(
                            member, {"tasks": 0, "time_tracked": 0, "time_estimate": 0}
                        )
                        r["tasks"] += 1
                        r["time_tracked"] += val_t // len(assignees)
                        r["time_estimate"] += val_e // len(assignees)

                formatted = {
                    member: {
                        **data,
                        "human_tracked": _format_duration(data["time_tracked"]),
                        "human_est": _format_duration(data["time_estimate"]),
                    }
                    for member, data in report.items()
                }

                return {
                    "report_type": "space_weekly",
                    "space_name": space_name,
                    "week_start": week_start,
                    "week_end": week_end,
                    "report": formatted,
                    "total_tasks": len(all_tasks),
                }

            elif report_type == "team_member":
                if not project and not list_id:
                    return {"error": "Provide project or list_id"}

                list_ids = _resolve_to_list_ids(project, list_id)
                if not list_ids:
                    return {"error": "No context found"}

                # Fetch tasks with date filter
                all_tasks = _fetch_all_tasks(
                    list_ids, {"date_updated_gt": week_start_ms}, include_archived=True
                )

                # Filter by end date
                all_tasks = [
                    t for t in all_tasks if int(t.get("date_updated", 0)) <= week_end_ms
                ]

                print(f"[DEBUG] Found {len(all_tasks)} tasks in date range")

                metrics = _calculate_task_metrics(all_tasks)

                report = {}
                for t in all_tasks:
                    m = metrics.get(t["id"], {})
                    val_t = m.get("tracked_direct", 0)
                    val_e = m.get("est_direct", 0)

                    if val_t == 0 and val_e == 0:
                        continue

                    assignees = [u["username"] for u in t.get("assignees", [])] or [
                        "Unassigned"
                    ]
                    for member in assignees:
                        r = report.setdefault(
                            member, {"tasks": 0, "time_tracked": 0, "time_estimate": 0}
                        )
                        r["tasks"] += 1
                        r["time_tracked"] += val_t // len(assignees)
                        r["time_estimate"] += val_e // len(assignees)

                formatted = {
                    member: {
                        **data,
                        "human_tracked": _format_duration(data["time_tracked"]),
                        "human_est": _format_duration(data["time_estimate"]),
                    }
                    for member, data in report.items()
                }

                return {
                    "report_type": "team_member_weekly",
                    "project": project or "Direct list",
                    "week_start": week_start,
                    "week_end": week_end,
                    "report": formatted,
                    "total_tasks": len(all_tasks),
                }

            else:
                return {"error": f"Invalid report_type: {report_type}"}

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_weekly_time_report: {e}")
            print(traceback.format_exc())
            return {"error": str(e)}

    @mcp.tool()
    def get_task_status_distribution(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        include_archived: bool = True,
    ) -> dict:
        """
        Get ACTUAL task status distribution (not just defined statuses).

        This shows how many tasks are actually IN each status, not just
        which statuses are defined in the list configuration.

        Args:
            project: Project name
            list_id: Direct list ID
            include_archived: Include archived tasks

        Returns:
            Actual distribution of tasks across statuses
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
