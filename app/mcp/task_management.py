# app/mcp/task_management.py

from fastmcp import FastMCP
import requests
from app.config import CLICKUP_API_TOKEN, BASE_URL


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
