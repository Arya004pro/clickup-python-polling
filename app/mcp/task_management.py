# app/mcp/task_management.py - Final Optimized Version
# Features:
# 1. Uses robust 'subtasks=true' fetching from PM Analytics (no manual recursion).
# 2. Implements 'Missing Parent' fetch to handle cross-list time rollups.
# 3. Uses '_calculate_task_metrics' for precise Bottom-Up time summation.

from fastmcp import FastMCP
import requests
import re
import time
from typing import List, Dict
from app.config import CLICKUP_API_TOKEN, BASE_URL

try:
    from app.config import CLICKUP_TEAM_ID
except ImportError:
    CLICKUP_TEAM_ID = None

SPACE_NAME_CACHE = {}

# ============================================================================
# HELPER FUNCTIONS (API & Formatting)
# ============================================================================


def _headers():
    return {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}


def _api_call(method, endpoint, params=None, payload=None):
    """Unified API call handler."""
    url = f"{BASE_URL}{endpoint}"
    kwargs = {"headers": _headers()}
    if params:
        kwargs["params"] = params
    if payload:
        kwargs["json"] = payload

    response = getattr(requests, method)(url, **kwargs)
    success_codes = (200, 201) if method in ("post", "put") else (200,)

    if response.status_code not in success_codes:
        return None, f"API error {response.status_code}: {response.text}"
    return response.json(), None


def _get_team_id():
    if CLICKUP_TEAM_ID:
        return CLICKUP_TEAM_ID, None
    data, err = _api_call("get", "/team")
    if err:
        return None, err
    teams = data.get("teams", [])
    return teams[0]["id"] if teams else None, "No teams found" if not teams else None


def _safe_get(obj, *keys):
    for key in keys:
        obj = obj.get(key) if isinstance(obj, dict) else None
        if obj is None:
            return None
    return obj


def _format_assignees(assignees):
    return [a.get("username") for a in (assignees or []) if a.get("username")]


def _format_duration(ms: int) -> str:
    """Format milliseconds to human-readable duration."""
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


# ============================================================================
# CORE LOGIC (Ported from pm_analytics.py for consistency)
# ============================================================================


def _fetch_all_tasks(
    list_ids: List[str], base_params: Dict, include_archived: bool = True
) -> List[Dict]:
    """
    Fetch ALL tasks including deeply nested subtasks.

    By default this will include archived tasks. Set `include_archived=False` to only fetch active tasks.
    """
    all_tasks = []
    seen_ids = set()

    flags = [False, True] if include_archived else [False]

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

                data, error = _api_call("get", f"/list/{list_id}/task", params=params)

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


def _fetch_missing_parents(all_tasks: List[Dict]) -> List[Dict]:
    """
    Identifies if any tasks have parents that are NOT in the current list,
    and fetches them. This ensures cross-list parents (Main Tasks) are included.
    """
    existing_ids = {t["id"] for t in all_tasks}
    missing_parents = set()

    for t in all_tasks:
        parent_id = t.get("parent")
        if parent_id and parent_id not in existing_ids:
            missing_parents.add(parent_id)

    if not missing_parents:
        return all_tasks

    extended_tasks = all_tasks.copy()

    for pid in missing_parents:
        data, err = _api_call(
            "get", f"/task/{pid}", params={"include_subtasks": "true"}
        )
        if data and not err:
            if data["id"] not in existing_ids:
                existing_ids.add(data["id"])
                extended_tasks.append(data)

                # Fetch grandparent if needed (1 level up)
                grandparent = data.get("parent")
                if grandparent and grandparent not in existing_ids:
                    gp_data, gp_err = _api_call(
                        "get",
                        f"/task/{grandparent}",
                        params={"include_subtasks": "true"},
                    )
                    if gp_data and not gp_err and gp_data["id"] not in existing_ids:
                        existing_ids.add(gp_data["id"])
                        extended_tasks.append(gp_data)

    return extended_tasks


def _calculate_task_metrics(all_tasks: List[Dict]) -> Dict[str, Dict[str, int]]:
    """
    CORE CALCULATION ENGINE: Robust Bottom-Up Calculation.
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
        # Logic: If API Time > Children Sum, the remainder is Direct Time.
        if api_tracked < sum_child_total_tracked:
            direct_tracked = api_tracked  # Fallback (API returning direct time)
        else:
            direct_tracked = api_tracked - sum_child_total_tracked
        direct_tracked = max(0, direct_tracked)

        # --- Calculate Direct Estimate ---
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


def _build_subtask_tree(all_tasks: List[Dict], parent_id: str) -> List[Dict]:
    """
    Recursively build a nested tree of subtasks for a given parent task.
    Returns list of subtasks, each with their own 'subtasks' field if they have children.
    """
    subtasks = []
    for t in all_tasks:
        if t.get("parent") == parent_id:
            subtask = {
                "task_id": t.get("id"),
                "name": t.get("name"),
                "status": _safe_get(t, "status", "status"),
                "assignee": _format_assignees(t.get("assignees")),
                "due_date": t.get("due_date"),
            }
            # Recursively add subtasks
            children = _build_subtask_tree(all_tasks, t["id"])
            if children:
                subtask["subtasks"] = children
            subtasks.append(subtask)
    return subtasks


def _paginate_tasks(list_id, include_closed=True):
    """Fetch all tasks from a list with pagination (Legacy Helper)."""
    all_tasks, page = [], 0
    while True:
        data, err = _api_call(
            "get",
            f"/list/{list_id}/task",
            [("page", str(page)), ("include_closed", str(include_closed).lower())],
        )
        if err or not data:
            return all_tasks, err
        tasks = data.get("tasks", [])
        if not tasks:
            break
        all_tasks.extend(tasks)
        page += 1
    return all_tasks, None


# ============================================================================
# TOOL REGISTRATION
# ============================================================================


def register_task_tools(mcp: FastMCP):
    @mcp.tool
    def get_tasks(
        list_id: str,
        include_closed: bool = False,
        statuses: list[str] = None,
        assignees: list[int] = None,
        page: int = None,
        filter_no_time_entries: bool = False,
    ) -> dict:
        """
        List tasks in a list with optional filters.

        Args:
            list_id: The ClickUp list ID to fetch tasks from.
            include_closed: Whether to include closed tasks.
            statuses: Filter by specific status names.
            assignees: Filter by assignee IDs.
            page: Specific page number (None = fetch all pages).
            filter_no_time_entries: If True, only return tasks with zero tracked time (time_spent = 0 or None).
        """
        try:
            params = [
                ("include_closed", str(include_closed).lower()),
                ("subtasks", "true"),  # Include nested subtasks for proper time rollups
            ]
            if statuses:
                params.extend([("statuses[]", s) for s in statuses])
            if assignees:
                params.extend([("assignees[]", str(a)) for a in assignees])

            all_tasks, current_page = [], page if page is not None else 0

            while True:
                response = requests.get(
                    f"{BASE_URL}/list/{list_id}/task",
                    headers=_headers(),
                    params=params + [("page", str(current_page))],
                )
                if response.status_code != 200:
                    return {"error": f"API error {response.status_code}", "tasks": []}

                tasks = response.json().get("tasks", [])
                if not tasks:
                    break
                all_tasks.extend(tasks)
                current_page += 1
                if page is not None:
                    break

            # Filter for tasks with no time entries if requested
            if filter_no_time_entries:
                all_tasks = [t for t in all_tasks if int(t.get("time_spent") or 0) == 0]

            formatted = [
                {
                    "task_id": t.get("id"),
                    "name": t.get("name"),
                    "status": _safe_get(t, "status", "status"),
                    "assignee": _format_assignees(t.get("assignees")),
                    "due_date": t.get("due_date"),
                    "time_spent": int(t.get("time_spent") or 0),
                    "time_spent_readable": _format_duration(
                        int(t.get("time_spent") or 0)
                    ),
                }
                for t in all_tasks
            ]

            # Build status counts for returned tasks
            status_counts = {}
            for t in all_tasks:
                sname = _safe_get(t, "status", "status") or "Unknown"
                status_counts[sname] = status_counts.get(sname, 0) + 1

            # If caller provided a `statuses` filter, ensure counts for each requested status are present (0 if absent)
            requested_status_counts = {}
            if statuses:
                for s in statuses:
                    # Keep original casing for keys as caller provided
                    requested_status_counts[s] = status_counts.get(s, 0)

            return {
                "total_tasks": len(formatted),
                "tasks": formatted,
                "status_counts": status_counts,
                "requested_status_counts": requested_status_counts,
                "filter_applied": "no_time_entries" if filter_no_time_entries else None,
            }
        except Exception as e:
            return {"error": str(e), "tasks": []}

    @mcp.tool
    def get_task(task_id: str) -> dict:
        """
        Get detailed task information.

        INCLUDES: 'calculated_time_spent' and 'calculated_time_estimate'.
        These fields represent the TRUE rolled-up values calculated bottom-up,
        ensuring deep subtasks (even closed ones) and missing parents are counted.
        """
        try:
            # 1. Fetch the requested task
            task_data, err = _api_call(
                "get", f"/task/{task_id}", params={"include_subtasks": "true"}
            )
            if err or not task_data:
                return {"error": f"Task {task_id} not found"}

            # 2. Fetch Context (All tasks in the same list to build tree)
            list_id = _safe_get(task_data, "list", "id")

            calc_metrics = {}
            if list_id:
                tasks_in_list = _fetch_all_tasks([list_id], {})
                all_tree_tasks = _fetch_missing_parents(tasks_in_list)
                metrics_map = _calculate_task_metrics(all_tree_tasks)
                calc_metrics = metrics_map.get(task_data["id"], {})

            # 3. Extract calculated values
            tracked_total = calc_metrics.get(
                "tracked_total", task_data.get("time_spent") or 0
            )
            tracked_direct = calc_metrics.get("tracked_direct", 0)
            est_total = calc_metrics.get(
                "est_total", task_data.get("time_estimate") or 0
            )

            # Build result and then attempt a best-effort space name lookup before returning
            result = {
                "task_id": _safe_get(task_data, "custom_id") or task_data.get("id"),
                "name": task_data.get("name"),
                "description": task_data.get("description"),
                "status": _safe_get(task_data, "status", "status"),
                "status_type": _safe_get(task_data, "status", "type"),
                "assignee": _format_assignees(task_data.get("assignees")),
                "due_date": task_data.get("due_date"),
                "priority": _safe_get(task_data, "priority", "priority"),
                # --- Time Tracking (Accurate) ---
                "calculated_time_spent": _format_duration(tracked_total),
                "calculated_time_estimate": _format_duration(est_total),
                "time_breakdown": {
                    "total_rolled_up": tracked_total,
                    "total_direct": tracked_direct,
                    "subtasks_contribution": tracked_total - tracked_direct,
                },
                "custom_fields": [
                    {"name": cf.get("name"), "value": cf.get("value")}
                    for cf in task_data.get("custom_fields", [])
                    if isinstance(cf, dict)
                ],
                "list_id": list_id,
                "list_name": _safe_get(task_data, "list", "name"),
                "folder_id": _safe_get(task_data, "folder", "id"),
                "folder_name": _safe_get(task_data, "folder", "name"),
                "space_id": _safe_get(task_data, "space", "id"),
                "space_name": _safe_get(task_data, "space", "name"),
            }

            # Add subtasks if available
            subtasks = []
            if list_id and all_tree_tasks:
                subtasks = _build_subtask_tree(all_tree_tasks, task_data["id"])
            result["subtasks"] = subtasks

            # If ClickUp task object lacks the space name, try a direct space fetch (best-effort)
            try:
                if not result.get("space_name"):
                    space_id = result.get("space_id") or _safe_get(
                        task_data, "space", "id"
                    )
                    if space_id:
                        space_data, sp_err = _api_call("get", f"/space/{space_id}")
                        if not sp_err and space_data:
                            space_obj = space_data.get("space") or space_data
                            if isinstance(space_obj, dict):
                                name = space_obj.get("name")
                                if name:
                                    result["space_name"] = name
                                    result["space_id"] = space_id
            except Exception:
                # Don't fail the whole tool on this best-effort lookup
                pass

            return result
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool
    def create_task(
        list_id: str,
        name: str,
        description: str = None,
        status: str = None,
        priority: int = None,
        assignees: list[int] = None,
        due_date: str = None,
        tags: list[str] = None,
    ) -> dict:
        """Create a new task."""
        try:
            payload = {
                k: v
                for k, v in {
                    "name": name,
                    "description": description,
                    "status": status,
                    "priority": priority,
                    "assignees": assignees,
                    "due_date": due_date,
                    "tags": tags,
                }.items()
                if v is not None
            }

            data, err = _api_call("post", f"/list/{list_id}/task", payload=payload)
            if err:
                return {"error": err}

            return {
                "task_id": data.get("id"),
                "url": data.get("url"),
                "status": "success",
                "message": f"Task '{name}' created",
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool
    def update_task(
        task_id: str,
        name: str = None,
        description: str = None,
        status: str = None,
        priority: int = None,
        due_date: str = None,
        add_assignees: list[int] = None,
        remove_assignees: list[int] = None,
    ) -> dict:
        """Update an existing task."""
        try:
            payload = {
                k: v
                for k, v in {
                    "name": name,
                    "description": description,
                    "status": status,
                    "priority": priority,
                    "due_date": due_date,
                }.items()
                if v is not None
            }

            if add_assignees or remove_assignees:
                payload["assignees"] = {}
                if add_assignees:
                    payload["assignees"]["add"] = add_assignees
                if remove_assignees:
                    payload["assignees"]["rem"] = remove_assignees

            if not payload:
                return {"error": "No fields to update"}

            data, err = _api_call("put", f"/task/{task_id}", payload=payload)
            if err:
                return {"error": err}

            return {
                "task_id": data.get("id", task_id),
                "status": "success",
                "message": "Task updated successfully",
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool
    def search_tasks(
        project: str,
        query: str,
        include_closed: bool = False,
        whole_word: bool = False,
    ) -> dict:
        """Search tasks within a folder or space."""
        try:
            team_id, err = _get_team_id()
            if err:
                return {"error": err, "results": []}

            spaces_data, err = _api_call("get", f"/team/{team_id}/space")
            if err:
                return {"error": err, "results": []}

            spaces, all_lists, project_info = spaces_data.get("spaces", []), [], None

            for space in spaces:
                space_id, space_name = space["id"], space["name"]

                if project.lower() == space_name.lower():
                    project_info = {"type": "space", "name": space_name}
                    lists_data, _ = _api_call("get", f"/space/{space_id}/list")
                    if lists_data:
                        all_lists.extend(lists_data.get("lists", []))
                    folders_data, _ = _api_call("get", f"/space/{space_id}/folder")
                    if folders_data:
                        for folder in folders_data.get("folders", []):
                            all_lists.extend(folder.get("lists", []))
                    break

                folders_data, _ = _api_call("get", f"/space/{space_id}/folder")
                if folders_data:
                    for folder in folders_data.get("folders", []):
                        if project.lower() == folder["name"].lower():
                            project_info = {
                                "type": "folder",
                                "name": folder["name"],
                                "space": space_name,
                            }
                            all_lists.extend(folder.get("lists", []))
                            break
                if project_info:
                    break

            if not project_info:
                return {"error": f"Project '{project}' not found", "results": []}

            matching_tasks, query_lower = [], query.lower()
            pattern = r"\b" + re.escape(query_lower) + r"\b" if whole_word else None

            for lst in all_lists:
                tasks, _ = _paginate_tasks(lst["id"], include_closed)
                for task in tasks:
                    task_name, task_desc = (
                        (task.get("name") or "").lower(),
                        (task.get("text_content") or "").lower(),
                    )

                    if whole_word:
                        matched = bool(
                            re.search(pattern, task_name)
                            or re.search(pattern, task_desc)
                        )
                        match_loc = (
                            "name" if re.search(pattern, task_name) else "description"
                        )
                    else:
                        matched = query_lower in task_name or query_lower in task_desc
                        match_loc = (
                            "name" if query_lower in task_name else "description"
                        )

                    if matched:
                        full_desc = task.get("text_content") or ""
                        matching_tasks.append(
                            {
                                "task_id": task.get("id"),
                                "name": task.get("name"),
                                "description": full_desc[:300]
                                + ("..." if len(full_desc) > 300 else ""),
                                "status": _safe_get(task, "status", "status"),
                                "assignee": _format_assignees(task.get("assignees")),
                                "due_date": task.get("due_date"),
                                "list_name": lst["name"],
                                "url": task.get("url"),
                                "matched_in": match_loc,
                            }
                        )

            return {
                "project": project,
                "project_type": project_info["type"],
                "query": query,
                "whole_word_match": whole_word,
                "total_results": len(matching_tasks),
                "results": matching_tasks,
            }
        except Exception as e:
            return {"error": f"Search failed: {str(e)}", "results": []}

    @mcp.tool
    def get_project_tasks(
        project: str,
        include_closed: bool = False,
        statuses: list[str] = None,
    ) -> dict:
        """Get all tasks in a project (folder/space) with optional filters."""
        try:
            team_id, err = _get_team_id()
            if err:
                return {"error": err, "tasks": []}

            spaces_data, err = _api_call("get", f"/team/{team_id}/space")
            if err:
                return {"error": err, "tasks": []}

            all_tasks, spaces = [], spaces_data.get("spaces", [])

            for space in spaces:
                space_id, space_name = space["id"], space["name"]

                if project.lower() == space_name.lower():
                    lists_data, _ = _api_call("get", f"/space/{space_id}/list")
                    lists = lists_data.get("lists", []) if lists_data else []
                    folders_data, _ = _api_call("get", f"/space/{space_id}/folder")
                    if folders_data:
                        for folder in folders_data.get("folders", []):
                            lists.extend(folder.get("lists", []))
                    for lst in lists:
                        result = get_tasks(lst["id"], include_closed, statuses)
                        all_tasks.extend(result.get("tasks", []))
                    break

                folders_data, _ = _api_call("get", f"/space/{space_id}/folder")
                if folders_data:
                    for folder in folders_data.get("folders", []):
                        if project.lower() == folder["name"].lower():
                            for lst in folder.get("lists", []):
                                result = get_tasks(lst["id"], include_closed, statuses)
                                all_tasks.extend(result.get("tasks", []))
                            break

            return {
                "project": project,
                "total_tasks": len(all_tasks),
                "tasks": all_tasks,
            }
        except Exception as e:
            return {"error": str(e), "tasks": []}

    @mcp.tool
    def get_list_progress(list_id: str) -> dict:
        """Get progress summary for a list (useful for sprints)."""
        try:
            tasks, err = _paginate_tasks(list_id, include_closed=True)
            if err:
                return {"error": err}
            if not tasks:
                return {"error": "No tasks found in this list"}

            status_stage_map = {
                "backlog": "not_started",
                "queued": "not_started",
                "scoping": "active",
                "in design": "active",
                "in development": "active",
                "in review": "active",
                "testing": "active",
                "ready for development": "active",
                "shipped": "done",
                "cancelled": "closed",
            }

            stage_count = {"not_started": 0, "active": 0, "done": 0, "closed": 0}
            status_count, completed, now = {}, 0, int(time.time() * 1000)
            week_ago, completed_last_week = now - 7 * 24 * 60 * 60 * 1000, 0

            for t in tasks:
                status_name = _safe_get(t, "status", "status") or "Unknown"
                status_key = status_name.strip().lower()
                stage = status_stage_map.get(status_key, "active")
                stage_count[stage] += 1
                status_count[status_name] = status_count.get(status_name, 0) + 1

                if status_key == "shipped":
                    completed += 1
                    for field in ["date_done", "date_closed", "date_updated"]:
                        ts = t.get(field)
                        if ts and str(ts).isdigit():
                            ts = int(ts)
                            if ts >= week_ago:
                                completed_last_week += 1
                            break

            return {
                "list_id": list_id,
                "total_tasks": len(tasks),
                "completion_rate": round(completed / len(tasks), 3) if tasks else 0,
                "status_breakdown": status_count,
                "stage_breakdown": stage_count,
                "velocity_7d": completed_last_week,
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool
    def get_workload(list_id: str) -> dict:
        """Get workload distribution per team member."""
        try:
            tasks, err = _paginate_tasks(list_id, include_closed=True)
            if err:
                return {"error": err}

            workload = {}
            for t in tasks:
                assignees = t.get("assignees", [])
                if not assignees:
                    workload["Unassigned"] = workload.get("Unassigned", 0) + 1
                else:
                    for a in assignees:
                        name = (
                            a.get("username") or a.get("email") or f"User_{a.get('id')}"
                        )
                        workload[name] = workload.get(name, 0) + 1

            return {"list_id": list_id, "workload": workload, "total_tasks": len(tasks)}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool
    def get_overdue_tasks(list_id: str) -> dict:
        """Get all overdue tasks in a list."""
        try:
            tasks, err = _paginate_tasks(list_id, include_closed=False)
            if err:
                return {"error": err}

            now, overdue_tasks = int(time.time() * 1000), []
            for t in tasks:
                due_date = t.get("due_date")
                if due_date and str(due_date).isdigit():
                    due_ts = int(due_date)
                    if due_ts < now:
                        days_overdue = int((now - due_ts) / (1000 * 60 * 60 * 24))
                        overdue_tasks.append(
                            {
                                "task_id": t.get("id"),
                                "name": t.get("name"),
                                "days_overdue": days_overdue,
                                "assignees": _format_assignees(t.get("assignees")),
                            }
                        )

            return {
                "list_id": list_id,
                "overdue_tasks": overdue_tasks,
                "total_overdue": len(overdue_tasks),
            }
        except Exception as e:
            return {"error": str(e)}
