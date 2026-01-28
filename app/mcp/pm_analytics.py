"""
PM Analytics Module for ClickUp MCP Server - Optimized Version
Implements all 8 analytics tools with full spec compliance.
"""

import requests
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from fastmcp import FastMCP
from app.config import CLICKUP_API_TOKEN, BASE_URL

try:
    from app.config import CLICKUP_TEAM_ID
except ImportError:
    CLICKUP_TEAM_ID = None

# Status configuration - add new statuses here as discovered
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
    """Smart status categorization with 3-tier fallback."""
    if not status_name:
        return "other"
    # Priority 1: Custom mapping
    if cat := STATUS_TO_CATEGORY.get(status_name.upper()):
        return cat
    # Priority 2: ClickUp native type
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
            else (None, f"API Error {response.status_code}: {response.text}")
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

    target_lists = []
    proj_name = project.lower().strip()

    for space in spaces_data.get("spaces", []):
        space_id, space_name = space["id"], space.get("name", "")

        # Check if space matches project name
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

        # Check folders in space
        folders_data, _ = _api_call("GET", f"/space/{space_id}/folder")
        if folders_data:
            for f in folders_data.get("folders", []):
                if f.get("name", "").lower() == proj_name:
                    return [lst["id"] for lst in f.get("lists", []) if lst.get("id")]
    return []


def _fetch_all_tasks(list_ids: List[str], params: Dict) -> List[Dict]:
    all_tasks = []
    for l_id in list_ids:
        page = 0
        while True:
            p = {**params, "page": page}
            data, error = _api_call("GET", f"/list/{l_id}/task", params=p)
            if error or not data:
                break
            tasks = [t for t in data.get("tasks", []) if isinstance(t, dict)]
            all_tasks.extend(tasks)
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
    seconds = ms // 1000
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


def _safe_int_from_dates(task: Dict, fields: List[str]) -> int:
    """Safely extract max timestamp from date fields."""
    dates = []
    for f in fields:
        if (val := task.get(f)) is not None:
            try:
                dates.append(int(val))
            except Exception:
                pass
    return max(dates) if dates else 0


def register_pm_analytics_tools(mcp: FastMCP):
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

            tasks = _fetch_all_tasks(
                list_ids, {"date_updated_gt": since_ms, "include_closed": "true"}
            )
            completed, status_changes = [], []

            for t in tasks:
                if not isinstance(status_info := t.get("status", {}), dict):
                    continue

                status_name, status_type = (
                    status_info.get("status", ""),
                    status_info.get("type", ""),
                )
                status_category = get_status_category(status_name, status_type)

                # Check completed tasks
                if status_category in ["done", "closed"] and (
                    date_closed := t.get("date_closed")
                ):
                    try:
                        if int(date_closed) >= since_ms:
                            completed.append(
                                {
                                    "task_id": t.get("id"),
                                    "name": t.get("name"),
                                    "status": status_name,
                                    "completed_at": _ms_to_readable(date_closed),
                                }
                            )
                    except Exception:
                        pass

                # Track status changes
                if include_status_changes:
                    if (history := t.get("status_history")) and isinstance(
                        history, list
                    ):
                        for item in history:
                            if (
                                (change_date := item.get("date"))
                                and str(change_date).isdigit()
                                and int(change_date) >= since_ms
                            ):
                                status_changes.append(
                                    {
                                        "task_id": t.get("id"),
                                        "name": t.get("name"),
                                        "status": item.get("status"),
                                        "changed_at": _ms_to_readable(change_date),
                                    }
                                )
                    elif (
                        (date_updated := t.get("date_updated"))
                        and str(date_updated).isdigit()
                        and int(date_updated) >= since_ms
                    ):
                        status_changes.append(
                            {
                                "task_id": t.get("id"),
                                "name": t.get("name"),
                                "status": status_name,
                                "changed_at": _ms_to_readable(date_updated),
                            }
                        )

            return {
                "completed_tasks": completed,
                "total_completed": len(completed),
                "status_changes": status_changes if include_status_changes else None,
                "total_status_changes": len(status_changes)
                if include_status_changes
                else None,
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_time_tracking_report(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        group_by: str = "assignee",
    ) -> dict:
        """Analyze time tracked vs estimated. Supports grouping by task, assignee, or status."""
        try:
            if group_by not in {"task", "assignee", "status"}:
                return {
                    "error": f"Invalid group_by: '{group_by}'. Must be 'task', 'assignee', or 'status'."
                }

            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": f"No context found for '{project or list_id}'"}

            report = {}
            for lid in list_ids:
                for t in _fetch_all_tasks([lid], {"include_closed": "true"}):
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
                        divisor = len(assignees) if group_by == "assignee" else 1
                        report[key]["time_tracked"] += (
                            t.get("time_spent") or 0
                        ) // divisor
                        report[key]["time_estimate"] += (
                            t.get("time_estimate") or 0
                        ) // divisor

            # Add human-readable times and efficiency
            formatted = {}
            for key, val in report.items():
                efficiency = (
                    round(val["time_tracked"] / val["time_estimate"], 2)
                    if val["time_estimate"] > 0
                    else None
                )
                formatted[key] = {
                    **val,
                    "time_tracked_human": _format_duration(val["time_tracked"]),
                    "time_estimate_human": _format_duration(val["time_estimate"]),
                    "efficiency_ratio": efficiency,
                }

            return {"group_by": group_by, "report": formatted}
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

            for lid in list_ids:
                for t in _fetch_all_tasks([lid], {"include_closed": "true"}):
                    for a in t.get("assignees", []):
                        name = (
                            a.get("username") or a.get("email") or f"User_{a.get('id')}"
                        )
                        last = _safe_int_from_dates(t, ["date_updated", "date_closed"])

                        if name not in assignee_activity:
                            assignee_activity[name] = {
                                "task_count": 0,
                                "last_activity": 0,
                            }

                        if last > assignee_activity[name]["last_activity"]:
                            assignee_activity[name]["last_activity"] = last
                        assignee_activity[name]["task_count"] += 1

            inactive = [
                {
                    "assignee": k,
                    "task_count": v["task_count"],
                    "last_activity": v["last_activity"],
                    "last_activity_human": _ms_to_readable(v["last_activity"]),
                    "days_inactive": int(
                        (now - v["last_activity"]) / (1000 * 60 * 60 * 24)
                    )
                    if v["last_activity"] > 0
                    else None,
                }
                for k, v in assignee_activity.items()
                if v["last_activity"] < cutoff
            ]

            return {
                "inactive_assignees": inactive,
                "total_inactive": len(inactive),
                "inactive_threshold_days": inactive_days,
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_untracked_tasks(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        status_filter: str = "in_progress",
    ) -> dict:
        """Find tasks with no time logged. Filters: 'all', 'in_progress', 'closed'."""
        try:
            if status_filter not in {"all", "in_progress", "closed"}:
                return {
                    "error": f"Invalid status_filter: '{status_filter}'. Must be 'all', 'in_progress', or 'closed'."
                }

            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": f"No context found for '{project or list_id}'"}

            untracked, total_checked, status_breakdown = [], 0, {}
            include_closed = "true" if status_filter == "closed" else "false"

            for lid in list_ids:
                for t in _fetch_all_tasks([lid], {"include_closed": include_closed}):
                    status_info = t.get("status", {})
                    status_name, status_type = (
                        status_info.get("status", ""),
                        status_info.get("type", ""),
                    )
                    status_category = get_status_category(status_name, status_type)

                    # Apply filter
                    should_check = (
                        status_filter == "all"
                        or (
                            status_filter == "in_progress"
                            and status_category == "active"
                        )
                        or (
                            status_filter == "closed"
                            and status_category in ["done", "closed"]
                        )
                    )

                    if should_check:
                        total_checked += 1
                        if status_name not in status_breakdown:
                            status_breakdown[status_name] = {
                                "category": status_category,
                                "total": 0,
                                "untracked": 0,
                            }
                        status_breakdown[status_name]["total"] += 1

                        if not (t.get("time_spent") and t.get("time_spent") > 0):
                            status_breakdown[status_name]["untracked"] += 1
                            untracked.append(
                                {
                                    "task_id": t.get("id"),
                                    "name": t.get("name"),
                                    "status": status_name,
                                    "status_category": status_category,
                                    "assignees": [
                                        a.get("username")
                                        for a in t.get("assignees", [])
                                    ],
                                }
                            )

            compliance_rate = (
                round((total_checked - len(untracked)) / total_checked * 100, 1)
                if total_checked > 0
                else 0
            )

            return {
                "untracked_tasks": untracked,
                "total_untracked": len(untracked),
                "total_checked": total_checked,
                "compliance_rate": f"{compliance_rate}%",
                "status_filter": status_filter,
                "status_breakdown": status_breakdown,
                "note": "Statuses auto-categorize. Unknown statuses appear as 'other'.",
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_stale_tasks(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        stale_days: int = 7,
    ) -> dict:
        """Find tasks not updated in X days (forgotten or stuck work)."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": f"No context found for '{project or list_id}'"}

            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            cutoff = now - stale_days * 24 * 60 * 60 * 1000
            stale = []

            for lid in list_ids:
                for t in _fetch_all_tasks([lid], {"include_closed": "false"}):
                    last = _safe_int_from_dates(t, ["date_updated", "date_closed"])

                    if last < cutoff:
                        status_info = t.get("status", {})
                        status_name, status_type = (
                            status_info.get("status", ""),
                            status_info.get("type", ""),
                        )
                        stale.append(
                            {
                                "task_id": t.get("id"),
                                "name": t.get("name"),
                                "status": status_name,
                                "status_category": get_status_category(
                                    status_name, status_type
                                ),
                                "last_update": last,
                                "last_update_human": _ms_to_readable(last),
                                "days_stale": int((now - last) / (1000 * 60 * 60 * 24))
                                if last > 0
                                else None,
                            }
                        )

            return {
                "stale_tasks": stale,
                "total_stale": len(stale),
                "stale_threshold_days": stale_days,
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_estimation_accuracy(
        project: Optional[str] = None, list_id: Optional[str] = None
    ) -> dict:
        """Analyze estimation accuracy with recommendations and metrics."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": f"No context found for '{project or list_id}'"}
            total_est, total_spent, count = 0, 0, 0
            overestimated, underestimated, accurate = 0, 0, 0
            for lid in list_ids:
                for t in _fetch_all_tasks([lid], {"include_closed": "true"}):
                    est, spent = t.get("time_estimate") or 0, t.get("time_spent") or 0
                    if est > 0:
                        total_est += est
                        total_spent += spent
                        count += 1
                        ratio = spent / est
                        if ratio < 0.8:
                            overestimated += 1
                        elif ratio > 1.2:
                            underestimated += 1
                        else:
                            accurate += 1
            metrics = {
                "tasks_with_estimates": count,
                "total_estimate": total_est,
                "total_estimate_human": _format_duration(total_est),
                "total_spent": total_spent,
                "total_spent_human": _format_duration(total_spent),
                "task_breakdown": {
                    "accurate": accurate,
                    "under_estimated": underestimated,
                    "over_estimated": overestimated,
                },
            }
            if count == 0:
                return {
                    **metrics,
                    "message": "No tasks with time estimates found",
                    "accuracy_ratio": None,
                    "variance_percentage": None,
                    "accuracy_rating": None,
                    "recommendations": [
                        "Start adding time estimates to track accuracy"
                    ],
                }
            accuracy_ratio = round(total_spent / total_est, 2)
            variance_pct = round(((total_spent - total_est) / total_est) * 100, 1)
            # Generate rating and recommendations
            if accuracy_ratio < 0.8:
                rating = "Over-estimated"
                recs = [
                    f"Tasks taking {abs(variance_pct)}% less time than estimated",
                    "Consider reducing estimates",
                    "Teams may be overestimating or very efficient",
                ]
            elif accuracy_ratio > 1.5:
                rating = "Severely under-estimated"
                recs = [
                    f"Tasks taking {variance_pct}% MORE time than estimated",
                    "CRITICAL: Review estimation process",
                    "Break tasks into smaller units",
                    "May indicate scope creep or unforeseen complexity",
                ]
            elif accuracy_ratio > 1.2:
                rating = "Under-estimated"
                recs = [
                    f"Tasks taking {variance_pct}% more time than estimated",
                    "Increase estimates by 20-30%",
                    "Account for interruptions and context switching",
                ]
            elif 0.9 <= accuracy_ratio <= 1.1:
                rating = "Excellent"
                recs = [
                    f"Within {abs(variance_pct)}% - very accurate!",
                    "Continue current practices",
                ]
            else:
                rating = "Good"
                recs = [f"Within {abs(variance_pct)}%", "Minor adjustments may help"]
            if count >= 10:
                recs.append(
                    f"Task breakdown: {round(accurate / count * 100, 1)}% accurate, "
                    f"{round(underestimated / count * 100, 1)}% under, {round(overestimated / count * 100, 1)}% over"
                )
            return {
                **metrics,
                "accuracy_ratio": accuracy_ratio,
                "variance_percentage": variance_pct,
                "accuracy_rating": rating,
                "recommendations": recs,
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_at_risk_tasks(
        project: Optional[str] = None, list_id: Optional[str] = None, risk_days: int = 3
    ) -> dict:
        """Find overdue/at-risk tasks categorized by urgency (spec requirement)."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": f"No context found for '{project or list_id}'"}
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            overdue, at_risk = [], []
            for lid in list_ids:
                for t in _fetch_all_tasks([lid], {"include_closed": "false"}):
                    if (due := t.get("due_date")) and str(due).isdigit():
                        days_left = int((int(due) - now) / (1000 * 60 * 60 * 24))
                        status_info = t.get("status", {})
                        status_name, status_type = (
                            status_info.get("status", ""),
                            status_info.get("type", ""),
                        )
                        task_data = {
                            "task_id": t.get("id"),
                            "name": t.get("name"),
                            "status": status_name,
                            "status_category": get_status_category(
                                status_name, status_type
                            ),
                            "due_in_days": days_left,
                            "urgency": "overdue" if days_left < 0 else "at_risk",
                        }
                        if days_left < 0:
                            overdue.append(task_data)
                        elif days_left <= risk_days:
                            at_risk.append(task_data)
            return {
                "overdue": {"tasks": overdue, "count": len(overdue)},
                "at_risk": {"tasks": at_risk, "count": len(at_risk)},
                "total_at_risk": len(overdue) + len(at_risk),
                "risk_threshold_days": risk_days,
                "urgency_breakdown": f"{len(overdue)} overdue, {len(at_risk)} at-risk",
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_status_summary(
        project: Optional[str] = None, list_id: Optional[str] = None
    ) -> dict:
        """Status summary with metrics and health indicators (spec requirement)."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": f"No context found for '{project or list_id}'"}

            status_count, category_count = (
                {},
                {"not_started": 0, "active": 0, "done": 0, "closed": 0, "other": 0},
            )
            total = 0

            for lid in list_ids:
                for t in _fetch_all_tasks([lid], {"include_closed": "true"}):
                    status_info = t.get("status", {})
                    status_name, status_type = (
                        status_info.get("status", "Unknown"),
                        status_info.get("type", ""),
                    )
                    status_count[status_name] = status_count.get(status_name, 0) + 1
                    category = get_status_category(status_name, status_type)
                    category_count[category] += 1
                    total += 1

            # Health indicators
            active_pct = (
                round(category_count["active"] / total * 100, 1) if total > 0 else 0
            )
            done_pct = (
                round(category_count["done"] / total * 100, 1) if total > 0 else 0
            )
            not_started_pct = (
                round(category_count["not_started"] / total * 100, 1)
                if total > 0
                else 0
            )

            # Determine health
            if active_pct > 60:
                health = "At Risk - Too many active tasks"
            elif active_pct < 20 and not_started_pct > 50:
                health = "Needs Attention - Many tasks not started"
            elif done_pct > 50:
                health = "Good - High completion rate"
            else:
                health = "Normal - Balanced distribution"

            return {
                "total_tasks": total,
                "status_breakdown": status_count,
                "category_breakdown": category_count,
                "health_indicators": {
                    "overall_health": health,
                    "active_percentage": f"{active_pct}%",
                    "done_percentage": f"{done_pct}%",
                    "not_started_percentage": f"{not_started_pct}%",
                },
                "metrics": {
                    "completion_rate": f"{done_pct}%",
                    "work_in_progress": category_count["active"],
                    "backlog_size": category_count["not_started"],
                },
                "note": "Categories auto-adapt. Unknown statuses appear as 'other'.",
            }
        except Exception as e:
            return {"error": str(e)}
