"""
ClickUp API Client - Enterprise Grade (Business/Enterprise Plans)
Optimized for 1,000-10,000 requests/minute
Target: 2-3 minutes for 3,000 tasks
"""

import requests
import time
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from app.config import CLICKUP_API_TOKEN, CLICKUP_TEAM_ID, BASE_URL

# ------------------------------------------------------------------
# Configuration - Adjust based on your plan
# ------------------------------------------------------------------
# Business Plan: 1,000 req/min
# Enterprise Plan: 10,000 req/min
# Set your rate limit here:
ENTERPRISE_RATE_LIMIT = 1000  # requests per minute

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
        error_msg = f"ClickUp API error {r.status_code}: {r.text}"
        raise RuntimeError(error_msg)
    return r.json()


# ------------------------------------------------------------------
# Enterprise Token Bucket Rate Limiter
# ------------------------------------------------------------------
class EnterpriseRateLimiter:
    """
    High-performance rate limiter for Business/Enterprise ClickUp plans.
    Supports 1,000+ requests per minute with intelligent bursting.
    """

    def __init__(self, requests_per_minute=1000):
        """
        Args:
            requests_per_minute: Your plan's rate limit (1000 or 10000)
        """
        self.rate_per_second = requests_per_minute / 60
        # Burst size = 10% of minute limit (allows fast starts)
        self.burst_size = int(requests_per_minute * 0.1)
        self.tokens = self.burst_size
        self.last_update = time.time()
        self.lock = Lock()

        # Statistics
        self.total_requests = 0
        self.total_waits = 0
        self.total_wait_time = 0

        print("🚀 Enterprise Rate Limiter initialized:")
        print(
            f"   Rate: {requests_per_minute:,} req/min ({self.rate_per_second:.1f} req/sec)"
        )
        print(f"   Burst: {self.burst_size} requests")

    def acquire(self, tokens=1):
        """
        Acquire tokens from bucket. Minimal blocking for max performance.
        """
        with self.lock:
            now = time.time()

            # Refill tokens based on time passed
            time_passed = now - self.last_update
            self.tokens = min(
                self.burst_size, self.tokens + time_passed * self.rate_per_second
            )
            self.last_update = now

            # If we have enough tokens, take them
            if self.tokens >= tokens:
                self.tokens -= tokens
                self.total_requests += tokens
                return 0

            # Calculate minimal wait time needed
            tokens_needed = tokens - self.tokens
            wait_time = tokens_needed / self.rate_per_second

            # Wait and take tokens
            if wait_time > 0:
                time.sleep(wait_time)
                self.total_waits += 1
                self.total_wait_time += wait_time

            self.tokens = 0
            self.last_update = time.time()
            self.total_requests += tokens
            return wait_time

    def get_stats(self):
        """Get performance statistics"""
        return {
            "total_requests": self.total_requests,
            "total_waits": self.total_waits,
            "total_wait_time": self.total_wait_time,
            "avg_wait": self.total_wait_time / self.total_waits
            if self.total_waits > 0
            else 0,
            "requests_per_sec": self.rate_per_second,
        }


# Initialize rate limiter with your plan's limit
rate_limiter = EnterpriseRateLimiter(requests_per_minute=ENTERPRISE_RATE_LIMIT)


# ------------------------------------------------------------------
# Spaces & Lists
# ------------------------------------------------------------------
@lru_cache(maxsize=1)
def fetch_all_spaces():
    team_id = CLICKUP_TEAM_ID
    if not team_id:
        data = _get(f"{BASE_URL}/team")
        teams = data.get("teams", [])
        if not teams:
            raise RuntimeError("No ClickUp teams found for the provided API token.")
        team_id = teams[0]["id"]
    spaces = _get(f"{BASE_URL}/team/{team_id}/space").get("spaces", [])
    print(f"📂 Found {len(spaces)} spaces")
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
            try:
                data = _get(f"{BASE_URL}/list/{list_id}/task", params).get("tasks", [])
            except Exception:
                break
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
        with ThreadPoolExecutor(max_workers=32) as ex:
            for f in as_completed(
                [ex.submit(fetch_tasks_from_list, lst["id"]) for lst in lists]
            ):
                all_tasks.extend(f.result())
    print(f"✅ Total tasks fetched: {len(all_tasks)}")
    return all_tasks


def fetch_all_tasks_updated_since_team(updated_after_ms):
    tasks = []
    for space in fetch_all_spaces():
        lists = fetch_all_lists_in_space(space["id"])
        with ThreadPoolExecutor(max_workers=32) as ex:
            for f in as_completed(
                [
                    ex.submit(fetch_tasks_from_list, lst["id"], updated_after_ms)
                    for lst in lists
                ]
            ):
                tasks.extend(f.result())
    return tasks


# ------------------------------------------------------------------
# Enterprise-Grade Time Entries (Batch Processing)
# ------------------------------------------------------------------
def fetch_time_entries_for_task(task_id):
    return _get(f"{BASE_URL}/task/{task_id}/time").get("data", [])


def fetch_all_time_entries_batch(task_ids):
    """
    Enterprise-grade batch processing for time entries.
    Optimized for Business/Enterprise plans with high rate limits.

    Performance targets:
    - Business Plan (1000 req/min): ~2-3 minutes for 3000 tasks
    - Enterprise Plan (10000 req/min): ~20-30 seconds for 3000 tasks
    """
    if not task_ids:
        return {}

    total = len(task_ids)
    result = {}
    processed = 0
    errors = 0
    start_time = time.time()

    # Calculate optimal worker count based on rate limit
    # Rule: workers = rate_per_second * 2 (allows for network latency)
    optimal_workers = min(200, int(rate_limiter.rate_per_second * 2))

    # Batch size for progress reporting
    batch_size = max(100, int(total / 20))  # Report 20 times during processing

    print(f"\n⏳ Fetching time entries for {total:,} tasks...")
    print(f"🚀 Enterprise mode: {optimal_workers} concurrent workers")
    print(
        f"⚡ Rate: {ENTERPRISE_RATE_LIMIT:,} req/min ({rate_limiter.rate_per_second:.1f} req/sec)"
    )
    print(f"🎯 Target: ~{total / rate_limiter.rate_per_second / 60:.1f} minutes\n")

    def fetch_with_retry(tid):
        """Fetch time entry with minimal retry overhead"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Acquire rate limit token
                rate_limiter.acquire(tokens=1)

                # Make API call
                data = _get(f"{BASE_URL}/task/{tid}/time").get("data", [])
                return tid, data, None
            except Exception as e:
                if "429" in str(
                    e
                ):  # Rate limit hit (shouldn't happen with proper limiting)
                    wait = min(30, 2**attempt * 3)
                    time.sleep(wait)
                    continue
                elif attempt < max_retries - 1:
                    time.sleep(0.5)
                    continue
                else:
                    return tid, [], str(e)
        return tid, [], "Max retries exceeded"

    # Process all tasks in parallel with high concurrency
    with ThreadPoolExecutor(max_workers=optimal_workers) as executor:
        futures = [executor.submit(fetch_with_retry, tid) for tid in task_ids]

        for future in as_completed(futures):
            tid, entries, error = future.result()
            result[tid] = entries
            processed += 1

            if error:
                errors += 1

            # Dynamic progress reporting
            if processed % batch_size == 0 or processed == total:
                elapsed = time.time() - start_time
                rate = processed / elapsed if elapsed > 0 else 0
                remaining = total - processed
                eta = (remaining / rate) if rate > 0 else 0
                progress = (processed / total) * 100

                print(
                    f"📊 [{processed:,}/{total:,}] {progress:.1f}% | "
                    f"⚡ {rate:.1f} tasks/sec | "
                    f"⏱️  ETA: {eta:.0f}s | "
                    f"❌ Errors: {errors}"
                )

    # Final statistics
    total_time = time.time() - start_time
    stats = rate_limiter.get_stats()

    print("\n✅ Time entries complete!")
    print(f"   📦 Total: {processed:,} tasks")
    print(f"   ⏱️  Time: {total_time:.1f}s ({total_time / 60:.2f} minutes)")
    print(f"   ⚡ Speed: {processed / total_time:.1f} tasks/second")
    print(f"   ❌ Errors: {errors}")
    print(f"   ⏸️  Total waits: {stats['total_waits']}")
    print(f"   ⏱️  Total wait time: {stats['total_wait_time']:.1f}s")

    return result


# ------------------------------------------------------------------
# Enterprise-Grade Comments (Batch Processing)
# ------------------------------------------------------------------
def fetch_assigned_comments_batch(task_ids):
    """
    Enterprise-grade batch processing for comments.
    """
    if not task_ids:
        return {}

    total = len(task_ids)
    result = {}
    processed = 0
    errors = 0
    start_time = time.time()

    optimal_workers = min(200, int(rate_limiter.rate_per_second * 2))
    batch_size = max(100, int(total / 20))

    print(f"\n💬 Fetching comments for {total:,} tasks...")
    print(f"🚀 Enterprise mode: {optimal_workers} concurrent workers")
    print(f"🎯 Target: ~{total / rate_limiter.rate_per_second / 60:.1f} minutes\n")

    def fetch_with_retry(tid):
        """Fetch comments with minimal retry overhead"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                rate_limiter.acquire(tokens=1)

                comments = _get(f"{BASE_URL}/task/{tid}/comment").get("comments", [])
                texts = [
                    c.get("comment_text", "").strip()
                    for c in comments
                    if c.get("assignee") and not c.get("resolved")
                ]
                return tid, " | ".join(filter(None, texts)) or None, None
            except Exception as e:
                if "429" in str(e):
                    wait = min(30, 2**attempt * 3)
                    time.sleep(wait)
                    continue
                elif attempt < max_retries - 1:
                    time.sleep(0.5)
                    continue
                else:
                    return tid, None, str(e)
        return tid, None, "Max retries exceeded"

    # Process all tasks in parallel
    with ThreadPoolExecutor(max_workers=optimal_workers) as executor:
        futures = [executor.submit(fetch_with_retry, tid) for tid in task_ids]

        for future in as_completed(futures):
            tid, comment, error = future.result()
            result[tid] = comment
            processed += 1

            if error:
                errors += 1

            if processed % batch_size == 0 or processed == total:
                elapsed = time.time() - start_time
                rate = processed / elapsed if elapsed > 0 else 0
                remaining = total - processed
                eta = (remaining / rate) if rate > 0 else 0
                progress = (processed / total) * 100

                print(
                    f"📊 [{processed:,}/{total:,}] {progress:.1f}% | "
                    f"⚡ {rate:.1f} tasks/sec | "
                    f"⏱️  ETA: {eta:.0f}s | "
                    f"❌ Errors: {errors}"
                )

    total_time = time.time() - start_time
    stats = rate_limiter.get_stats()

    print("\n✅ Comments complete!")
    print(f"   📦 Total: {processed:,} tasks")
    print(f"   ⏱️  Time: {total_time:.1f}s ({total_time / 60:.2f} minutes)")
    print(f"   ⚡ Speed: {processed / total_time:.1f} tasks/second")
    print(f"   ❌ Errors: {errors}")
    print(f"   ⏸️  Total waits: {stats['total_waits']}")
    print(f"   ⏱️  Total wait time: {stats['total_wait_time']:.1f}s")

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
