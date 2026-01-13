"""
ClickUp API Client - Simplified
"""

import requests
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.config import CLICKUP_API_TOKEN, CLICKUP_TEAM_ID, BASE_URL

# Shared HTTP session
session = requests.Session()
session.headers.update(
    {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}
)


def _get(url, params=None):
    resp = session.get(url, params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"ClickUp error {resp.status_code}: {resp.text}")
    return resp.json()


# -----------------------------------------------------------------------------
# Spaces & Lists
# -----------------------------------------------------------------------------
@lru_cache(maxsize=1)
def fetch_all_spaces():
    spaces = _get(f"{BASE_URL}/team/{CLICKUP_TEAM_ID}/space").get("spaces", [])
    print(f"📂 Found {len(spaces)} spaces: {[s['name'] for s in spaces]}")
    return spaces


@lru_cache(maxsize=32)
def fetch_all_lists_in_space(space_id):
    lists = []
    for folder in _get(f"{BASE_URL}/space/{space_id}/folder").get("folders", []):
        lists.extend(folder.get("lists", []))
    lists.extend(_get(f"{BASE_URL}/space/{space_id}/list").get("lists", []))
    return lists


def clear_space_cache():
    fetch_all_spaces.cache_clear()
    fetch_all_lists_in_space.cache_clear()


# -----------------------------------------------------------------------------
# Task Fetching
# -----------------------------------------------------------------------------
def fetch_tasks_from_list(list_id, updated_after_ms=None, include_archived=True):
    all_tasks, seen = [], set()

    def fetch_pages(params_base):
        page = 0
        while True:
            params = {**params_base, "page": page, "include_closed": "true"}
            tasks = _get(f"{BASE_URL}/list/{list_id}/task", params).get("tasks", [])
            if not tasks:
                break
            for t in tasks:
                if t["id"] not in seen:
                    seen.add(t["id"])
                    all_tasks.append(t)
            page += 1

    # Active tasks
    fetch_pages(
        {
            "archived": "false",
            **({"date_updated_gt": updated_after_ms} if updated_after_ms else {}),
        }
    )

    # New tasks (by creation date)
    if updated_after_ms:
        fetch_pages({"archived": "false", "date_created_gt": updated_after_ms})

    # Archived tasks
    if include_archived:
        fetch_pages(
            {
                "archived": "true",
                **({"date_updated_gt": updated_after_ms} if updated_after_ms else {}),
            }
        )

    return all_tasks


def fetch_all_tasks_from_team():
    """Fetch all tasks from ALL spaces."""
    all_tasks = []
    for space in fetch_all_spaces():
        print(f"  → Fetching from space: {space['name']}")
        lists = fetch_all_lists_in_space(space["id"])
        with ThreadPoolExecutor(max_workers=5) as ex:
            for future in as_completed(
                [ex.submit(fetch_tasks_from_list, lst["id"]) for lst in lists]
            ):
                all_tasks.extend(future.result())
    return all_tasks


def fetch_all_tasks_updated_since_team(updated_after_ms):
    """Fetch updated tasks from ALL spaces."""
    all_tasks = []
    for space in fetch_all_spaces():
        for lst in fetch_all_lists_in_space(space["id"]):
            all_tasks.extend(fetch_tasks_from_list(lst["id"], updated_after_ms))
    return all_tasks


# -----------------------------------------------------------------------------
# Time Entries (Batch)
# -----------------------------------------------------------------------------
def fetch_time_entries_for_task(task_id):
    return _get(f"{BASE_URL}/task/{task_id}/time").get("data", [])


def fetch_all_time_entries_batch(task_ids):
    """Fetch time entries for multiple tasks concurrently."""
    result = {tid: [] for tid in task_ids}

    def fetch(tid):
        try:
            return tid, _get(f"{BASE_URL}/task/{tid}/time").get("data", [])
        except Exception:
            return tid, []

    with ThreadPoolExecutor(max_workers=10) as ex:
        for future in as_completed([ex.submit(fetch, tid) for tid in task_ids]):
            tid, entries = future.result()
            result[tid] = entries
    return result


# -----------------------------------------------------------------------------
# Assigned Comments (Batch)
# -----------------------------------------------------------------------------
def fetch_assigned_comments_batch(task_ids):
    """Fetch assigned comments for multiple tasks concurrently."""
    result = {}

    def fetch(tid):
        try:
            comments = [
                c.get("comment_text", "").strip()
                for c in _get(f"{BASE_URL}/task/{tid}/comment").get("comments", [])
                if c.get("assignee") and not c.get("resolved")
            ]
            return tid, " | ".join(filter(None, comments)) or None
        except Exception:
            return tid, None

    with ThreadPoolExecutor(max_workers=10) as ex:
        for future in as_completed([ex.submit(fetch, tid) for tid in task_ids]):
            tid, comment = future.result()
            result[tid] = comment
    return result


# -----------------------------------------------------------------------------
# Team Members
# -----------------------------------------------------------------------------
def fetch_team_members():
    ROLES = {1: "owner", 2: "admin", 3: "member", 4: "guest"}
    members = []
    for m in (
        _get(f"{BASE_URL}/team/{CLICKUP_TEAM_ID}").get("team", {}).get("members", [])
    ):
        u = m.get("user", {})
        members.append(
            {
                "clickup_user_id": str(u.get("id")),
                "name": u.get("username"),
                "email": u.get("email"),
                "role": ROLES.get(u.get("role")),
            }
        )
    return members
