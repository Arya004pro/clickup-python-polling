# app/mcp/task_management.py

from fastmcp import FastMCP
import requests
import json
from app.config import (
    CLICKUP_API_TOKEN,
    BASE_URL,
)  # only these two constants from config


def register_task_tools(mcp: FastMCP):
    # Helper for clean terminal output
    def pretty_json(data):
        return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)

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

        Parameters:
        - list_id (string, required): The list ID to get tasks from.
        - include_closed (boolean, optional, default: false): Include closed/completed tasks.
        - statuses (list[string], optional): Filter by status names.
        - assignees (list[number], optional): Filter by assignee user IDs.
        - page (number, optional): Page number for pagination (starts at 0).

        Returns: Formatted task list with only:
        - task_id
        - name
        - status
        - assignee (list of usernames, empty if none)
        - due_date (ISO string or null)
        """
        try:
            headers = {
                "Authorization": CLICKUP_API_TOKEN,
                "Content-Type": "application/json",
            }

            all_tasks = []
            current_page = page if page is not None else 0

            while True:
                params = {
                    "include_closed": str(include_closed).lower(),
                    "page": current_page,
                }
                if statuses:
                    params["statuses"] = ",".join(statuses)
                if assignees:
                    params["assignees"] = ",".join(map(str, assignees))

                print(
                    f"[DEBUG] Fetching page {current_page} for list {list_id} with params: {params}"
                )

                response = requests.get(
                    f"{BASE_URL}/list/{list_id}/task", headers=headers, params=params
                )

                if response.status_code != 200:
                    return [
                        {
                            "error": f"ClickUp API error {response.status_code}: {response.text}"
                        }
                    ]

                data = response.json()
                page_tasks = data.get("tasks", [])

                if not page_tasks:
                    break  # No more tasks

                all_tasks.extend(page_tasks)
                current_page += 1

                # If page param was given, stop after one page
                if page is not None:
                    break

            print(f"[DEBUG] Total tasks fetched: {len(all_tasks)}")

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
                        "assignee": assignee_usernames,  # list of usernames
                        "due_date": t.get("due_date"),
                    }
                )

            return formatted

        except Exception as e:
            print(f"[ERROR] get_tasks failed: {str(e)}")
            return [{"error": str(e)}]
