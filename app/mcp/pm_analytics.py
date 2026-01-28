"""
PM Analytics Module for ClickUp MCP Server.

Implements all analytics tools as per ClickUp MCP documentation:
- get_progress_since
- get_time_tracking_report
- get_inactive_assignees
- get_untracked_tasks
- get_stale_tasks
- get_estimation_accuracy
- get_at_risk_tasks
- get_status_summary
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


# -----------------------------------------------------------------------------
# API Helpers
# -----------------------------------------------------------------------------
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
        if method.upper() == "GET":
            response = requests.get(url, headers=_headers(), params=params)
        else:
            response = requests.post(url, headers=_headers(), json=payload)
        if response.status_code == 200:
            return response.json(), None
        return None, f"API Error {response.status_code}: {response.text}"
    except Exception as e:
        return None, str(e)


def _get_team_id() -> str:
    if CLICKUP_TEAM_ID:
        return CLICKUP_TEAM_ID
    data, _ = _api_call("GET", "/team")
    if data and "teams" in data and len(data["teams"]) > 0:
        return data["teams"][0]["id"]
    return "0"


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
        space_id = space.get("id")
        if space.get("name", "").lower() == proj_name:
            lists_data, _ = _api_call("GET", f"/space/{space_id}/list")
            if lists_data:
                target_lists.extend(
                    [
                        lst.get("id")
                        for lst in lists_data.get("lists", [])
                        if lst.get("id")
                    ]
                )
            folders_data, _ = _api_call("GET", f"/space/{space_id}/folder")
            if folders_data:
                for f in folders_data.get("folders", []):
                    target_lists.extend(
                        [lst.get("id") for lst in f.get("lists", []) if lst.get("id")]
                    )
            return target_lists
        folders_data, _ = _api_call("GET", f"/space/{space_id}/folder")
        if folders_data:
            for f in folders_data.get("folders", []):
                if f.get("name", "").lower() == proj_name:
                    return [
                        lst.get("id") for lst in f.get("lists", []) if lst.get("id")
                    ]
    return []


def _fetch_all_tasks(list_ids: List[str], params: Dict) -> List[Dict]:
    all_tasks = []
    for l_id in list_ids:
        page = 0
        while True:
            p = params.copy()
            p["page"] = page
            data, error = _api_call("GET", f"/list/{l_id}/task", params=p)
            if error or not data:
                break
            tasks = data.get("tasks", [])
            valid_tasks = [t for t in tasks if isinstance(t, dict)]
            all_tasks.extend(valid_tasks)
            if len(tasks) < 100:
                break
            page += 1
    return all_tasks


def _ms_to_readable(ms: Any) -> str:
    if not ms:
        return "N/A"
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
    except Exception:
        return "N/A"


def _format_duration(ms: int) -> str:
    if ms is None or ms == 0:
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


# -----------------------------------------------------------------------------
# Tool Registration
# -----------------------------------------------------------------------------
def register_pm_analytics_tools(mcp: FastMCP):
    @mcp.tool()
    def get_progress_since(
        since_date: str,
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        include_status_changes: bool = True,
    ) -> dict:
        """
        Get tasks completed or status changed since a given date.
        """
        try:
            if "T" not in since_date:
                since_date += "T00:00:00Z"
            dt = datetime.fromisoformat(since_date.replace("Z", "+00:00"))
            since_ms = int(dt.timestamp() * 1000)
            list_ids = _resolve_to_list_ids(project, list_id)
            if not list_ids:
                return {"error": f"No context found for '{project or list_id}'"}
            tasks = _fetch_all_tasks(
                list_ids, {"date_updated_gt": since_ms, "include_closed": "true"}
            )
            completed = []
            status_changes = []
            for t in tasks:
                status_info = t.get("status", {})
                if not isinstance(status_info, dict):
                    continue
                status_type = status_info.get("type", "").lower()
                if status_type in ["closed", "done"]:
                    completed.append(
                        {
                            "task_id": t.get("id"),
                            "name": t.get("name"),
                            "completed_at": _ms_to_readable(t.get("date_updated")),
                        }
                    )
                if include_status_changes:
                    for field in ["date_updated", "date_closed"]:
                        ts = t.get(field)
                        if ts and str(ts).isdigit() and int(ts) >= since_ms:
                            status_changes.append(
                                {
                                    "task_id": t.get("id"),
                                    "name": t.get("name"),
                                    "status": status_info.get("status"),
                                    "changed_at": _ms_to_readable(ts),
                                }
                            )
                            break
            return {
                "completed_tasks": completed,
                "status_changes": status_changes if include_status_changes else None,
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_time_tracking_report(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        group_by: str = "assignee",
    ) -> dict:
        """
        Analyze time tracked vs estimated for tasks and team members.
        group_by must be one of: 'task', 'assignee', 'status'.
        """
        try:
            allowed = {"task", "assignee", "status"}
            if group_by not in allowed:
                return {
                    "error": f"Invalid group_by: '{group_by}'. Must be one of {sorted(allowed)}."
                }
            list_ids = _resolve_to_list_ids(project, list_id)
            if not list_ids:
                return {"error": f"No context found for '{project or list_id}'"}
            report = {}
            for lid in list_ids:
                tasks = _fetch_all_tasks([lid], {"include_closed": "true"})
                for t in tasks:
                    assignees = tuple(
                        a.get("username", "Unassigned") for a in t.get("assignees", [])
                    ) or ("Unassigned",)
                    if group_by == "task":
                        key = t.get("name")
                    elif group_by == "status":
                        key = t.get("status", {}).get("status")
                    else:  # group_by == "assignee"
                        key = assignees
                    if key not in report:
                        report[key] = {
                            "tasks": 0,
                            "time_tracked": 0,
                            "time_estimate": 0,
                        }
                    report[key]["tasks"] += 1
                    report[key]["time_tracked"] += t.get("time_spent") or 0
                    report[key]["time_estimate"] += t.get("time_estimate") or 0
            # Format time fields for readability
            formatted_report = {}
            for key, val in report.items():
                formatted_report[key] = {
                    **val,
                    "time_tracked_human": _format_duration(val["time_tracked"]),
                    "time_estimate_human": _format_duration(val["time_estimate"]),
                }
            return {"group_by": group_by, "report": formatted_report}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_inactive_assignees(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        inactive_days: int = 3,
    ) -> dict:
        """
        Find team members with assigned tasks but no recent activity.
        """
        try:
            list_ids = _resolve_to_list_ids(project, list_id)
            if not list_ids:
                return {"error": f"No context found for '{project or list_id}'"}
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            cutoff = now - inactive_days * 24 * 60 * 60 * 1000
            assignee_activity = {}
            for lid in list_ids:
                tasks = _fetch_all_tasks([lid], {"include_closed": "true"})
                for t in tasks:
                    for a in t.get("assignees", []):
                        name = (
                            a.get("username") or a.get("email") or f"User_{a.get('id')}"
                        )
                        # Safely get date fields as integers
                        last_dates = []
                        for f in ["date_updated", "date_closed"]:
                            val = t.get(f)
                            if val is not None:
                                try:
                                    intval = int(val)
                                    last_dates.append(intval)
                                except Exception:
                                    continue
                        last = max(last_dates) if last_dates else 0
                        if (
                            name not in assignee_activity
                            or last > assignee_activity[name]["last_activity"]
                        ):
                            assignee_activity[name] = {
                                "task_count": 0,
                                "last_activity": last,
                            }
                        assignee_activity[name]["task_count"] += 1
            inactive = [
                {
                    "assignee": k,
                    "task_count": v["task_count"],
                    "last_activity": v["last_activity"],
                    "last_activity_human": _ms_to_readable(v["last_activity"]),
                }
                for k, v in assignee_activity.items()
                if v["last_activity"] < cutoff
            ]
            return {"inactive_assignees": inactive}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_untracked_tasks(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        status_filter: str = "in_progress",
    ) -> dict:
        """
        Find tasks with no time logged (time tracking compliance).
        """
        try:
            list_ids = _resolve_to_list_ids(project, list_id)
            if not list_ids:
                return {"error": f"No context found for '{project or list_id}'"}
            untracked = []
            for lid in list_ids:
                tasks = _fetch_all_tasks([lid], {"include_closed": "false"})
                for t in tasks:
                    status = t.get("status", {}).get("status", "")
                    # Only consider as tracked if time_spent > 0, else untracked
                    if (status_filter == "all" or status == status_filter):
                        time_spent = t.get("time_spent")
                        if not (time_spent > 0):
                            untracked.append(
                                {
                                    "task_id": t.get("id"),
                                    "name": t.get("name"),
                                    "status": status,
                                }
                            )
            return {"untracked_tasks": untracked, "total_untracked": len(untracked)}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_stale_tasks(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        stale_days: int = 7,
    ) -> dict:
        """
        Find tasks not updated in X days (forgotten or stuck work).
        """
        try:
            list_ids = _resolve_to_list_ids(project, list_id)
            if not list_ids:
                return {"error": f"No context found for '{project or list_id}'"}
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            cutoff = now - stale_days * 24 * 60 * 60 * 1000
            stale = []
            for lid in list_ids:
                tasks = _fetch_all_tasks([lid], {"include_closed": "false"})
                for t in tasks:
                    last = max(
                        [
                            t.get(f)
                            for f in ["date_updated", "date_closed"]
                            if t.get(f) and str(t.get(f)).isdigit()
                        ]
                        or [0]
                    )
                    if last < cutoff:
                        stale.append(
                            {
                                "task_id": t.get("id"),
                                "name": t.get("name"),
                                "last_update": last,
                            }
                        )
            return {"stale_tasks": stale, "total_stale": len(stale)}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_estimation_accuracy(
        project: Optional[str] = None, list_id: Optional[str] = None
    ) -> dict:
        """
        Analyze how accurate time estimates are vs actual time spent.
        """
        try:
            list_ids = _resolve_to_list_ids(project, list_id)
            if not list_ids:
                return {"error": f"No context found for '{project or list_id}'"}
            total_est, total_spent, count = 0, 0, 0
            for lid in list_ids:
                tasks = _fetch_all_tasks([lid], {"include_closed": "true"})
                for t in tasks:
                    est, spent = t.get("time_estimate") or 0, t.get("time_spent") or 0
                    if est > 0:
                        total_est += est
                        total_spent += spent
                        count += 1
            accuracy = (total_spent / total_est) if total_est else None
            return {
                "tasks_with_estimates": count,
                "total_estimate": total_est,
                "total_spent": total_spent,
                "accuracy_ratio": accuracy,
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_at_risk_tasks(
        project: Optional[str] = None, list_id: Optional[str] = None, risk_days: int = 3
    ) -> dict:
        """
        Find tasks that are overdue or at risk of missing deadline.
        """
        try:
            list_ids = _resolve_to_list_ids(project, list_id)
            if not list_ids:
                return {"error": f"No context found for '{project or list_id}'"}
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            at_risk = []
            for lid in list_ids:
                tasks = _fetch_all_tasks([lid], {"include_closed": "false"})
                for t in tasks:
                    due = t.get("due_date")
                    if due and str(due).isdigit():
                        due_ts = int(due)
                        days_left = int((due_ts - now) / (1000 * 60 * 60 * 24))
                        if days_left < 0:
                            urgency = "overdue"
                        elif days_left <= risk_days:
                            urgency = "at_risk"
                        else:
                            continue
                        at_risk.append(
                            {
                                "task_id": t.get("id"),
                                "name": t.get("name"),
                                "due_in_days": days_left,
                                "urgency": urgency,
                            }
                        )
            return {"at_risk_tasks": at_risk, "total_at_risk": len(at_risk)}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_status_summary(
        project: Optional[str] = None, list_id: Optional[str] = None
    ) -> dict:
        """
        Quick status rollup for stakeholder updates.
        """
        try:
            list_ids = _resolve_to_list_ids(project, list_id)
            if not list_ids:
                return {"error": f"No context found for '{project or list_id}'"}
            status_count, total = {}, 0
            for lid in list_ids:
                tasks = _fetch_all_tasks([lid], {"include_closed": "true"})
                for t in tasks:
                    status = t.get("status", {}).get("status", "Unknown")
                    status_count[status] = status_count.get(status, 0) + 1
                    total += 1
            return {"total_tasks": total, "status_breakdown": status_count}
        except Exception as e:
            return {"error": str(e)}
