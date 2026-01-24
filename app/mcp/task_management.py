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
                    params.append(("statuses[]", s))  # key is "statuses[]"

            if assignees:
                for a in assignees:
                    params.append(("assignees[]", str(a)))

            if page is not None:
                params.append(("page", str(page)))

            print(f"[DEBUG] Fetching tasks for list {list_id} with params: {params}")

            all_tasks = []
            current_page = page if page is not None else 0

            while True:
                # Add current page to params
                params.append(("page", str(current_page)))
                response = requests.get(
                    f"{BASE_URL}/list/{list_id}/task", headers=headers, params=params
                )
                # Remove page for next iteration
                params.pop()

                if response.status_code != 200:
                    error_msg = (
                        f"ClickUp API error {response.status_code}: {response.text}"
                    )
                    print(f"[ERROR] {error_msg}")
                    return [{"error": error_msg}]

                data = response.json()
                page_tasks = data.get("tasks", [])

                if not page_tasks:
                    break

                all_tasks.extend(page_tasks)
                current_page += 1

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
                        "assignee": assignee_usernames,
                        "due_date": t.get("due_date"),
                    }
                )

            return formatted

        except Exception as e:
            print(f"[ERROR] get_tasks failed: {str(e)}")
            return [{"error": str(e)}]
