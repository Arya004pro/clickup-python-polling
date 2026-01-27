# app/mcp/task_management.py

from fastmcp import FastMCP
import requests
import re
from app.config import CLICKUP_API_TOKEN, BASE_URL

# Optionally import CLICKUP_TEAM_ID if defined, otherwise set to None
try:
    from app.config import CLICKUP_TEAM_ID
except ImportError:
    CLICKUP_TEAM_ID = None

# Cache for space names to avoid redundant API calls
SPACE_NAME_CACHE = {}


def get_space_name(space_id):
    if space_id in SPACE_NAME_CACHE:
        return SPACE_NAME_CACHE[space_id]

    try:
        resp = requests.get(
            f"{BASE_URL}/space/{space_id}", headers={"Authorization": CLICKUP_API_TOKEN}
        )
        if resp.status_code == 200:
            name = resp.json().get("space", {}).get("name")
            SPACE_NAME_CACHE[space_id] = name
            return name
    except Exception:
        pass
    return "Unknown"


def register_task_tools(mcp: FastMCP):
    @mcp.tool
    def get_tasks(
        list_id: str,
        include_closed: bool = False,
        statuses: list[str] = None,
        assignees: list[int] = None,
        page: int = None,
    ) -> list[dict]:
        """
        List tasks in a list with optional filters.
        Handles single and multiple statuses/assignees correctly.
        """
        try:
            headers = {
                "Authorization": CLICKUP_API_TOKEN,
                "Content-Type": "application/json",
            }

            # Use list of tuples to force repeated keys (statuses[]=, assignees[]=)
            params = [
                ("include_closed", str(include_closed).lower()),
            ]

            if statuses:
                for s in statuses:
                    params.append(("statuses[]", s))

            if assignees:
                for a in assignees:
                    params.append(("assignees[]", str(a)))

            if page is not None:
                params.append(("page", str(page)))

            all_tasks = []
            current_page = page if page is not None else 0

            while True:
                params.append(("page", str(current_page)))
                response = requests.get(
                    f"{BASE_URL}/list/{list_id}/task", headers=headers, params=params
                )
                params.pop()

                if response.status_code != 200:
                    return [
                        {
                            "error": f"ClickUp API error {response.status_code}: {response.text}"
                        }
                    ]

                data = response.json()
                page_tasks = data.get("tasks", [])

                if not page_tasks:
                    break

                all_tasks.extend(page_tasks)
                current_page += 1

                if page is not None:
                    break

            # Format exactly as per specs
            formatted = []
            for t in all_tasks:
                assignee_usernames = [
                    a.get("username")
                    for a in t.get("assignees", [])
                    if a.get("username")
                ]

                formatted.append(
                    {
                        "task_id": t.get("id"),
                        "name": t.get("name"),
                        "status": t.get("status", {}).get("status"),
                        "assignee": assignee_usernames,
                        "due_date": t.get("due_date"),
                    }
                )

            return formatted

        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool
    def get_task(task_id: str) -> dict:
        """
        Get details of a specific task including its lists.

        Parameters:
        - task_id (string, required): The task ID to retrieve.

        Returns: Complete task details including description, time tracking, custom fields.
        """
        try:
            headers = {
                "Authorization": CLICKUP_API_TOKEN,
                "Content-Type": "application/json",
            }

            response = requests.get(f"{BASE_URL}/task/{task_id}", headers=headers)

            if response.status_code != 200:
                return {
                    "error": f"ClickUp API error {response.status_code}: {response.text}"
                }

            task = response.json()

            if not task or not isinstance(task, dict) or not task.get("id"):
                return {"error": f"Task {task_id} not found"}

            # Extract assignees as list of usernames
            assignees = [
                a.get("username") or f"User_{a.get('id')}"
                for a in task.get("assignees", [])
                if isinstance(a, dict)
            ]

            # Extract custom fields (list of name/value pairs)
            custom_fields = [
                {"name": cf.get("name"), "value": cf.get("value")}
                for cf in task.get("custom_fields", [])
                if isinstance(cf, dict)
            ]

            # Safely get nested values with type checking
            def safe_get_nested(obj, *keys):
                """Safely navigate nested dictionaries"""
                for key in keys:
                    if isinstance(obj, dict):
                        obj = obj.get(key)
                    else:
                        return None
                return obj

            # Build result with complete details
            result = {
                "task_id_short": task.get("id"),
                "task_id": safe_get_nested(task, "custom_task_ids", 0, "id")
                or task.get("id"),
                "name": task.get("name"),
                "description": task.get("description"),
                "status": safe_get_nested(task, "status", "status"),
                "status_type": safe_get_nested(task, "status", "type"),
                "assignee": assignees,
                "due_date": task.get("due_date"),
                "priority": safe_get_nested(task, "priority", "priority"),
                "time_estimate": task.get("time_estimate"),
                "tracked_time": task.get(
                    "time_spent"
                ),  # ClickUp uses 'time_spent', not 'time_tracking.tracked_time'
                "custom_fields": custom_fields,
                "list_id": safe_get_nested(task, "list", "id"),
                "list_name": safe_get_nested(task, "list", "name"),
                "folder_id": safe_get_nested(task, "folder", "id"),
                "folder_name": safe_get_nested(task, "folder", "name"),
                "space_id": safe_get_nested(task, "space", "id"),
                "space_name": safe_get_nested(task, "space", "name"),
            }

            return result

        except Exception as e:
            return {"error": f"Unexpected error: {str(e)}"}

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
        """
        Create a new task in a list.

        Parameters:
        - list_id (string, required): List to create task in
        - name (string, required): Task name
        - description (string, optional): Task description (markdown supported)
        - status (string, optional): Initial status
        - priority (int, optional): 1=urgent, 2=high, 3=normal, 4=low
        - assignees (list[int], optional): User IDs to assign
        - due_date (string, optional): Due date (ISO 8601 or timestamp)
        - tags (list[str], optional): Tag names to apply

        Returns: Created task confirmation with ID and URL.
        """
        try:
            headers = {
                "Authorization": CLICKUP_API_TOKEN,
                "Content-Type": "application/json",
            }
            payload = {
                "name": name,
            }
            if description is not None:
                payload["description"] = description
            if status is not None:
                payload["status"] = status
            if priority is not None:
                payload["priority"] = priority
            if assignees is not None:
                payload["assignees"] = assignees
            if due_date is not None:
                payload["due_date"] = due_date
            if tags is not None:
                payload["tags"] = tags

            response = requests.post(
                f"{BASE_URL}/list/{list_id}/task",
                headers=headers,
                json=payload,
            )

            if response.status_code not in (200, 201):
                error_msg = f"ClickUp API error {response.status_code}: {response.text}"
                print(f"[ERROR] {error_msg}")
                return {"error": error_msg, "response": response.text}

            data = response.json()
            return {
                "task_id": data.get("id"),
                "url": data.get("url"),
                "status": "success",
                "message": f"Task '{name}' created in list {list_id}",
            }
        except Exception as e:
            print(f"[ERROR] create_task failed: {str(e)}")
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
        """
        Update an existing task.

        Parameters:
        - task_id (string, required): Task ID to update
        - name (string, optional): New task name
        - description (string, optional): New description
        - status (string, optional): New status
        - priority (int or None, optional): New priority (null to remove)
        - due_date (string or None, optional): New due date (null to remove)
        - add_assignees (list[int], optional): User IDs to add
        - remove_assignees (list[int], optional): User IDs to remove

        Returns: Updated task confirmation.
        """
        try:
            headers = {
                "Authorization": CLICKUP_API_TOKEN,
                "Content-Type": "application/json",
            }
            payload = {}
            if name is not None:
                payload["name"] = name
            if description is not None:
                payload["description"] = description
            if status is not None:
                payload["status"] = status
            if priority is not None:
                payload["priority"] = priority
            if due_date is not None:
                payload["due_date"] = due_date
            if add_assignees or remove_assignees:
                payload["assignees"] = {}
                if add_assignees:
                    payload["assignees"]["add"] = add_assignees
                if remove_assignees:
                    payload["assignees"]["rem"] = remove_assignees

            if not payload:
                return {"error": "No fields to update"}

            response = requests.put(
                f"{BASE_URL}/task/{task_id}",
                headers=headers,
                json=payload,
            )

            if response.status_code not in (200, 201):
                error_msg = f"ClickUp API error {response.status_code}: {response.text}"
                print(f"[ERROR] {error_msg}")
                return {"error": error_msg, "response": response.text}

            data = response.json()
            return {
                "task_id": data.get("id", task_id),
                "status": "success",
                "message": f"Task '{task_id}' updated successfully",
            }
        except Exception as e:
            print(f"[ERROR] update_task failed: {str(e)}")
            return {"error": str(e)}

    @mcp.tool
    def search_tasks(
        project: str,
        query: str,
        include_closed: bool = False,
        whole_word: bool = True,
    ) -> dict:
        """
        Search tasks within a folder or space.

        Parameters:
        - project: Folder name ("AyuRAG Agent") or Space name ("AIX")
        - query: Search term (searches in task name and description)
        - include_closed: Include completed/closed tasks
        - whole_word: If true, match whole words only (e.g., "bot" won't match "robot")

        Returns: Matching tasks with their details
        """
        try:
            headers = {
                "Authorization": CLICKUP_API_TOKEN,
                "Content-Type": "application/json",
            }

            # Get team ID
            team_id = CLICKUP_TEAM_ID
            if not team_id:
                teams_response = requests.get(f"{BASE_URL}/team", headers=headers)
                if teams_response.status_code == 200:
                    teams = teams_response.json().get("teams", [])
                    team_id = teams[0]["id"] if teams else None
                if not team_id:
                    return {"error": "No teams found", "results": []}

            # Get all spaces
            spaces_response = requests.get(
                f"{BASE_URL}/team/{team_id}/space", headers=headers
            )
            if spaces_response.status_code != 200:
                return {"error": "Failed to fetch spaces", "results": []}

            spaces = spaces_response.json().get("spaces", [])

            # Find target project and collect lists
            all_lists = []
            project_info = None

            for space in spaces:
                space_id = space["id"]
                space_name = space["name"]

                # Check if searching for this space
                if project.lower() == space_name.lower():
                    project_info = {"type": "space", "name": space_name}

                    # Get folderless lists
                    lists_response = requests.get(
                        f"{BASE_URL}/space/{space_id}/list", headers=headers
                    )
                    if lists_response.status_code == 200:
                        for lst in lists_response.json().get("lists", []):
                            lst["space_id"] = space_id
                            lst["space_name"] = space_name
                            all_lists.append(lst)

                    # Get lists from folders
                    folders_response = requests.get(
                        f"{BASE_URL}/space/{space_id}/folder", headers=headers
                    )
                    if folders_response.status_code == 200:
                        for folder in folders_response.json().get("folders", []):
                            for lst in folder.get("lists", []):
                                lst["space_id"] = space_id
                                lst["space_name"] = space_name
                                lst["folder_name"] = folder.get("name")
                                all_lists.append(lst)
                    break

                # Check folders
                folders_response = requests.get(
                    f"{BASE_URL}/space/{space_id}/folder", headers=headers
                )
                if folders_response.status_code == 200:
                    for folder in folders_response.json().get("folders", []):
                        if project.lower() == folder["name"].lower():
                            project_info = {
                                "type": "folder",
                                "name": folder["name"],
                                "space": space_name,
                            }
                            for lst in folder.get("lists", []):
                                lst["space_id"] = space_id
                                lst["space_name"] = space_name
                                lst["folder_name"] = folder.get("name")
                                all_lists.append(lst)
                            break

                if project_info:
                    break

            if not project_info:
                return {
                    "error": f"Project '{project}' not found",
                    "hint": "Check spelling (case-insensitive). Examples: 'AyuRAG Agent', 'AIX'",
                    "results": [],
                }

            # Search tasks
            matching_tasks = []
            query_lower = query.lower()
            pattern = r"\b" + re.escape(query_lower) + r"\b"

            for lst in all_lists:
                list_id = lst["id"]
                list_name = lst["name"]
                space_id = lst.get("space_id")
                space_name = lst.get("space_name")
                folder_name = lst.get("folder_name")
                page = 0

                while True:
                    params = [
                        ("page", str(page)),
                        ("include_closed", str(include_closed).lower()),
                    ]

                    tasks_response = requests.get(
                        f"{BASE_URL}/list/{list_id}/task",
                        headers=headers,
                        params=params,
                    )

                    if tasks_response.status_code != 200:
                        break

                    tasks = tasks_response.json().get("tasks", [])
                    if not tasks:
                        break

                    for task in tasks:
                        task_name = (task.get("name") or "").lower()
                        task_desc = (task.get("text_content") or "").lower()

                        # Whole word match only
                        matched = False
                        match_location = None
                        if re.search(pattern, task_name):
                            matched = True
                            match_location = "name"
                        elif re.search(pattern, task_desc):
                            matched = True
                            match_location = "description"

                        if matched:
                            assignees = [
                                a.get("username")
                                for a in task.get("assignees", [])
                                if a.get("username")
                            ]

                            # Get full description (up to 500 chars for debugging)
                            full_desc = task.get("text_content") or ""

                            matching_tasks.append(
                                {
                                    "task_id": task.get("id"),
                                    "name": task.get("name"),
                                    "description": full_desc[:300]
                                    + ("..." if len(full_desc) > 300 else ""),
                                    "status": task.get("status", {}).get("status"),
                                    "assignee": assignees,
                                    "due_date": task.get("due_date"),
                                    "list_name": list_name,
                                    "folder_name": folder_name,
                                    "space_id": space_id,
                                    "space_name": space_name,
                                    "url": task.get("url"),
                                    "matched_in": match_location,  # Show where query was found
                                }
                            )

                    page += 1

            return {
                "project": project,
                "project_type": project_info["type"],
                "query": query,
                "whole_word_match": True,
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
        """
        Get all tasks in a mapped project with optional filters.
        Dynamically resolves project name to space/folder/list IDs.
        """
        try:
            import re

            headers = {
                "Authorization": CLICKUP_API_TOKEN,
                "Content-Type": "application/json",
            }

            def normalize(s):
                return re.sub(r"[^a-z0-9]+", "", s.lower().strip())

            project_norm = normalize(project)

            # Step 1: Get workspaces
            ws_resp = requests.get(f"{BASE_URL}/team", headers=headers)
            if ws_resp.status_code != 200:
                return {"error": f"Failed to fetch workspaces: {ws_resp.text}"}

            workspaces = ws_resp.json().get("teams", [])
            if not workspaces:
                return {"error": "No accessible workspaces"}

            team_id = workspaces[0]["id"]

            # Step 2: Get spaces
            space_resp = requests.get(
                f"{BASE_URL}/team/{team_id}/space", headers=headers
            )
            if space_resp.status_code != 200:
                return {"error": f"Failed to fetch spaces: {space_resp.text}"}

            spaces = space_resp.json().get("spaces", [])

            # Find best match: list > folder > space (exact match preferred)
            list_meta = {}
            best_list = None
            best_folder = None
            best_space = None
            for s in spaces:
                space_name = s.get("name", "")
                space_norm = normalize(space_name)
                if project_norm == space_norm:
                    best_space = s["id"]
                folder_resp = requests.get(
                    f"{BASE_URL}/space/{s['id']}/folder", headers=headers
                )
                if folder_resp.status_code == 200:
                    folders = folder_resp.json().get("folders", [])
                    for f in folders:
                        folder_name = f.get("name", "")
                        folder_norm = normalize(folder_name)
                        if project_norm == folder_norm:
                            best_folder = f["id"]
                        for lst in f.get("lists", []):
                            list_name = lst.get("name", "")
                            list_norm = normalize(list_name)
                            list_meta[lst["id"]] = (s["id"], space_name, folder_name)
                            if project_norm == list_norm:
                                best_list = lst["id"]

            # If no exact match, try partial (substring) match
            if not (best_list or best_folder or best_space):
                for s in spaces:
                    space_name = s.get("name", "")
                    space_norm = normalize(space_name)
                    if project_norm in space_norm:
                        best_space = s["id"]
                    folder_resp = requests.get(
                        f"{BASE_URL}/space/{s['id']}/folder", headers=headers
                    )
                    if folder_resp.status_code == 200:
                        folders = folder_resp.json().get("folders", [])
                        for f in folders:
                            folder_name = f.get("name", "")
                            folder_norm = normalize(folder_name)
                            if project_norm in folder_norm:
                                best_folder = f["id"]
                            for lst in f.get("lists", []):
                                list_name = lst.get("name", "")
                                list_norm = normalize(list_name)
                                list_meta[lst["id"]] = (
                                    s["id"],
                                    space_name,
                                    folder_name,
                                )
                                if project_norm in list_norm:
                                    best_list = lst["id"]

            # Only use the best match (prefer list > folder > space)
            target_list_ids = []
            if best_list:
                target_list_ids = [best_list]
            elif best_folder:
                # Fetch all lists in the matched folder
                for s in spaces:
                    folder_resp = requests.get(
                        f"{BASE_URL}/space/{s['id']}/folder", headers=headers
                    )
                    if folder_resp.status_code == 200:
                        folders = folder_resp.json().get("folders", [])
                        for f in folders:
                            if f["id"] == best_folder:
                                for lst in f.get("lists", []):
                                    target_list_ids.append(lst["id"])
            elif best_space:
                target_space_ids = [best_space]
            else:
                target_space_ids = []

            if not target_list_ids and not (
                "target_space_ids" in locals() and target_space_ids
            ):
                return {
                    "error": f"No matching space, folder, or list found for '{project}'",
                    "hint": "Check spelling/case. Available names: see ClickUp UI.",
                }

            all_tasks = []

            def fetch_from_url(url, base_params):
                page = 0
                while True:
                    current_params = base_params + [("page", str(page))]
                    resp = requests.get(url, headers=headers, params=current_params)
                    if resp.status_code != 200:
                        print(
                            f"[WARN] Failed {url} with params {current_params}: {resp.status_code} {resp.text}"
                        )
                        break
                    data = resp.json()
                    tasks = data.get("tasks", [])
                    if not tasks:
                        break
                    all_tasks.extend(tasks)
                    page += 1

            base_params = [("include_closed", str(include_closed).lower())]
            if statuses:
                for s in statuses:
                    base_params.append(("statuses[]", s))

            for lid in target_list_ids:
                fetch_from_url(f"{BASE_URL}/list/{lid}/task", base_params)
            if "target_space_ids" in locals() and target_space_ids:
                for sid in target_space_ids:
                    fetch_from_url(f"{BASE_URL}/space/{sid}/task", base_params)

            print(f"[DEBUG] Total tasks fetched: {len(all_tasks)}")

            # You can add your custom status mapping and summary logic here

            formatted_tasks = []
            for t in all_tasks:
                assignees = [
                    a.get("username") or f"User_{a.get('id')}"
                    for a in t.get("assignees", [])
                ]
                formatted_tasks.append(
                    {
                        "task_id": t.get("id"),
                        "name": t.get("name"),
                        "status": t.get("status", {}).get("status", "Unknown"),
                        "assignee": assignees,
                        "due_date": t.get("due_date"),
                        "list_name": t.get("list", {}).get("name"),
                        "folder_name": t.get("folder", {}).get("name"),
                        "space_name": get_space_name(t.get("space", {}).get("id")),
                    }
                )

            return {
                "project": project,
                "total_tasks": len(all_tasks),
                "tasks": formatted_tasks,
            }

        except Exception as e:
            print(f"[ERROR] get_project_tasks failed: {str(e)}")
            return {"error": str(e)}

    @mcp.tool
    def get_list_progress(list_id: str) -> dict:
        """
        Get progress summary for a list (useful for sprints).
        Returns: Progress metrics including completion rate, status breakdown, velocity.
        """
        import time

        try:
            headers = {
                "Authorization": CLICKUP_API_TOKEN,
                "Content-Type": "application/json",
            }

            # Fetch all tasks in the list (paginated)
            all_tasks = []
            page = 0
            while True:
                params = [
                    ("page", str(page)),
                    ("include_closed", "true"),
                ]
                resp = requests.get(
                    f"{BASE_URL}/list/{list_id}/task", headers=headers, params=params
                )
                if resp.status_code != 200:
                    return {
                        "error": f"Failed to fetch tasks: {resp.status_code} {resp.text}"
                    }
                data = resp.json()
                tasks = data.get("tasks", [])
                if not tasks:
                    break
                all_tasks.extend(tasks)
                page += 1

            total = len(all_tasks)
            if total == 0:
                return {"error": "No tasks found in this list."}

            # Status mapping for progress
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
            status_count = {}
            done_statuses = {"shipped"}
            # closed_statuses = {"cancelled"}
            completed = 0
            for t in all_tasks:
                status_name = t.get("status", {}).get("status", "Unknown")
                status_key = status_name.strip().lower()
                stage = status_stage_map.get(status_key, "active")
                stage_count[stage] += 1
                status_count[status_name] = status_count.get(status_name, 0) + 1
                if status_key in done_statuses:
                    completed += 1
            completion_rate = completed / total if total else 0

            # Velocity: tasks completed in the last 7 days (using date_done, date_closed, or date_updated)
            now = int(time.time() * 1000)
            week_ago = now - 7 * 24 * 60 * 60 * 1000
            completed_last_week = 0
            for t in all_tasks:
                status_name = t.get("status", {}).get("status", "Unknown")
                status_key = status_name.strip().lower()
                if status_key in done_statuses:
                    # Prefer date_done, then date_closed, then date_updated
                    done_ts = None
                    for field in ["date_done", "date_closed", "date_updated"]:
                        ts = t.get(field)
                        if ts and isinstance(ts, str) and ts.isdigit():
                            ts = int(ts)
                        if ts and isinstance(ts, int):
                            done_ts = ts
                            break
                    if done_ts and done_ts >= week_ago:
                        completed_last_week += 1

            velocity = completed_last_week

            return {
                "list_id": list_id,
                "total_tasks": total,
                "completion_rate": round(completion_rate, 3),
                "status_breakdown": status_count,
                "stage_breakdown": stage_count,
                "velocity_7d": velocity,
            }
        except Exception as e:
            return {"error": str(e)}
