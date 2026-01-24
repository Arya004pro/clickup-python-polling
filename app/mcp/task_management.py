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
