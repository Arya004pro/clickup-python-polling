# app/mcp/task_management.py - Complete Optimized Version (All 9 Functions)

from fastmcp import FastMCP
import requests
import re
import time
from app.config import CLICKUP_API_TOKEN, BASE_URL

try:
    from app.config import CLICKUP_TEAM_ID
except ImportError:
    CLICKUP_TEAM_ID = None

SPACE_NAME_CACHE = {}

# ============================================================================
# HELPER FUNCTIONS
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


def _paginate_tasks(list_id, include_closed=True):
    """Fetch all tasks from a list with pagination."""
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


def get_space_name(space_id):
    if space_id in SPACE_NAME_CACHE:
        return SPACE_NAME_CACHE[space_id]
    data, _ = _api_call("get", f"/space/{space_id}")
    name = _safe_get(data, "space", "name") or "Unknown"
    SPACE_NAME_CACHE[space_id] = name
    return name


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
    ) -> dict:
        """List tasks in a list with optional filters."""
        try:
            params = [("include_closed", str(include_closed).lower())]
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

            formatted = [
                {
                    "task_id": t.get("id"),
                    "name": t.get("name"),
                    "status": _safe_get(t, "status", "status"),
                    "assignee": _format_assignees(t.get("assignees")),
                    "due_date": t.get("due_date"),
                }
                for t in all_tasks
            ]

            return {"total_tasks": len(formatted), "tasks": formatted}
        except Exception as e:
            return {"error": str(e), "tasks": []}

    @mcp.tool
    def get_task(task_id: str) -> dict:
        """Get detailed task information."""
        try:
            data, err = _api_call("get", f"/task/{task_id}")
            if err or not data or not data.get("id"):
                return {"error": err or f"Task {task_id} not found"}

            return {
                "task_id": _safe_get(data, "custom_id") or data.get("id"),
                "name": data.get("name"),
                "description": data.get("description"),
                "status": _safe_get(data, "status", "status"),
                "status_type": _safe_get(data, "status", "type"),
                "assignee": _format_assignees(data.get("assignees")),
                "due_date": data.get("due_date"),
                "priority": _safe_get(data, "priority", "priority"),
                "time_estimate": data.get("time_estimate"),
                "tracked_time": data.get("time_spent"),
                "custom_fields": [
                    {"name": cf.get("name"), "value": cf.get("value")}
                    for cf in data.get("custom_fields", [])
                    if isinstance(cf, dict)
                ],
                "list_id": _safe_get(data, "list", "id"),
                "list_name": _safe_get(data, "list", "name"),
                "folder_id": _safe_get(data, "folder", "id"),
                "folder_name": _safe_get(data, "folder", "name"),
                "space_id": _safe_get(data, "space", "id"),
                "space_name": _safe_get(data, "space", "name"),
            }
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
