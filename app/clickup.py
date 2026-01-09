import requests
from app.config import CLICKUP_API_TOKEN, CLICKUP_SPACE_ID, CLICKUP_TEAM_ID, BASE_URL


HEADERS = {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}


def fetch_lists_from_space():
    """
    Fetch all lists inside a space
    """
    url = f"{BASE_URL}/space/{CLICKUP_SPACE_ID}/list"

    response = requests.get(url, headers=HEADERS)

    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch lists: {response.status_code} {response.text}"
        )

    return response.json().get("lists", [])


def fetch_tasks_from_list(list_id: str):
    """
    Fetch all tasks from a ClickUp list
    """
    url = f"{BASE_URL}/list/{list_id}/task"

    params = {"archived": "false"}

    response = requests.get(url, headers=HEADERS, params=params)

    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch tasks for list {list_id}: {response.status_code} {response.text}"
        )

    return response.json().get("tasks", [])


def fetch_all_tasks_from_space(space_id: str):
    all_tasks = []

    # 1️⃣ Get lists in space
    lists_url = f"{BASE_URL}/space/{space_id}/list"
    lists_resp = requests.get(lists_url, headers=HEADERS)

    if lists_resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch lists: {lists_resp.text}")

    lists = lists_resp.json().get("lists", [])

    # 2️⃣ Fetch tasks from each list
    for lst in lists:
        list_id = lst["id"]
        page = 0

        while True:
            tasks_url = f"{BASE_URL}/list/{list_id}/task"
            params = {
                "page": page,
                "include_closed": "true",
                "archived": "false",
            }

            resp = requests.get(tasks_url, headers=HEADERS, params=params)

            if resp.status_code != 200:
                raise RuntimeError(f"Task fetch failed: {resp.text}")

            batch = resp.json().get("tasks", [])

            if not batch:
                break

            all_tasks.extend(batch)
            page += 1

    return all_tasks


def fetch_time_entries_for_task(task_id: str):
    """
    Fetch all time entries for a given ClickUp task
    """
    url = f"{BASE_URL}/task/{task_id}/time"

    response = requests.get(url, headers=HEADERS)

    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch time entries for task {task_id}: "
            f"{response.status_code} {response.text}"
        )

    return response.json().get("data", [])


def fetch_all_lists_in_space(space_id: str):
    lists = []

    # Projects = folders
    folders_res = requests.get(f"{BASE_URL}/space/{space_id}/folder", headers=HEADERS)
    folders_res.raise_for_status()

    for folder in folders_res.json().get("folders", []):
        for lst in folder.get("lists", []):
            lists.append(lst)

    # Standalone lists
    lists_res = requests.get(f"{BASE_URL}/space/{space_id}/list", headers=HEADERS)
    lists_res.raise_for_status()

    lists.extend(lists_res.json().get("lists", []))

    return lists


def fetch_time_entries():
    """
    Fetch ALL time entries for the team.
    ClickUp returns time in milliseconds.
    """
    url = f"{BASE_URL}/team/{CLICKUP_TEAM_ID}/time_entries"

    response = requests.get(url, headers=HEADERS)

    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch time entries: {response.status_code} {response.text}"
        )

    return response.json().get("data", [])
