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
        "BACKLOG", "QUEUED", "QUEUE", "IN QUEUE", "TO DO", "TO-DO", "PENDING", "OPEN", "IN PLANNING"
    ],
    "active": [
        "SCOPING", "IN DESIGN", "DEV", "IN DEVELOPMENT", "DEVELOPMENT", "REVIEW", 
        "IN REVIEW", "TESTING", "QA", "BUG", "BLOCKED", "WAITING", "STAGING DEPLOY", 
        "READY FOR DEVELOPMENT", "READY FOR PRODUCTION", "IN PROGRESS", "ON HOLD"
    ],
    "done": [
        "SHIPPED", "RELEASE", "COMPLETE", "DONE", "RESOLVED", "PROD", "QC CHECK"
    ],
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
        type_map = {"open": "not_started", "done": "done", "closed": "closed", "custom": "active"}
        return type_map.get(status_type.lower(), "other")
    return "other"

# --- API & Data Helpers ---

def _headers() -> Dict[str, str]:
    return {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}

def _api_call(method: str, endpoint: str, params: Optional[Dict] = None):
    url = f"{BASE_URL}{endpoint}"
    try:
        response = requests.request(method, url, headers=_headers(), params=params)
        return (response.json(), None) if response.status_code == 200 else (None, f"API Error {response.status_code}")
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
    
    # Basic resolution strategy
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

def _fetch_all_tasks(list_ids: List[str], base_params: Dict, include_archived: bool = True) -> List[Dict]:
    """Fetch ALL tasks including nested subtasks and archived items."""
    all_tasks = []
    seen_ids = set()
    flags = [False, True] if include_archived else [False]

    for list_id in list_ids:
        for is_archived in flags:
            page = 0
            while True:
                params = {**base_params, "page": page, "subtasks": "true", "archived": str(is_archived).lower()}
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

        direct_tracked = max(0, api_tracked - sum_child_tracked) if api_tracked >= sum_child_tracked else api_tracked
        direct_est = max(0, api_est - sum_child_est) if api_est >= sum_child_est else api_est

        res = (direct_tracked + sum_child_tracked, direct_tracked, direct_est + sum_child_est, direct_est)
        cache[tid] = res
        return res

    for tid in task_map: 
        get_values(tid)
    
    final_map = {}
    for tid, res in cache.items():
        final_map[tid] = {
            "tracked_total": res[0], "tracked_direct": res[1],
            "est_total": res[2], "est_direct": res[3]
        }
    return final_map

# --- Formatting Helpers ---

def _ms_to_readable(ms):
    return datetime.fromtimestamp(int(ms)/1000, tz=timezone.utc).strftime("%Y-%m-%d") if ms else "N/A"

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
        if (val := task.get(f)):
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
    def get_progress_since(since_date: str, project: Optional[str] = None, list_id: Optional[str] = None, include_status_changes: bool = True, include_archived: bool = False) -> dict:
        """
        Get tasks completed or changed since date. 
        Correctly identifies 'Shipped' as Done. 
        Provides breakdown of subtasks vs main tasks.
        """
        try:
            if "T" not in since_date: 
                since_date += "T00:00:00Z"
            since_ms = int(datetime.fromisoformat(since_date.replace("Z", "+00:00")).timestamp() * 1000)
            
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": f"No context found for '{project or list_id}'"}

            tasks = _fetch_all_tasks(list_ids, {"date_updated_gt": since_ms}, include_archived=include_archived)
            completed, status_changes = [], []
            
            # Detailed Breakdown Counters
            metrics = {
                "category_counts": {"not_started": 0, "active": 0, "done": 0, "closed": 0, "unknown": 0},
                "status_name_counts": {},
                "type_breakdown": {"main_tasks": 0, "subtasks": 0}
            }

            for t in tasks:
                status_obj = t.get("status", {}) if isinstance(t.get("status"), dict) else {}
                status_name = _extract_status_name(t)
                cat = get_status_category(status_name, status_obj.get("type", ""))

                # Check completion
                if cat in ["done", "closed"]:
                    done_date = t.get("date_closed") or t.get("date_done") or t.get("date_updated")
                    if done_date and int(done_date) >= since_ms:
                        completed.append({
                            "name": t.get("name"),
                            "status": status_name,
                            "completed_at": _ms_to_readable(done_date),
                            "is_subtask": bool(t.get("parent"))
                        })

                # Status Changes & Counts
                if include_status_changes:
                    if (upd := t.get("date_updated")) and int(upd) >= since_ms:
                        status_changes.append({
                            "name": t.get("name"),
                            "status": status_name,
                            "changed_at": _ms_to_readable(upd)
                        })
                    
                    # Update metrics
                    metrics["status_name_counts"][status_name] = metrics["status_name_counts"].get(status_name, 0) + 1
                    
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
                "metrics": metrics 
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_time_tracking_report(project: Optional[str] = None, list_id: Optional[str] = None, group_by: str = "assignee") -> dict:
        """Time tracking report using precise bottom-up metrics."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": "No context found."}

            all_tasks = _fetch_all_tasks(list_ids, {})
            metrics = _calculate_task_metrics(all_tasks)
            report = {}

            for t in all_tasks:
                m = metrics.get(t["id"], {})
                # Assignee view = Direct Time. Task view = Total (Rolled up) Time.
                val_t = m.get("tracked_direct", 0) if group_by == "assignee" else m.get("tracked_total", 0)
                val_e = m.get("est_direct", 0) if group_by == "assignee" else m.get("est_total", 0)

                if val_t == 0 and val_e == 0: 
                    continue

                keys = [u["username"] for u in t.get("assignees", [])] or ["Unassigned"] if group_by == "assignee" else [_extract_status_name(t)]
                if group_by == "task": 
                    keys = [t.get("name")]

                for k in keys:
                    r = report.setdefault(k, {"tasks": 0, "time_tracked": 0, "time_estimate": 0})
                    r["tasks"] += 1
                    div = len(keys) if group_by == "assignee" else 1
                    r["time_tracked"] += val_t // div
                    r["time_estimate"] += val_e // div

            formatted = {k: {**v, "human_tracked": _format_duration(v["time_tracked"]), "human_est": _format_duration(v["time_estimate"])} for k,v in report.items()}
            return {"report": formatted}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_task_time_breakdown(task_id: str) -> dict:
        """Detailed breakdown of a task tree."""
        try:
            task_data, err = _api_call("GET", f"/task/{task_id}")
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
                
                tree_view.append({
                    "task": f"{'  '*depth}{t.get('name')}",
                    "status": _extract_status_name(t),
                    "tracked_total": _format_duration(m.get("tracked_total", 0)),
                    "tracked_direct": _format_duration(m.get("tracked_direct", 0)),
                    "estimated": _format_duration(m.get("est_total", 0))
                })
                for cid in children_map.get(tid, []): 
                    build_tree(cid, depth+1)

            build_tree(task_id)
            return {"root_task": task_data["name"], "breakdown_tree": tree_view}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_estimation_accuracy(project: Optional[str] = None, list_id: Optional[str] = None) -> dict:
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
                "accuracy_breakdown": {"accurate": accurate, "under_estimated": under, "over_estimated": over}
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_at_risk_tasks(project: Optional[str] = None, list_id: Optional[str] = None, risk_days: int = 3) -> dict:
        """Find tasks overdue or due soon."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)): 
                return {"error": "No context"}
            tasks = _fetch_all_tasks(list_ids, {})
            now = time.time() * 1000
            limit = now + (risk_days * 86400000)
            
            risks = []
            for t in tasks:
                status = t.get("status", {}) if isinstance(t.get("status"), dict) else {}
                status_name = _extract_status_name(t)
                cat = get_status_category(status_name, status.get("type"))
                
                if cat in ["active", "not_started"]:
                    if due := t.get("due_date"):
                        due = int(due)
                        if due < now:
                            risks.append({"name": t["name"], "risk": "Overdue", "due": _ms_to_readable(due)})
                        elif due <= limit:
                            risks.append({"name": t["name"], "risk": "Due Soon", "due": _ms_to_readable(due)})
                            
            return {"at_risk_count": len(risks), "tasks": risks}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_stale_tasks(project: Optional[str] = None, list_id: Optional[str] = None, stale_days: int = 7) -> dict:
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
                        stale.append({"name": t["name"], "last_update": _ms_to_readable(updated)})
                        
            return {"stale_count": len(stale), "tasks": stale}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_untracked_tasks(project: Optional[str] = None, list_id: Optional[str] = None, status_filter: str = "in_progress") -> dict:
        """Find tasks with zero logged time."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)): 
                return {"error": "No context"}
            tasks = _fetch_all_tasks(list_ids, {})
            metrics = _calculate_task_metrics(tasks)
            untracked = []
            
            for t in tasks:
                status_obj = t.get("status", {}) if isinstance(t.get("status"), dict) else {}
                status_name = _extract_status_name(t)
                cat = get_status_category(status_name, status_obj.get("type"))
                
                check = (status_filter == "all") or (status_filter == "in_progress" and cat == "active")
                
                if check:
                    if metrics.get(t["id"], {}).get("tracked_direct", 0) == 0:
                        untracked.append({"name": t["name"], "status": status_name})
                        
            return {"count": len(untracked), "tasks": untracked}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_inactive_assignees(project: Optional[str] = None, list_id: Optional[str] = None, inactive_days: int = 3) -> dict:
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
            
            inactive = [{"user": k, "last_active": _ms_to_readable(v)} for k,v in activity_map.items() if v < cutoff]
            return {"inactive_count": len(inactive), "users": inactive}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_status_summary(project: Optional[str] = None, list_id: Optional[str] = None) -> dict:
        """Summary of task statuses."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)): 
                return {"error": "No context"}
            tasks = _fetch_all_tasks(list_ids, {})
            counts = {}
            categories = {"not_started": 0, "active": 0, "done": 0, "closed": 0, "other": 0}
            
            for t in tasks:
                status_obj = t.get("status", {}) if isinstance(t.get("status"), dict) else {}
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
        