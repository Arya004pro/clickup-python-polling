import requests
from functools import lru_cache
from typing import List, Dict, Optional

from app.config import (
    CLICKUP_API_TOKEN,
    CLICKUP_TEAM_ID,
    BASE_URL,
)

# =================================================
# Shared HTTP session
# =================================================
session = requests.Session()
session.headers.update(
    {
        "Authorization": CLICKUP_API_TOKEN,
        "Content-Type": "application/json",
    }
)


def _get(url: str, params: Optional[dict] = None) -> dict:
    resp = session.get(url, params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"ClickUp error {resp.status_code}: {resp.text}")
    return resp.json()


# =================================================
# LIST FETCHING (cached)
# =================================================
@lru_cache(maxsize=1)
def fetch_all_lists_in_space(space_id: str) -> List[Dict]:
    lists: List[Dict] = []

    folders = _get(f"{BASE_URL}/space/{space_id}/folder").get("folders", [])
    for folder in folders:
        lists.extend(folder.get("lists", []))

    standalone = _get(f"{BASE_URL}/space/{space_id}/list").get("lists", [])
    lists.extend(standalone)

    return lists


# =================================================
# TASK FETCHING (STABLE)
# =================================================
def fetch_tasks_from_list(
    list_id: str,
    updated_after_ms: Optional[int] = None,
) -> List[Dict]:
    all_tasks: List[Dict] = []
    page = 0

    while True:
        params = {
            "page": page,
            "include_closed": "true",
        }

        if updated_after_ms is not None:
            params["date_updated_gt"] = updated_after_ms

        data = _get(f"{BASE_URL}/list/{list_id}/task", params)
        tasks = data.get("tasks", [])

        if not tasks:
            break

        all_tasks.extend(tasks)
        page += 1

    return all_tasks


# =================================================
# PUBLIC API (USED BY scheduler & main)
# =================================================
def fetch_all_tasks_from_space(space_id: str) -> List[Dict]:
    all_tasks: List[Dict] = []

    lists = fetch_all_lists_in_space(space_id)
    for lst in lists:
        all_tasks.extend(fetch_tasks_from_list(lst["id"]))

    return all_tasks


def fetch_tasks_updated_since(
    space_id: str,
    updated_after_ms: int,
) -> List[Dict]:
    tasks: List[Dict] = []

    lists = fetch_all_lists_in_space(space_id)
    for lst in lists:
        tasks.extend(
            fetch_tasks_from_list(
                lst["id"],
                updated_after_ms=updated_after_ms,
            )
        )

    return tasks


# =================================================
# TIME TRACKING
# =================================================
def fetch_time_entries_for_task(task_id: str) -> List[Dict]:
    data = _get(f"{BASE_URL}/task/{task_id}/time")
    return data.get("data", [])


def fetch_time_entries() -> List[Dict]:
    data = _get(f"{BASE_URL}/team/{CLICKUP_TEAM_ID}/time_entries")
    return data.get("data", [])


# =================================================
# TEAM MEMBERS
# =================================================
def fetch_team_members() -> List[Dict]:
    data = _get(f"{BASE_URL}/team/{CLICKUP_TEAM_ID}")

    ROLE_MAP = {
        1: "owner",
        2: "admin",
        3: "member",
        4: "guest",
    }

    members: List[Dict] = []

    for member in data.get("team", {}).get("members", []):
        user = member.get("user", {})
        members.append(
            {
                "clickup_user_id": str(user.get("id")),
                "name": user.get("username"),
                "email": user.get("email"),
                "role": ROLE_MAP.get(user.get("role")),
            }
        )

    return members
