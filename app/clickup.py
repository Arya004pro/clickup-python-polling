"""
ClickUp API Client
"""

import requests
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.config import CLICKUP_API_TOKEN, CLICKUP_TEAM_ID, BASE_URL

# ------------------------------------------------------------------
# Session
# ------------------------------------------------------------------
session = requests.Session()
session.headers.update({
    "Authorization": CLICKUP_API_TOKEN,
    "Content-Type": "application/json",
})


def _get(url, params=None):
    r = session.get(url, params=params)
    if r.status_code != 200:
        raise RuntimeError(f"ClickUp error {r.status_code}: {r.text}")
    return r.json()


# ------------------------------------------------------------------
# Spaces & Lists
# ------------------------------------------------------------------
@lru_cache(maxsize=1)
def fetch_all_spaces():
    spaces = _get(f"{BASE_URL}/team/{CLICKUP_TEAM_ID}/space").get("spaces", [])
    print(f"📂 Found {len(spaces)} spaces: {[s['name'] for s in spaces]}")
    return spaces


@lru_cache(maxsize=32)
def fetch_all_lists_in_space(space_id):
    lists = []
    for f in _get(f"{BASE_URL}/space/{space_id}/folder").get("folders", []):
        lists.extend(f.get("lists", []))
    lists.extend(_get(f"{BASE_URL}/space/{space_id}/list").get("lists", []))
    return lists


def clear_space_cache():
    fetch_all_spaces.cache_clear()
    fetch_all_lists_in_space.cache_clear()


# ------------------------------------------------------------------
# Task Fetching
# ------------------------------------------------------------------
def fetch_tasks_from_list(list_id, updated_after_ms=None, include_archived=True):
    tasks, seen = [], set()
    page = 0

    def fetch_pages(extra):
        nonlocal page
        page = 0
        while True:
            params = {
                "page": page,
                "include_closed": "true",
                **extra,
            }
            data = _get(f"{BASE_URL}/list/{list_id}/task", params).get("tasks", [])
            if not data:
                break
            for t in data:
                if t["id"] not in seen:
                    seen.add(t["id"])
                    tasks.append(t)
            page += 1

    base = {"archived": "false"}
    if updated_after_ms:
        base["date_updated_gt"] = updated_after_ms

    fetch_pages(base)

    if updated_after_ms:
        fetch_pages({"archived": "false", "date_created_gt": updated_after_ms})

    if include_archived:
        arch = {"archived": "true"}
        if updated_after_ms:
            arch["date_updated_gt"] = updated_after_ms
        fetch_pages(arch)

    return tasks


def fetch_all_tasks_from_team():
    all_tasks = []
    for space in fetch_all_spaces():
        print(f"  → Fetching from space: {space['name']}")
        lists = fetch_all_lists_in_space(space["id"])
        with ThreadPoolExecutor(max_workers=5) as ex:
            for f in as_completed(
                [ex.submit(fetch_tasks_from_list, lst["id"]) for lst in lists]
            ):
                all_tasks.extend(f.result())
    return all_tasks


def fetch_all_tasks_updated_since_team(updated_after_ms):
    tasks = []
    for space in fetch_all_spaces():
        for lst in fetch_all_lists_in_space(space["id"]):
            tasks.extend(fetch_tasks_from_list(lst["id"], updated_after_ms))
    return tasks


# ------------------------------------------------------------------
# Time Entries
# ------------------------------------------------------------------
def fetch_time_entries_for_task(task_id):
    return _get(f"{BASE_URL}/task/{task_id}/time").get("data", [])


def fetch_all_time_entries_batch(task_ids):
    result = {tid: [] for tid in task_ids}

    def fetch(tid):
        try:
            return tid, _get(f"{BASE_URL}/task/{tid}/time").get("data", [])
        except Exception:
            return tid, []

    with ThreadPoolExecutor(max_workers=10) as ex:
        for f in as_completed([ex.submit(fetch, tid) for tid in task_ids]):
            tid, entries = f.result()
            result[tid] = entries
    return result


# ------------------------------------------------------------------
# Assigned Comments
# ------------------------------------------------------------------
def fetch_assigned_comments_batch(task_ids):
    result = {}

    def fetch(tid):
        try:
            comments = _get(f"{BASE_URL}/task/{tid}/comment").get("comments", [])
            texts = [
                c.get("comment_text", "").strip()
                for c in comments
                if c.get("assignee") and not c.get("resolved")
            ]
            return tid, " | ".join(filter(None, texts)) or None
        except Exception:
            return tid, None

    with ThreadPoolExecutor(max_workers=10) as ex:
        for f in as_completed([ex.submit(fetch, tid) for tid in task_ids]):
            tid, comment = f.result()
            result[tid] = comment
    return result


# ------------------------------------------------------------------
# Team Members
# ------------------------------------------------------------------
def fetch_team_members():
    ROLES = {1: "owner", 2: "admin", 3: "member", 4: "guest"}
    members = []

    data = _get(f"{BASE_URL}/team/{CLICKUP_TEAM_ID}")
    for m in data.get("team", {}).get("members", []):
        u = m.get("user", {})
        members.append({
            "clickup_user_id": str(u.get("id")),
            "name": u.get("username"),
            "email": u.get("email"),
            "role": ROLES.get(u.get("role")),
        })
    return members
