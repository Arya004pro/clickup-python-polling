import requests
from functools import lru_cache
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

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
# LIST FETCHING (cached per space)
# =================================================
@lru_cache(maxsize=32)
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
    include_archived: bool = True,
):
    all_tasks = []
    seen_ids = set()

    def add_tasks(tasks_list):
        for t in tasks_list:
            if t["id"] not in seen_ids:
                seen_ids.add(t["id"])
                all_tasks.append(t)

    # Fetch non-archived tasks
    page = 0
    while True:
        params = {
            "page": page,
            "include_closed": "true",
            "archived": "false",
        }
        if updated_after_ms is not None:
            params["date_updated_gt"] = updated_after_ms

        data = _get(f"{BASE_URL}/list/{list_id}/task", params)
        tasks = data.get("tasks", [])
        if not tasks:
            break
        add_tasks(tasks)
        page += 1

    # Also fetch by date_created_gt for new tasks (deduped)
    if updated_after_ms is not None:
        page = 0
        while True:
            params = {
                "page": page,
                "include_closed": "true",
                "archived": "false",
                "date_created_gt": updated_after_ms,
            }
            data = _get(f"{BASE_URL}/list/{list_id}/task", params)
            tasks = data.get("tasks", [])
            if not tasks:
                break
            add_tasks(tasks)
            page += 1

    # Fetch archived tasks separately
    if include_archived:
        page = 0
        while True:
            params = {
                "page": page,
                "include_closed": "true",
                "archived": "true",
            }
            if updated_after_ms is not None:
                params["date_updated_gt"] = updated_after_ms

            data = _get(f"{BASE_URL}/list/{list_id}/task", params)
            tasks = data.get("tasks", [])
            if not tasks:
                break
            add_tasks(tasks)
            page += 1

    return all_tasks


# =================================================
# SPACE FETCHING
# =================================================
@lru_cache(maxsize=1)
def fetch_all_spaces() -> List[Dict]:
    """Fetch all spaces in the team."""
    data = _get(f"{BASE_URL}/team/{CLICKUP_TEAM_ID}/space")
    spaces = data.get("spaces", [])
    print(f"📂 Found {len(spaces)} spaces: {[s['name'] for s in spaces]}")
    return spaces


def clear_space_cache():
    """Clear cached spaces and lists (call when new space is created)."""
    fetch_all_spaces.cache_clear()
    fetch_all_lists_in_space.cache_clear()


# =================================================
# PUBLIC API (USED BY scheduler & main)
# =================================================
def fetch_all_tasks_from_space(space_id: str) -> List[Dict]:
    all_tasks: List[Dict] = []
    lists = fetch_all_lists_in_space(space_id)

    # Fetch from all lists concurrently
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(fetch_tasks_from_list, lst["id"]): lst for lst in lists
        }
        for future in as_completed(futures):
            all_tasks.extend(future.result())

    return all_tasks


def fetch_all_tasks_from_team() -> List[Dict]:
    """Fetch tasks from ALL spaces in the team."""
    all_tasks: List[Dict] = []
    spaces = fetch_all_spaces()

    for space in spaces:
        space_id = space["id"]
        space_name = space["name"]
        print(f"  → Fetching from space: {space_name}")
        tasks = fetch_all_tasks_from_space(space_id)
        all_tasks.extend(tasks)

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


def fetch_all_tasks_updated_since_team(updated_after_ms: int) -> List[Dict]:
    """Fetch updated tasks from ALL spaces."""
    all_tasks: List[Dict] = []
    spaces = fetch_all_spaces()

    for space in spaces:
        tasks = fetch_tasks_updated_since(space["id"], updated_after_ms)
        all_tasks.extend(tasks)

    return all_tasks


# =================================================
# TIME TRACKING
# =================================================
def fetch_time_entries_for_task(task_id: str) -> List[Dict]:
    data = _get(f"{BASE_URL}/task/{task_id}/time")
    return data.get("data", [])


def fetch_time_entries() -> List[Dict]:
    data = _get(f"{BASE_URL}/team/{CLICKUP_TEAM_ID}/time_entries")
    return data.get("data", [])


def fetch_all_time_entries_batch(task_ids: List[str]) -> Dict[str, List[Dict]]:
    """
    Fetch time entries for multiple tasks concurrently.
    Returns dict: task_id -> list of time entries
    """
    result: Dict[str, List[Dict]] = {tid: [] for tid in task_ids}

    def fetch_one(task_id: str):
        try:
            return task_id, _get(f"{BASE_URL}/task/{task_id}/time").get("data", [])
        except Exception:
            return task_id, []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_one, tid) for tid in task_ids]
        for future in as_completed(futures):
            task_id, entries = future.result()
            result[task_id] = entries

    return result


# =================================================
# ASSIGNED COMMENTS
# =================================================
def fetch_assigned_comment(task_id: str) -> Optional[str]:
    """
    Fetch all unresolved assigned comments for a task.
    Returns comments joined by ' | ' or None.
    """
    try:
        data = _get(f"{BASE_URL}/task/{task_id}/comment")
        comments = []
        for c in data.get("comments", []):
            if c.get("assignee") and not c.get("resolved"):
                text = c.get("comment_text", "").strip()
                if text:
                    comments.append(text)
        return " | ".join(comments) if comments else None
    except RuntimeError:
        pass
    return None


def fetch_assigned_comments_batch(task_ids: List[str]) -> Dict[str, Optional[str]]:
    """
    Fetch assigned comments for multiple tasks concurrently.
    Returns dict: task_id -> comment text (or None)
    """
    result: Dict[str, Optional[str]] = {}

    def fetch_one(task_id: str):
        return task_id, fetch_assigned_comment(task_id)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_one, tid) for tid in task_ids]
        for future in as_completed(futures):
            task_id, comment = future.result()
            result[task_id] = comment

    return result


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
