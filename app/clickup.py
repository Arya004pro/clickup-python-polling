import requests
from functools import lru_cache
from typing import List, Dict

from app.config import (
    CLICKUP_API_TOKEN,
    CLICKUP_TEAM_ID,
    BASE_URL,
)

# -------------------------
# Shared session (IMPORTANT)
# -------------------------
session = requests.Session()
session.headers.update(
    {
        "Authorization": CLICKUP_API_TOKEN,
        "Content-Type": "application/json",
    }
)


def _get(url: str, params: dict | None = None) -> dict:
    """
    Centralized GET with error handling
    """
    resp = session.get(url, params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"ClickUp error {resp.status_code}: {resp.text}")
    return resp.json()


# -------------------------------------------------
# LIST FETCHING (cached – lists rarely change)
# -------------------------------------------------
@lru_cache(maxsize=1)
def fetch_all_lists_in_space(space_id: str) -> List[Dict]:
    """
    Fetch ALL lists in a space (folders + standalone).
    Cached for performance.
    """
    lists: List[Dict] = []

    # Folder lists (projects)
    folders = _get(f"{BASE_URL}/space/{space_id}/folder").get("folders", [])
    for folder in folders:
        lists.extend(folder.get("lists", []))

    # Standalone lists
    standalone = _get(f"{BASE_URL}/space/{space_id}/list").get("lists", [])
    lists.extend(standalone)

    return lists


# -------------------------------------------------
# TASK FETCHING (incremental friendly)
# -------------------------------------------------
def fetch_tasks_from_list(
    list_id: str, updated_after_ms: int | None = None
) -> List[Dict]:
    """
    Fetch tasks from a list (supports incremental sync).
    """
    all_tasks: List[Dict] = []
    page = 0

    while True:
        params = {
            "page": page,
            "include_closed": "true",
            "archived": "false",
        }

        if updated_after_ms:
            params["date_updated_gt"] = updated_after_ms

        data = _get(f"{BASE_URL}/list/{list_id}/task", params)
        tasks = data.get("tasks", [])

        if not tasks:
            break

        all_tasks.extend(tasks)
        page += 1

    return all_tasks


# -------------------------------------------------
# PUBLIC API (⚠️ REQUIRED by main.py & scheduler)
# -------------------------------------------------
def fetch_all_tasks_from_space(space_id: str) -> List[Dict]:
    """
    Phase-2 public wrapper.
    Keeps backward compatibility with main.py & scheduler.
    """
    all_tasks: List[Dict] = []

    lists = fetch_all_lists_in_space(space_id)

    for lst in lists:
        all_tasks.extend(fetch_tasks_from_list(lst["id"]))

    return all_tasks


def fetch_tasks_updated_since(space_id: str, updated_after_ms: int) -> List[Dict]:
    """
    Phase-2 optimized:
    Fetch ONLY tasks updated after a timestamp.
    (Ready for Phase-3 incremental sync)
    """
    tasks: List[Dict] = []

    lists = fetch_all_lists_in_space(space_id)

    for lst in lists:
        tasks.extend(
            fetch_tasks_from_list(lst["id"], updated_after_ms=updated_after_ms)
        )

    return tasks


# -------------------------------------------------
# TIME TRACKING (task-scoped, accurate)
# -------------------------------------------------
def fetch_time_entries_for_task(task_id: str) -> List[Dict]:
    """
    Fetch interval-based time entries for ONE task.
    """
    data = _get(f"{BASE_URL}/task/{task_id}/time")
    return data.get("data", [])


# -------------------------------------------------
# TEAM-LEVEL TIME (kept for compatibility)
# -------------------------------------------------
def fetch_time_entries() -> List[Dict]:
    """
    Fetch ALL team time entries.
    ⚠️ Expensive – avoid in Phase-2 sync.
    """
    data = _get(f"{BASE_URL}/team/{CLICKUP_TEAM_ID}/time_entries")
    return data.get("data", [])
