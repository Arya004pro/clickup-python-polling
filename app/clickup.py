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
session.headers.update(
    {
        "Authorization": CLICKUP_API_TOKEN,
        "Content-Type": "application/json",
    }
)


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
    team_id = CLICKUP_TEAM_ID
    if not team_id:
        # Fetch all teams and use the first one if not set
        data = _get(f"{BASE_URL}/team")
        teams = data.get("teams", [])
        if not teams:
            raise RuntimeError("No ClickUp teams found for the provided API token.")
        team_id = teams[0]["id"]
    spaces = _get(f"{BASE_URL}/team/{team_id}/space").get("spaces", [])
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
        with ThreadPoolExecutor(max_workers=12) as ex:  # Increased for faster full sync
            for f in as_completed(
                [ex.submit(fetch_tasks_from_list, lst["id"]) for lst in lists]
            ):
                all_tasks.extend(f.result())
    return all_tasks


def fetch_all_tasks_updated_since_team(updated_after_ms):
    tasks = []
    for space in fetch_all_spaces():
        print(f"  → Fetching from space: {space['name']}")
        lists = fetch_all_lists_in_space(space["id"])
        with ThreadPoolExecutor(max_workers=3) as ex:
            for f in as_completed(
                [
                    ex.submit(fetch_tasks_from_list, lst["id"], updated_after_ms)
                    for lst in lists
                ]
            ):
                tasks.extend(f.result())
    return tasks


# ------------------------------------------------------------------
# Time Entries
# ------------------------------------------------------------------
def fetch_time_entries_for_task(task_id):
    return _get(f"{BASE_URL}/task/{task_id}/time").get("data", [])


def fetch_all_time_entries_batch(task_ids):
    import time

    result = {tid: [] for tid in task_ids}
<<<<<<< HEAD
    BATCH_SIZE = 200  # Further increased for maximum speed
=======
    BATCH_SIZE = 500
    DELAY_BETWEEN_BATCHES = 0.5  # seconds (keep low for speed)
>>>>>>> 994eaba6753de9c5b9aa7c7c06ad18dcd3cf0319
    MAX_RETRIES = 3

    def fetch(tid):
        retries = 0
        while retries < MAX_RETRIES:
            try:
                data = _get(f"{BASE_URL}/task/{tid}/time").get("data", [])
                return tid, data
            except Exception as e:
                if (
                    hasattr(e, "response")
                    and getattr(e.response, "status_code", None) == 429
                ):
                    wait = 2**retries
                    time.sleep(wait)
                    retries += 1
                else:
                    return tid, []
        return tid, []

    for i in range(0, len(task_ids), BATCH_SIZE):
        batch = task_ids[i : i + BATCH_SIZE]
<<<<<<< HEAD
        with ThreadPoolExecutor(
            max_workers=20
        ) as ex:  # Increased to 20 for more parallelism
            for f in as_completed([ex.submit(fetch, tid) for tid in batch]):
                tid, entries = f.result()
                result[tid] = entries
        # No delay between batches for maximum speed
=======
        with ThreadPoolExecutor(max_workers=50) as ex:
            for f in as_completed([ex.submit(fetch, tid) for tid in batch]):
                tid, entries = f.result()
                result[tid] = entries
        if i + BATCH_SIZE < len(task_ids):
            time.sleep(DELAY_BETWEEN_BATCHES)
>>>>>>> 994eaba6753de9c5b9aa7c7c06ad18dcd3cf0319
    return result


# ------------------------------------------------------------------
# Assigned Comments
# ------------------------------------------------------------------
def fetch_assigned_comments_batch(task_ids):
    import time

    result = {}
<<<<<<< HEAD
    BATCH_SIZE = 100  # Increased for faster processing
    DELAY_BETWEEN_BATCHES = 0.2  # Reduced delay
=======
    BATCH_SIZE = 500
    DELAY_BETWEEN_BATCHES = 0.5  # seconds (keep low for speed)
>>>>>>> 994eaba6753de9c5b9aa7c7c06ad18dcd3cf0319
    MAX_RETRIES = 3

    def fetch(tid):
        retries = 0
        while retries < MAX_RETRIES:
            try:
                comments = _get(f"{BASE_URL}/task/{tid}/comment").get("comments", [])
                texts = [
                    c.get("comment_text", "").strip()
                    for c in comments
                    if c.get("assignee") and not c.get("resolved")
                ]
                return tid, " | ".join(filter(None, texts)) or None
            except Exception as e:
                if (
                    hasattr(e, "response")
                    and getattr(e.response, "status_code", None) == 429
                ):
                    wait = 2**retries
                    time.sleep(wait)
                    retries += 1
                else:
                    return tid, None
        return tid, None

    for i in range(0, len(task_ids), BATCH_SIZE):
        batch = task_ids[i : i + BATCH_SIZE]
<<<<<<< HEAD
        with ThreadPoolExecutor(max_workers=12) as ex:  # Increased workers
=======
        with ThreadPoolExecutor(max_workers=50) as ex:
>>>>>>> 994eaba6753de9c5b9aa7c7c06ad18dcd3cf0319
            for f in as_completed([ex.submit(fetch, tid) for tid in batch]):
                tid, comment = f.result()
                result[tid] = comment
        if i + BATCH_SIZE < len(task_ids):
            time.sleep(DELAY_BETWEEN_BATCHES)
    return result


# ------------------------------------------------------------------
# Team Members
# ------------------------------------------------------------------
def fetch_team_members():
    ROLES = {1: "owner", 2: "admin", 3: "member", 4: "guest"}
    members = []

    team_id = CLICKUP_TEAM_ID
    if not team_id:
        data = _get(f"{BASE_URL}/team")
        teams = data.get("teams", [])
        if not teams:
            raise RuntimeError("No ClickUp teams found for the provided API token.")
        team_id = teams[0]["id"]
    data = _get(f"{BASE_URL}/team/{team_id}")
    for m in data.get("team", {}).get("members", []):
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
