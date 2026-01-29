"""
PM Analytics Module for ClickUp MCP Server - FINAL REFACTOR
Features:
1. Deep Nesting Fix: Uses 'subtasks=true' to fetch all levels flattened.
2. Centralized Math: '_calculate_task_metrics' helper powers ALL reports.
3. Estimation Accuracy: Properly implemented using bottom-up sums to avoid double counting.
"""

from fastmcp import FastMCP
import requests
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from app.config import CLICKUP_API_TOKEN, BASE_URL

try:
    from app.config import CLICKUP_TEAM_ID
except ImportError:
    CLICKUP_TEAM_ID = None

# Status configuration
STATUS_CATEGORIES = {
    "not_started": ["BACKLOG", "QUEUED", "TO DO", "IN PLANNING"],
    "active": [
        "SCOPING",
        "IN DESIGN",
        "IN DEVELOPMENT",
        "IN PROGRESS",
        "IN REVIEW",
        "TESTING",
        "BUG",
        "READY FOR DEVELOPMENT",
        "READY FOR PRODUCTION",
        "STAGING DEPLOY",
    ],
    "done": ["SHIPPED", "RELEASE", "QC CHECK", "COMPLETE"],
    "closed": ["CANCELLED"],
}

STATUS_TO_CATEGORY = {
    s.upper(): cat for cat, statuses in STATUS_CATEGORIES.items() for s in statuses
}


def get_status_category(status_name: str, status_type: str = None) -> str:
    if not status_name:
        return "other"
    if cat := STATUS_TO_CATEGORY.get(status_name.upper()):
        return cat
    if status_type:
        type_map = {"closed": "closed", "open": "not_started", "custom": "active"}
        if cat := type_map.get(status_type.lower()):
            return cat
    return "other"


def _headers() -> Dict[str, str]:
    return {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}


def _api_call(
    method: str,
    endpoint: str,
    params: Optional[Dict] = None,
    payload: Optional[Dict] = None,
):
    url = f"{BASE_URL}{endpoint}"
    try:
        response = (
            requests.get(url, headers=_headers(), params=params)
            if method.upper() == "GET"
            else requests.post(url, headers=_headers(), json=payload)
        )
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
    if list_id:
        return [list_id]
    if not project:
        return []

    team_id = _get_team_id()
    spaces_data, error = _api_call("GET", f"/team/{team_id}/space")
    if error or not spaces_data:
        return []

    target_lists, proj_name = [], project.lower().strip()

    for space in spaces_data.get("spaces", []):
        space_id, space_name = space["id"], space.get("name", "")
        if space_name.lower() == proj_name:
            lists_data, _ = _api_call("GET", f"/space/{space_id}/list")
            if lists_data:
                target_lists.extend(
                    [lst["id"] for lst in lists_data.get("lists", []) if lst.get("id")]
                )
            folders_data, _ = _api_call("GET", f"/space/{space_id}/folder")
            if folders_data:
                for f in folders_data.get("folders", []):
                    target_lists.extend(
                        [lst["id"] for lst in f.get("lists", []) if lst.get("id")]
                    )
            return target_lists

        folders_data, _ = _api_call("GET", f"/space/{space_id}/folder")
        if folders_data:
            for f in folders_data.get("folders", []):
                if f.get("name", "").lower() == proj_name:
                    return [lst["id"] for lst in f.get("lists", []) if lst.get("id")]
    return []


def _fetch_all_tasks(list_ids: List[str], base_params: Dict) -> List[Dict]:
    """
    Fetch ALL tasks including deeply nested subtasks using the flattened API approach.
    Fetches both Open and Archived tasks to ensure complete time history.
    """
    all_tasks = []
    seen_ids = set()

    # We must fetch twice: once for active tasks, once for archived (closed) tasks
    flags = [False, True]

    for list_id in list_ids:
        for is_archived in flags:
            page = 0
            while True:
                # Params: subtasks=true forces ClickUp to return nested tasks in the main list
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


def _ms_to_readable(ms: Any) -> str:
    try:
        return (
            datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            if ms
            else "N/A"
        )
    except Exception:
        return "N/A"


def _format_duration(ms: int) -> str:
    if not ms:
        return "0 min"
    total_seconds = int(ms) // 1000
    minutes = (total_seconds // 60) % 60
    hours = (total_seconds // 3600) % 24
    days = total_seconds // (24 * 3600)

    if days > 0:
        return f"{days} d {hours} hr {minutes} min"
    if hours > 0:
        return f"{hours} hr {minutes} min"
    return f"{minutes} min"


def _hours_decimal(ms: int) -> float:
    """Return duration in decimal hours (rounded to 2 decimals)."""
    return round((int(ms or 0) / 3600000), 2)


def _safe_int_from_dates(task: Dict, fields: List[str]) -> int:
    dates = []
    for f in fields:
        if (val := task.get(f)) is not None:
            try:
                dates.append(int(val))
            except Exception:
                pass
    return max(dates) if dates else 0


def _calculate_task_metrics(all_tasks: List[Dict]) -> Dict[str, Dict[str, int]]:
    """
    CORE HELPER: Robust Bottom-Up Calculation.
    Builds a map of accurate time metrics for ALL tasks.
    Returns: { task_id: { 'tracked_total': int, 'tracked_direct': int, 'est_total': int, 'est_direct': int } }
    """
    task_map = {t["id"]: t for t in all_tasks}

    # Build adjacency list (Parent -> Children)
    children_map = {}
    for t in all_tasks:
        pid = t.get("parent")
        if pid:
            if pid not in children_map:
                children_map[pid] = []
            children_map[pid].append(t["id"])

    cache = {}

    def get_values(tid):
        if tid in cache:
            return cache[tid]

        task_obj = task_map.get(tid, {})
        # Safe fallback if task exists in parent ref but not in fetched list
        if not task_obj:
            return (0, 0, 0, 0)

        # Raw API values
        api_tracked = int(task_obj.get("time_spent") or 0)
        api_est = int(task_obj.get("time_estimate") or 0)

        # Recursively sum children
        sum_child_total_tracked = 0
        sum_child_total_est = 0

        for cid in children_map.get(tid, []):
            c_track, _, c_est, _ = get_values(cid)
            sum_child_total_tracked += c_track
            sum_child_total_est += c_est

        # --- Calculate Direct Tracked ---
        # If API Time > Children Sum, the difference is Direct Time.
        # If API Time <= Children Sum, we assume API is returning a rollup and Direct is 0.
        if api_tracked < sum_child_total_tracked:
            direct_tracked = api_tracked
        else:
            direct_tracked = api_tracked - sum_child_total_tracked
        direct_tracked = max(0, direct_tracked)

        # --- Calculate Direct Estimate ---
        # Same logic ensures we don't double count estimates in rollups
        if api_est < sum_child_total_est:
            direct_est = api_est
        else:
            direct_est = api_est - sum_child_total_est
        direct_est = max(0, direct_est)

        # True Rollup (Calculated fresh to ensure accuracy)
        true_total_tracked = direct_tracked + sum_child_total_tracked
        true_total_est = direct_est + sum_child_total_est

        result = (true_total_tracked, direct_tracked, true_total_est, direct_est)
        cache[tid] = result
        return result

    # Compute for all tasks
    for tid in task_map:
        get_values(tid)

    # Convert tuple to dict
    final_map = {}
    for tid, res in cache.items():
        final_map[tid] = {
            "tracked_total": res[0],
            "tracked_direct": res[1],
            "est_total": res[2],
            "est_direct": res[3],
        }
    return final_map


def register_pm_analytics_tools(mcp: FastMCP):
    @mcp.tool()
    def get_time_tracking_report(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        group_by: str = "assignee",
    ) -> dict:
        """
        Analyze time tracked vs estimated.
        Uses bottom-up logic to calculate DIRECT time per task, ensuring project totals are accurate.
        """
        try:
            if group_by not in {"task", "assignee", "status"}:
                return {
                    "error": f"Invalid group_by: '{group_by}'. Must be 'task', 'assignee', or 'status'."
                }

            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": f"No context found for '{project or list_id}'"}

            # 1. Fetch EVERYTHING (Active + Closed, Nested)
            all_tasks = _fetch_all_tasks(list_ids, {})

            # 2. Run calculation engine
            metrics_map = _calculate_task_metrics(all_tasks)

            report = {}
            total_tasks_with_time = 0

            for t in all_tasks:
                metrics = metrics_map.get(t["id"], {})

                # Choose which metric to use based on grouping:
                # - For 'assignee' grouping we show direct (own) time so individual's logged time is clear
                # - For 'task' or 'status' grouping we show rolled-up totals (own + subtasks)
                if group_by == "assignee":
                    time_val = metrics.get("tracked_direct", 0)
                    est_val = metrics.get("est_direct", 0)
                else:
                    time_val = metrics.get("tracked_total", 0)
                    est_val = metrics.get("est_total", 0)

                # Skip if no activity and no estimate
                if time_val == 0 and est_val == 0:
                    continue

                total_tasks_with_time += 1 if time_val > 0 else 0

                assignees = [
                    a.get("username", "Unassigned") for a in t.get("assignees", [])
                ] or ["Unassigned"]

                if group_by == "task":
                    keys = [t.get("name") or "Untitled Task"]
                elif group_by == "status":
                    keys = [t.get("status", {}).get("status") or "Unknown"]
                else:  # assignee
                    keys = assignees

                for key in keys:
                    if key not in report:
                        report[key] = {
                            "tasks": 0,
                            "time_tracked": 0,
                            "time_estimate": 0,
                        }

                    report[key]["tasks"] += 1

                    # Distribute proportionally if multiple assignees
                    divisor = len(assignees) if group_by == "assignee" else 1
                    report[key]["time_tracked"] += time_val // divisor
                    report[key]["time_estimate"] += est_val // divisor

            # Format output
            formatted = {}
            for key, val in report.items():
                if val["time_estimate"] > 0:
                    eff_ratio = val["time_tracked"] / val["time_estimate"]
                    eff_pct = round(eff_ratio * 100, 1)
                    eff_ratio_disp = round(eff_ratio, 2)
                else:
                    eff_ratio = None
                    eff_pct = None
                    eff_ratio_disp = None

                formatted[key] = {
                    **val,
                    "time_tracked_human": _format_duration(val["time_tracked"]),
                    "time_tracked_hours": _hours_decimal(val["time_tracked"]),
                    "time_estimate_human": _format_duration(val["time_estimate"]),
                    "time_estimate_hours": _hours_decimal(val["time_estimate"]),
                    "estimate_utilization": f"{eff_ratio_disp}x"
                    if eff_ratio_disp is not None
                    else "N/A",
                    "estimate_utilization_pct": f"{eff_pct}%"
                    if eff_pct is not None
                    else "N/A",
                }

            return {
                "group_by": group_by,
                "report": formatted,
                "meta": {
                    "total_tasks_scanned": len(all_tasks),
                    "tasks_with_activity": total_tasks_with_time,
                    "note": "When grouping by 'assignee', total tracked time of each assignee for that task is displayed.",
                },
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_task_time_breakdown(task_id: str) -> dict:
        """
        Get a detailed time breakdown (TRACKED vs ESTIMATED) for a specific task hierarchy.
        Shows the 'True Rollup' calculated bottom-up using the shared metrics helper.
        """
        try:
            # 1. Fetch task context
            root_task, err = _api_call("GET", f"/task/{task_id}")
            if err:
                return {"error": f"Could not fetch task {task_id}: {err}"}

            # 2. Fetch all tasks in list
            list_id = root_task["list"]["id"]
            all_list_tasks = _fetch_all_tasks([list_id], {})

            # 3. Calculate metrics using shared helper
            metrics_map = _calculate_task_metrics(all_list_tasks)

            # 4. Build Tree View
            task_map = {t["id"]: t for t in all_list_tasks}
            children_map = {}
            for t in all_list_tasks:
                pid = t.get("parent")
                if pid:
                    if pid not in children_map:
                        children_map[pid] = []
                    children_map[pid].append(t["id"])

            tree_view = []

            def build_tree_view(tid, depth=0):
                t = task_map.get(tid)
                if not t:
                    return

                m = metrics_map.get(tid, {})
                r_track = m.get("tracked_total", 0)
                d_track = m.get("tracked_direct", 0)
                r_est = m.get("est_total", 0)

                indent = "  " * depth

                # Only show interesting rows
                if r_track > 0 or r_est > 0 or tid in children_map:
                    if r_est > 0:
                        row_ratio = r_track / r_est
                        row_pct = round(row_ratio * 100, 1)
                        row_ratio_disp = round(row_ratio, 2)
                    else:
                        row_ratio = None
                        row_pct = None
                        row_ratio_disp = None

                    tree_view.append(
                        {
                            "task": f"{indent}{t.get('name')}",
                            "status": t.get("status", {}).get("status"),
                            "tracked_direct": _format_duration(d_track),
                            "tracked_direct_hours": _hours_decimal(d_track),
                            "tracked_total": _format_duration(r_track),
                            "tracked_total_hours": _hours_decimal(r_track),
                            "estimated_total": _format_duration(r_est),
                            "estimated_total_hours": _hours_decimal(r_est),
                            "estimate_utilization": f"{row_ratio_disp}x"
                            if row_ratio_disp is not None
                            else "-",
                            "estimate_utilization_pct": f"{row_pct}%"
                            if row_pct is not None
                            else "-",
                            "assignees": [
                                u["username"] for u in t.get("assignees", [])
                            ],
                        }
                    )

                for cid in children_map.get(tid, []):
                    build_tree_view(cid, depth + 1)

            build_tree_view(task_id)

            root_m = metrics_map.get(task_id, {})
            root_total_tracked = root_m.get("tracked_total", 0)
            root_total_est = root_m.get("est_total", 0)
            if root_total_est > 0:
                root_ratio = root_total_tracked / root_total_est
                root_pct = round(root_ratio * 100, 1)
                root_ratio_disp = round(root_ratio, 2)
            else:
                root_ratio = None
                root_pct = None
                root_ratio_disp = None

            root_eff = {
                "estimate_utilization": f"{root_ratio_disp}x"
                if root_ratio_disp is not None
                else "N/A",
                "estimate_utilization_pct": f"{root_pct}%"
                if root_pct is not None
                else "N/A",
            }

            return {
                "root_task": root_task.get("name"),
                "totals": {
                    "time_tracked": _format_duration(root_total_tracked),
                    "time_tracked_hours": _hours_decimal(root_total_tracked),
                    "time_estimated": _format_duration(root_total_est),
                    "time_estimated_hours": _hours_decimal(root_total_est),
                    **root_eff,
                },
                "breakdown_tree": tree_view,
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_estimation_accuracy(
        project: Optional[str] = None, list_id: Optional[str] = None
    ) -> dict:
        """
        Analyze estimation accuracy.
        Includes 'Unestimated Work' to ensure Total Spent matches the full project time.
        """
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": f"No context found for '{project or list_id}'"}

            all_tasks = _fetch_all_tasks(list_ids, {})
            metrics_map = _calculate_task_metrics(all_tasks)

            # Buckets
            est_buckets = {"total_est": 0, "spent_on_est": 0}
            unest_buckets = {"spent_on_unest": 0, "tasks": 0}

            # Accuracy Counters
            over, under, accurate = 0, 0, 0
            tasks_analyzed = 0

            for t in all_tasks:
                m = metrics_map.get(t["id"], {})
                direct_time = m.get("tracked_direct", 0)
                direct_est = m.get("est_direct", 0)

                # Scenario A: Task has an Estimate (We can measure accuracy)
                if direct_est > 0:
                    est_buckets["total_est"] += direct_est
                    est_buckets["spent_on_est"] += direct_time
                    tasks_analyzed += 1

                    if direct_time > 0:
                        ratio = direct_time / direct_est
                        if ratio < 0.8:
                            over += 1
                        elif ratio > 1.2:
                            under += 1
                        else:
                            accurate += 1
                    else:
                        # Estimate exists but 0 time spent (Over-estimated / Not Started)
                        over += 1

                # Scenario B: Task has Time but NO Estimate (Unplanned Work)
                elif direct_time > 0:
                    unest_buckets["spent_on_unest"] += direct_time
                    unest_buckets["tasks"] += 1

            # Totals
            total_project_spent = (
                est_buckets["spent_on_est"] + unest_buckets["spent_on_unest"]
            )

            if est_buckets["total_est"] == 0:
                return {"message": "No tasks with time estimates found"}

            # Calculate Accuracy Metrics only on the estimated portion
            accuracy_ratio = round(
                est_buckets["spent_on_est"] / est_buckets["total_est"], 2
            )
            variance_pct = round(
                (
                    (est_buckets["spent_on_est"] - est_buckets["total_est"])
                    / est_buckets["total_est"]
                )
                * 100,
                1,
            )

            if accuracy_ratio < 0.8:
                rating = "Over-estimated (Under-utilized)"
            elif accuracy_ratio > 1.5:
                rating = "Severely under-estimated"
            elif accuracy_ratio > 1.2:
                rating = "Under-estimated"
            else:
                rating = "Good"

            return {
                "tasks_analyzed": tasks_analyzed,
                "project_totals": {
                    "total_estimate": _format_duration(est_buckets["total_est"]),
                    "total_spent_all": _format_duration(total_project_spent),
                    "spent_on_estimated_tasks": _format_duration(
                        est_buckets["spent_on_est"]
                    ),
                    "spent_on_unestimated_tasks": _format_duration(
                        unest_buckets["spent_on_unest"]
                    ),
                },
                "accuracy_metrics": {
                    "ratio": accuracy_ratio,
                    "variance": f"{variance_pct}%",
                    "rating": rating,
                },
                "breakdown": {"accurate": accurate, "under": under, "over": over},
                "unestimated_items": unest_buckets["tasks"],
                "note": "Total includes work on unestimated tasks.",
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_untracked_tasks(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        status_filter: str = "in_progress",
    ) -> dict:
        """Find tasks with no time logged (Direct Time = 0)."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": f"No context found for '{project or list_id}'"}

            all_tasks = _fetch_all_tasks(list_ids, {})
            metrics_map = _calculate_task_metrics(all_tasks)

            untracked = []

            for t in all_tasks:
                status_info = t.get("status", {})
                status_cat = get_status_category(
                    status_info.get("status", ""), status_info.get("type", "")
                )

                should_check = (
                    (status_filter == "all")
                    or (status_filter == "in_progress" and status_cat == "active")
                    or (status_filter == "closed" and status_cat in ["done", "closed"])
                )

                if should_check:
                    m = metrics_map.get(t["id"], {})
                    # We check if DIRECT time is 0.
                    # If a parent has 0 direct time but children have time, it is NOT untracked (it's just a container).
                    if m.get("tracked_direct", 0) == 0:
                        untracked.append(
                            {
                                "name": t.get("name"),
                                "status": status_info.get("status"),
                                "assignees": [
                                    a.get("username") for a in t.get("assignees", [])
                                ],
                            }
                        )

            return {"untracked_tasks": untracked, "total_untracked": len(untracked)}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_progress_since(
        since_date: str,
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        include_status_changes: bool = True,
    ) -> dict:
        """Get tasks completed or status changed since a given date."""
        try:
            if "T" not in since_date:
                since_date += "T00:00:00Z"
            since_ms = int(
                datetime.fromisoformat(since_date.replace("Z", "+00:00")).timestamp()
                * 1000
            )

            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": f"No context found for '{project or list_id}'"}

            tasks = _fetch_all_tasks(list_ids, {"date_updated_gt": since_ms})
            completed, status_changes = [], []

            for t in tasks:
                if not isinstance(status_info := t.get("status", {}), dict):
                    continue

                status_name = status_info.get("status", "")
                status_cat = get_status_category(
                    status_name, status_info.get("type", "")
                )

                if status_cat in ["done", "closed"] and (
                    date_closed := t.get("date_closed")
                ):
                    try:
                        if int(date_closed) >= since_ms:
                            completed.append(
                                {
                                    "name": t.get("name"),
                                    "status": status_name,
                                    "completed_at": _ms_to_readable(date_closed),
                                }
                            )
                    except Exception:
                        pass

                if include_status_changes:
                    if (date_updated := t.get("date_updated")) and int(
                        date_updated
                    ) >= since_ms:
                        status_changes.append(
                            {
                                "name": t.get("name"),
                                "status": status_name,
                                "changed_at": _ms_to_readable(date_updated),
                            }
                        )

            return {
                "completed_tasks": completed,
                "total_completed": len(completed),
                "status_changes": status_changes if include_status_changes else None,
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_inactive_assignees(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        inactive_days: int = 3,
    ) -> dict:
        """Find team members with assigned tasks but no recent activity."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": f"No context found for '{project or list_id}'"}

            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            cutoff = now - inactive_days * 24 * 60 * 60 * 1000
            assignee_activity = {}

            tasks = _fetch_all_tasks(list_ids, {})

            for t in tasks:
                for a in t.get("assignees", []):
                    name = a.get("username") or a.get("email") or f"User_{a.get('id')}"
                    last = _safe_int_from_dates(t, ["date_updated", "date_closed"])

                    if name not in assignee_activity:
                        assignee_activity[name] = {"task_count": 0, "last_activity": 0}

                    if last > assignee_activity[name]["last_activity"]:
                        assignee_activity[name]["last_activity"] = last

                    status_cat = get_status_category(
                        t.get("status", {}).get("status", "")
                    )
                    if status_cat == "active":
                        assignee_activity[name]["task_count"] += 1

            inactive = [
                {
                    "assignee": k,
                    "task_count": v["task_count"],
                    "days_inactive": int(
                        (now - v["last_activity"]) / (1000 * 60 * 60 * 24)
                    )
                    if v["last_activity"] > 0
                    else None,
                }
                for k, v in assignee_activity.items()
                if v["last_activity"] < cutoff and v["task_count"] > 0
            ]

            return {"inactive_assignees": inactive, "total_inactive": len(inactive)}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_stale_tasks(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        stale_days: int = 7,
    ) -> dict:
        """Find tasks not updated in X days."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": f"No context found for '{project or list_id}'"}

            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            cutoff = now - stale_days * 24 * 60 * 60 * 1000
            stale = []

            all_tasks = _fetch_all_tasks(list_ids, {})

            for t in all_tasks:
                status_cat = get_status_category(t.get("status", {}).get("status", ""))
                if status_cat not in ["closed", "done"]:
                    last = _safe_int_from_dates(t, ["date_updated", "date_created"])
                    if last < cutoff and last > 0:
                        stale.append(
                            {
                                "name": t.get("name"),
                                "status": t.get("status", {}).get("status"),
                                "days_stale": int((now - last) / (1000 * 60 * 60 * 24)),
                            }
                        )

            return {"stale_tasks": stale, "total_stale": len(stale)}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_status_summary(
        project: Optional[str] = None, list_id: Optional[str] = None
    ) -> dict:
        """Status summary with metrics."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": f"No context found for '{project or list_id}'"}

            status_count, category_count = (
                {},
                {"not_started": 0, "active": 0, "done": 0, "closed": 0, "other": 0},
            )
            total = 0

            all_tasks = _fetch_all_tasks(list_ids, {})

            for t in all_tasks:
                status_info = t.get("status", {})
                status_name = status_info.get("status", "Unknown")
                status_count[status_name] = status_count.get(status_name, 0) + 1

                cat = get_status_category(status_name, status_info.get("type", ""))
                if cat in category_count:
                    category_count[cat] += 1
                else:
                    category_count["other"] += 1
                total += 1

            return {
                "total_tasks": total,
                "status_breakdown": status_count,
                "category_breakdown": category_count,
            }
        except Exception as e:
            return {"error": str(e)}
