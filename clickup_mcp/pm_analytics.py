"""
PM Analytics Module for ClickUp MCP Server - FINAL REFACTOR
Features:
1. Deep Nesting Fix: Uses 'subtasks=true' to fetch all levels flattened.
2. Centralized Math: '_calculate_task_metrics' helper powers ALL reports.
3. Estimation Accuracy: Properly implemented using bottom-up sums.
4. Robust Status Logic: Identifies 'Shipped', 'Release', etc. as DONE.
5. Complete Toolset: Includes all analytics, breakdowns, and risk assessments.
"""

from __future__ import annotations
from fastmcp import FastMCP
import sys
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
from app.config import BASE_URL
from .status_helpers import (
    get_current_week_dates,
    date_range_to_timestamps,
    filter_time_entries_by_date_range,
    is_valid_monday_sunday_range,
    parse_week_input,
    validate_week_dates,
)
from .api_client import client as _client


# --- Standardized Status Logic ---
STATUS_NAME_OVERRIDES = {
    "not_started": [
        "BACKLOG",
        "QUEUED",
        "QUEUE",
        "IN QUEUE",
        "TO DO",
        "TO-DO",
        "PENDING",
        "OPEN",
        "IN PLANNING",
    ],
    "active": [
        "SCOPING",
        "IN DESIGN",
        "DEV",
        "IN DEVELOPMENT",
        "DEVELOPMENT",
        "REVIEW",
        "IN REVIEW",
        "TESTING",
        "QA",
        "BUG",
        "BLOCKED",
        "WAITING",
        "STAGING DEPLOY",
        "READY FOR DEVELOPMENT",
        "READY FOR PRODUCTION",
        "IN PROGRESS",
        "ON HOLD",
    ],
    "done": ["SHIPPED", "RELEASE", "COMPLETE", "DONE", "RESOLVED", "PROD", "QC CHECK"],
    "closed": ["CANCELLED", "CLOSED"],
}

STATUS_OVERRIDE_MAP = {
    s.upper(): cat for cat, statuses in STATUS_NAME_OVERRIDES.items() for s in statuses
}


def get_status_category(status_name: str, status_type: str = None) -> str:
    if not status_name:
        return "other"
    if cat := STATUS_OVERRIDE_MAP.get(status_name.upper()):
        return cat
    if status_type:
        type_map = {
            "open": "not_started",
            "done": "done",
            "closed": "closed",
            "custom": "active",
        }
        return type_map.get(status_type.lower(), "other")
    return "other"


# --- API & Data Helpers ---


def _api_call(
    method: str, endpoint: str, params: Optional[Dict] = None, timeout: int = 30
):
    """API call using shared session with connection pooling."""
    m = method.upper()
    if m == "GET":
        return _client.get(endpoint, params=params, timeout=timeout)
    elif m == "POST":
        return _client.post(endpoint, params=params, timeout=timeout)
    elif m == "PUT":
        return _client.put(endpoint, timeout=timeout)
    return None, f"Unsupported method: {method}"


def _get_team_id() -> str:
    """Get team ID using shared client (cached)."""
    return _client.get_team_id()


def _fetch_time_entries_smart(
    task_ids: list, start_ms: int = 0, end_ms: int = 0
) -> dict:
    """
    Fetch time entries for all tasks concurrently.

    Uses _client's pooled requests.Session (pool_connections=60,
    pool_maxsize=80) and _client's rate limiter — the same stack optimized
    in api_client.py — so TCP/TLS connections are reused across all workers.

    Worker count stays capped at 60, leaving pool headroom for retries and
    occasional concurrent background API calls.

    start_ms / end_ms are forwarded to the ClickUp API when provided so that
    only entries inside the requested period are returned, greatly reducing
    payload size for large spaces.
    """
    if not task_ids:
        return {}

    total = len(task_ids)
    result: dict = {}
    errors = 0
    processed = 0
    start_time = time.time()
    log_every = max(100, total // 20)

    # Build optional date range query params
    time_params: dict = {}
    if start_ms:
        time_params["start_date"] = start_ms
    if end_ms:
        time_params["end_date"] = end_ms

    # Workers are capped at 60; api_client keeps a larger pool (80) so
    # retries/background calls do not force overflow/discard behavior.
    workers = min(60, total)

    print(
        f"[DEBUG] Fetching time entries for {total:,} tasks... "
        f"({workers} workers, pooled session)"
    )
    sys.stdout.flush()

    import random

    def _is_ssl_error(exc: Exception) -> bool:
        """Return True for SSL/connection-reset errors that are safe to retry."""
        s = str(exc).lower()
        return any(
            kw in s
            for kw in (
                "ssl",
                "eof occurred",
                "connection reset",
                "connection aborted",
                "broken pipe",
            )
        )

    def _fetch_one(tid):
        max_attempts = 6
        for attempt in range(max_attempts):
            try:
                _client.limiter.acquire()
                r = _client.session.get(
                    f"{BASE_URL}/task/{tid}/time",
                    params=time_params if time_params else None,
                    timeout=20,
                )
                if r.status_code == 200:
                    return tid, r.json().get("data", []), None
                if r.status_code == 429:
                    time.sleep(min(60, 2**attempt * 3))
                    continue
                return tid, [], f"HTTP {r.status_code}"
            except Exception as exc:
                if attempt < max_attempts - 1:
                    # SSL / connection errors: longer exponential backoff
                    # with jitter to avoid thundering herd when many workers
                    # hit a connection drop at the same time.
                    if _is_ssl_error(exc):
                        base = 2**attempt * 2  # 2, 4, 8, 16, 32 …
                        jitter = random.uniform(0, base * 0.5)
                        delay = min(base + jitter, 45)
                    else:
                        delay = 0.5 * (attempt + 1)
                    time.sleep(delay)
                else:
                    return tid, [], str(exc)
        return tid, [], "Max retries exceeded"

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_fetch_one, t) for t in task_ids]
        for fut in as_completed(futures):
            tid, entries, err = fut.result()
            result[tid] = entries
            processed += 1
            if err:
                errors += 1
            if processed % log_every == 0 or processed == total:
                elapsed = time.time() - start_time
                rate = processed / elapsed if elapsed > 0 else 0
                eta = (total - processed) / rate if rate > 0 else 0
                print(
                    f"[PROGRESS] [{processed:,}/{total:,}] {processed / total * 100:.0f}% | "
                    f"{rate:.1f} tasks/sec | ETA: {eta:.0f}s | errors: {errors}"
                )
                sys.stdout.flush()

    elapsed = time.time() - start_time
    stats = _client.limiter.stats()
    print(
        f"[DEBUG] Time entry fetch done: {processed:,} tasks in {elapsed:.1f}s "
        f"({processed / elapsed:.1f}/sec) | errors: {errors} | "
        f"rate-limiter waits: {stats['total_waits']} ({stats['total_wait_time']:.1f}s)"
    )
    sys.stdout.flush()
    return result


def _resolve_to_list_ids(project: Optional[str], list_id: Optional[str]) -> List[str]:
    """
    Resolve project name to list IDs using universal search.
    Works for spaces, folders, lists anywhere in the hierarchy.

    Priority:
    1. Direct list_id if provided
    2. Universal entity search (finds spaces/folders/lists anywhere)
    3. Fallback to live API search
    """
    if list_id:
        return [list_id]
    if not project:
        return []

    try:
        from .sync_mapping import find_entity_anywhere

        # Use universal finder
        result = find_entity_anywhere(project)

        if result:
            entity_type = result["type"]
            entity_id = result["id"]

            print(
                f"[DEBUG] Found '{project}' as {entity_type} (ID: {entity_id}) via universal search"
            )

            if entity_type == "list":
                return [entity_id]

            elif entity_type == "folder":
                # Get all lists in the folder
                resp, err = _api_call("GET", f"/folder/{entity_id}/list")
                if resp and resp.get("lists"):
                    list_ids = [lst["id"] for lst in resp["lists"]]
                    print(f"[DEBUG] Found {len(list_ids)} lists in folder")
                    return list_ids

                # Fallback to cached structure
                structure = result.get("structure", {})
                cached_lists = [
                    c["id"]
                    for c in structure.get("children", [])
                    if c.get("type") == "list"
                ]
                if cached_lists:
                    print(
                        f"[DEBUG] Using {len(cached_lists)} lists from cached structure"
                    )
                    return cached_lists

            elif entity_type == "space":
                # Get all lists in the space (both folderless and in folders)
                ids = []

                # Folderless lists
                resp, _ = _api_call("GET", f"/space/{entity_id}/list")
                if resp:
                    ids.extend([lst["id"] for lst in resp.get("lists", [])])

                # Lists in folders
                resp2, _ = _api_call("GET", f"/space/{entity_id}/folder")
                if resp2:
                    for f in resp2.get("folders", []):
                        folder_lists, _ = _api_call("GET", f"/folder/{f['id']}/list")
                        if folder_lists:
                            ids.extend(
                                [lst["id"] for lst in folder_lists.get("lists", [])]
                            )

                print(f"[DEBUG] Found {len(ids)} total lists in space")
                return ids

    except Exception as e:
        print(f"[DEBUG] Universal search failed: {e}, falling back to legacy method")

    # Legacy fallback - try project_map.json directly
    try:
        from .sync_mapping import db

        proj_lower = project.lower().strip()

        for alias, data in db.projects.items():
            alias_lower = alias.lower()
            stored_alias = data.get("alias", "").lower()

            if proj_lower in [alias_lower, stored_alias]:
                mapped_id = data["clickup_id"]
                mapped_type = data["clickup_type"]

                print(f"[DEBUG] Resolved '{project}' to {mapped_type} ID: {mapped_id}")

                if mapped_type == "list":
                    return [mapped_id]

                elif mapped_type == "folder":
                    resp, err = _api_call("GET", f"/folder/{mapped_id}/list")
                    if resp and resp.get("lists"):
                        list_ids = [lst["id"] for lst in resp["lists"]]
                        print(f"[DEBUG] Found {len(list_ids)} lists in folder")
                        return list_ids

                    structure = data.get("structure", {})
                    cached_lists = [
                        c["id"]
                        for c in structure.get("children", [])
                        if c.get("type") == "list"
                    ]
                    if cached_lists:
                        print(f"[DEBUG] Using {len(cached_lists)} lists from cache")
                        return cached_lists

                elif mapped_type == "space":
                    ids = []
                    resp, _ = _api_call("GET", f"/space/{mapped_id}/list")
                    if resp:
                        ids.extend([lst["id"] for lst in resp.get("lists", [])])

                    resp2, _ = _api_call("GET", f"/space/{mapped_id}/folder")
                    if resp2:
                        for f in resp2.get("folders", []):
                            ids.extend([lst["id"] for lst in f.get("lists", [])])

                    print(f"[DEBUG] Found {len(ids)} total lists in space")
                    return ids

    except Exception as e:
        print(f"[DEBUG] project_map.json lookup failed: {e}")

    print(f"[DEBUG] Falling back to API search for: {project}")
    team_id = _get_team_id()
    spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
    if not spaces_data:
        return []

    proj_lower = project.lower().strip()
    target_lists = []

    for space in spaces_data.get("spaces", []):
        if space["name"].lower() == proj_lower:
            s_lists, _ = _api_call("GET", f"/space/{space['id']}/list")
            if s_lists:
                target_lists.extend([lst["id"] for lst in s_lists.get("lists", [])])
            s_folders, _ = _api_call("GET", f"/space/{space['id']}/folder")
            if s_folders:
                for f in s_folders.get("folders", []):
                    target_lists.extend([lst["id"] for lst in f.get("lists", [])])
            return target_lists

        f_data, _ = _api_call("GET", f"/space/{space['id']}/folder")
        if f_data:
            for f in f_data.get("folders", []):
                if f["name"].lower() == proj_lower:
                    folder_lists = [lst["id"] for lst in f.get("lists", [])]
                    print(
                        f"[DEBUG] Found folder '{f['name']}' with {len(folder_lists)} lists"
                    )
                    return folder_lists

    print(f"[DEBUG] No matches found for '{project}'")
    return []


def _fetch_all_tasks(
    list_ids: List[str], base_params: Dict, include_archived: bool = True
) -> List[Dict]:
    """Fetch ALL tasks using concurrent multi-list fetching (shared client)."""
    return _client.fetch_all_tasks(list_ids, base_params, include_archived)


def _calculate_task_metrics(all_tasks: List[Dict]) -> Dict[str, Dict[str, int]]:
    """Bottom-up time calculation engine."""
    task_map = {t["id"]: t for t in all_tasks}
    children_map = {}
    for t in all_tasks:
        pid = t.get("parent")
        if pid:
            children_map.setdefault(pid, []).append(t["id"])

    cache = {}

    def get_values(tid):
        if tid in cache:
            return cache[tid]
        task_obj = task_map.get(tid, {})
        if not task_obj:
            return (0, 0, 0, 0)

        api_tracked = int(task_obj.get("time_spent") or 0)
        api_est = int(task_obj.get("time_estimate") or 0)

        sum_child_tracked, sum_child_est = 0, 0
        for cid in children_map.get(tid, []):
            c_track, _, c_est, _ = get_values(cid)
            sum_child_tracked += c_track
            sum_child_est += c_est

        direct_tracked = (
            max(0, api_tracked - sum_child_tracked)
            if api_tracked >= sum_child_tracked
            else api_tracked
        )
        direct_est = (
            max(0, api_est - sum_child_est) if api_est >= sum_child_est else api_est
        )

        res = (
            direct_tracked + sum_child_tracked,
            direct_tracked,
            direct_est + sum_child_est,
            direct_est,
        )
        cache[tid] = res
        return res

    for tid in task_map:
        get_values(tid)

    final_map = {}
    for tid, res in cache.items():
        final_map[tid] = {
            "tracked_total": res[0],
            "tracked_direct": res[1],
            "est_total": res[2],
            "est_direct": res[3],
        }
    return final_map


# --- Formatting Helpers ---


def _ms_to_readable(ms):
    return (
        datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if ms
        else "N/A"
    )


def _format_duration(ms):
    if not ms:
        return "0h 0m"
    mins = int(ms) // 60000
    return f"{mins // 60}h {mins % 60}m"


def _safe_int_from_dates(task: Dict, fields: List[str]) -> int:
    dates = []
    for f in fields:
        if val := task.get(f):
            try:
                dates.append(int(val))
            except Exception:
                pass
    return max(dates) if dates else 0


def _extract_status_name(task: Dict) -> str:
    """Safely extracts status name handling both dict and string formats."""
    status = task.get("status")
    if isinstance(status, dict):
        return status.get("status", "Unknown")
    return str(status) if status else "Unknown"


# --- Tools ---


def register_pm_analytics_tools(mcp: FastMCP):

    JOBS: Dict[str, Dict] = {}

    # -------------------------------------------------------------------------
    # Shared async helper — blocks the tool call until the job finishes so
    # the LLM receives the result directly without any manual polling.
    # -------------------------------------------------------------------------
    def _run_job_and_wait(
        fn, poll_interval: float = 5.0, max_wait: float = 360.0
    ) -> dict:
        """
        Run fn() in a background thread, sleep-poll every poll_interval
        seconds, and return the result once done.
        Falls back to a job-id response if max_wait seconds are exceeded so
        the result can still be retrieved via get_async_report_result.
        """
        jid = str(uuid.uuid4())
        JOBS[jid] = {"status": "queued", "result": None, "error": None}

        def _bg() -> None:
            try:
                JOBS[jid]["status"] = "running"
                JOBS[jid]["result"] = fn()
                JOBS[jid]["status"] = "finished"
            except Exception as exc:
                import traceback

                traceback.print_exc()
                JOBS[jid]["error"] = str(exc)
                JOBS[jid]["status"] = "failed"

        threading.Thread(target=_bg, daemon=True).start()

        elapsed = 0.0
        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval
            status = JOBS[jid]["status"]
            if status == "finished":
                return JOBS[jid]["result"]
            if status == "failed":
                return {"error": JOBS[jid].get("error", "Unknown async job error")}

        # Still running after max_wait — return a recoverable reference
        return {
            "job_id": jid,
            "status": JOBS[jid]["status"],
            "message": (
                "Report is still processing. "
                f"Use get_async_report_result(job_id='{jid}') to retrieve it when done."
            ),
        }

    @mcp.tool()
    def get_progress_since(
        since_date: str,
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        include_status_changes: bool = True,
        include_archived: bool = False,
    ) -> dict:
        """
        Get tasks completed or changed since date.
        Correctly identifies 'Shipped' as Done.
        Provides breakdown of subtasks vs main tasks.
        """
        try:
            if "T" not in since_date:
                since_date += "T00:00:00Z"
            since_ms = int(
                datetime.fromisoformat(since_date.replace("Z", "+00:00")).timestamp()
                * 1000
            )

            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": f"No context found for '{project or list_id}'"}

            tasks = _fetch_all_tasks(
                list_ids,
                {"date_updated_gt": since_ms},
                include_archived=include_archived,
            )
            completed, status_changes = [], []

            metrics = {
                "category_counts": {
                    "not_started": 0,
                    "active": 0,
                    "done": 0,
                    "closed": 0,
                    "unknown": 0,
                },
                "status_name_counts": {},
                "type_breakdown": {"main_tasks": 0, "subtasks": 0},
            }

            for t in tasks:
                status_obj = (
                    t.get("status", {}) if isinstance(t.get("status"), dict) else {}
                )
                status_name = _extract_status_name(t)
                cat = get_status_category(status_name, status_obj.get("type", ""))

                if cat in ["done", "closed"]:
                    done_date = (
                        t.get("date_closed")
                        or t.get("date_done")
                        or t.get("date_updated")
                    )
                    if done_date and int(done_date) >= since_ms:
                        completed.append(
                            {
                                "name": t.get("name"),
                                "status": status_name,
                                "completed_at": _ms_to_readable(done_date),
                                "is_subtask": bool(t.get("parent")),
                            }
                        )

                if include_status_changes:
                    if (upd := t.get("date_updated")) and int(upd) >= since_ms:
                        status_changes.append(
                            {
                                "name": t.get("name"),
                                "status": status_name,
                                "changed_at": _ms_to_readable(upd),
                            }
                        )

                    metrics["status_name_counts"][status_name] = (
                        metrics["status_name_counts"].get(status_name, 0) + 1
                    )

                    if cat in metrics["category_counts"]:
                        metrics["category_counts"][cat] += 1
                    else:
                        metrics["category_counts"]["unknown"] += 1

                    if t.get("parent"):
                        metrics["type_breakdown"]["subtasks"] += 1
                    else:
                        metrics["type_breakdown"]["main_tasks"] += 1

            return {
                "completed_tasks": completed,
                "total_completed": len(completed),
                "status_changes": status_changes if include_status_changes else None,
                "metrics": metrics,
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_time_tracking_report(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        group_by: str = "assignee",
        include_archived: bool = True,  # NEW: Include archived tasks
        status_filter: Optional[List[str]] = None,  # NEW: Optional status filter
    ) -> dict:
        """
        CRITICAL: This report includes ALL tasks from ALL statuses by default.

        Team member-wise time tracking report for a project (folder/space/list).
        Includes completed, in-progress, backlog, and all other statuses.

        Use Cases:
        - "team member wise time report for Luminique" → Shows ALL tasks across ALL statuses
        - "time tracking for Luminique" → Complete view including backlog, completed, cancelled

        Args:
            project: Project name (from project_map.json or space/folder name)
            list_id: Direct list ID (alternative to project)
            group_by: "assignee", "status", or "task"
            include_archived: Include archived tasks (default: True)
            status_filter: Optional list of status names to filter (default: all)

        Returns:
            Complete time report with all tasks included by default
        """
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": "No context found."}

            all_tasks = _fetch_all_tasks(
                list_ids,
                {},  # No date filters - get everything
                include_archived=include_archived,
            )

            print(f"[DEBUG] Fetched {len(all_tasks)} total tasks")

            if status_filter:
                filtered_tasks = []
                for t in all_tasks:
                    status_name = _extract_status_name(t)
                    if status_name in status_filter:
                        filtered_tasks.append(t)
                all_tasks = filtered_tasks
                print(f"[DEBUG] After status filter: {len(all_tasks)} tasks")

            metrics = _calculate_task_metrics(all_tasks)
            report = {}

            for t in all_tasks:
                m = metrics.get(t["id"], {})
                val_t = (
                    m.get("tracked_direct", 0)
                    if group_by == "assignee"
                    else m.get("tracked_total", 0)
                )
                val_e = (
                    m.get("est_direct", 0)
                    if group_by == "assignee"
                    else m.get("est_total", 0)
                )

                if val_t == 0 and val_e == 0:
                    continue

                keys = (
                    [u["username"] for u in t.get("assignees", [])] or ["Unassigned"]
                    if group_by == "assignee"
                    else [_extract_status_name(t)]
                )
                if group_by == "task":
                    keys = [t.get("name")]

                for k in keys:
                    r = report.setdefault(
                        k,
                        {
                            "tasks": 0,
                            "time_tracked": 0,
                            "time_estimate": 0,
                            "status_breakdown": {},  # NEW: Add status breakdown
                        },
                    )
                    r["tasks"] += 1
                    div = len(keys) if group_by == "assignee" else 1
                    r["time_tracked"] += val_t // div
                    r["time_estimate"] += val_e // div

                    status_name = _extract_status_name(t)
                    r["status_breakdown"][status_name] = (
                        r["status_breakdown"].get(status_name, 0) + 1
                    )

            formatted = {
                k: {
                    **v,
                    "human_tracked": _format_duration(v["time_tracked"]),
                    "human_est": _format_duration(v["time_estimate"]),
                    "efficiency": f"{round(v['time_tracked'] / v['time_estimate'] * 100)}%"
                    if v["time_estimate"] > 0
                    else "N/A",
                }
                for k, v in report.items()
            }

            return {
                "report": formatted,
                "total_tasks": len(all_tasks),
                "filters_applied": {
                    "include_archived": include_archived,
                    "status_filter": status_filter or "all statuses",
                },
            }
        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_time_tracking_report: {e}")
            print(traceback.format_exc())
            return {"error": str(e)}

    @mcp.tool()
    def get_task_time_breakdown(task_id: str) -> dict:
        """Detailed breakdown of a task tree."""
        try:
            task_data, err = _api_call(
                "GET", f"/task/{task_id}", params={"include_subtasks": "true"}
            )
            if err:
                return {"error": err}

            list_id = task_data["list"]["id"]
            all_list_tasks = _fetch_all_tasks([list_id], {})
            metrics_map = _calculate_task_metrics(all_list_tasks)

            task_map = {t["id"]: t for t in all_list_tasks}
            children_map = {}
            for t in all_list_tasks:
                if pid := t.get("parent"):
                    children_map.setdefault(pid, []).append(t["id"])

            tree_view = []

            def build_tree(tid, depth=0):
                t = task_map.get(tid)
                if not t:
                    return
                m = metrics_map.get(tid, {})

                tree_view.append(
                    {
                        "task": f"{'  ' * depth}{t.get('name')}",
                        "status": _extract_status_name(t),
                        "tracked_total": _format_duration(m.get("tracked_total", 0)),
                        "tracked_direct": _format_duration(m.get("tracked_direct", 0)),
                        "estimated": _format_duration(m.get("est_total", 0)),
                    }
                )
                for cid in children_map.get(tid, []):
                    build_tree(cid, depth + 1)

            build_tree(task_id)
            return {"root_task": task_data["name"], "breakdown_tree": tree_view}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_project_report_universal(
        project_name: str,
        report_type: str = "time_tracking",
        period_type: str = "this_week",
        custom_start: Optional[str] = None,
        custom_end: Optional[str] = None,
        week_selector: Optional[str] = None,
        group_by: str = "assignee",
        include_archived: bool = True,
        async_job: bool = False,
    ) -> dict:
        """
        Universal project report generator. Works with ANY project (space, folder, or list).
        Automatically detects the project type and generates the appropriate report.
        For space-level reports, always prefer async_job=True to avoid timeouts.

        Args:
            project_name: Name of the project (can be a space, folder, or list anywhere)
            report_type: Type of report - "time_tracking", "weekly", "status", or "progress"
            period_type: today, yesterday, this_week, last_week, this_month, last_month, custom
            custom_start, custom_end: YYYY-MM-DD format for custom period
            week_selector: For weekly reports: "current", "previous", "2-weeks-ago"
            group_by: How to group results (assignee, status, folder, list)
            include_archived: Include archived tasks
            async_job: Run in background — ALWAYS pass True for space-level reports

        Returns:
            Comprehensive report based on the project type and report_type requested,
            or {job_id, status} if async_job=True

        Use Cases:
            - "Generate monthly report for AIX" -> async_job=True, Detects AIX as space
            - "Time tracking for Luminique" -> Detects Luminique as folder, runs folder report
            - "Status distribution for Backlog" -> Detects Backlog as list, runs list report

        This is the easiest way to generate reports - just provide the project name!
        """
        try:
            from .sync_mapping import find_entity_anywhere

            # 1. Find the project
            print(f"[DEBUG] Searching for project: {project_name}")
            entity = find_entity_anywhere(project_name)

            if not entity:
                return {
                    "error": f"Project '{project_name}' not found",
                    "hint": "Check the spelling or use find_project_anywhere() to search for it",
                    "suggestion": "You may need to map the project first using map_project()",
                }

            entity_type = entity["type"]
            entity_id = entity["id"]
            entity_name = entity["name"]

            print(f"[DEBUG] Found '{entity_name}' as {entity_type} (ID: {entity_id})")

            # 2. Route to appropriate report based on entity type and report_type
            if report_type == "time_tracking":
                if entity_type == "space":
                    # Space reports can be large — always force async to prevent timeout
                    return get_space_time_report_comprehensive(
                        space_id=entity_id,
                        period_type=period_type,
                        custom_start=custom_start,
                        custom_end=custom_end,
                        group_by=group_by,
                        include_archived=include_archived,
                        async_job=True,
                    )
                elif entity_type == "folder":
                    parent_space = entity.get("parent_name", "")
                    return get_folder_time_report_comprehensive(
                        folder_id=entity_id,
                        space_name=parent_space,
                        period_type=period_type,
                        custom_start=custom_start,
                        custom_end=custom_end,
                        group_by=group_by,
                        include_archived=include_archived,
                        async_job=async_job,
                    )
                else:  # list
                    return get_time_tracking_report(
                        list_id=entity_id,
                        group_by=group_by,
                        include_archived=include_archived,
                    )

            elif report_type == "weekly":
                if entity_type == "space":
                    return get_space_time_report_by_period(
                        space_name=entity_name,
                        week_selector=week_selector,
                        week_start=custom_start,
                        week_end=custom_end,
                    )
                else:
                    # For folders and lists, use detailed time report
                    return get_time_report_detailed(
                        report_type="team_member",
                        project=entity_name,
                        week_selector=week_selector,
                        week_start=custom_start,
                        week_end=custom_end,
                    )

            elif report_type == "status":
                return get_task_status_distribution(
                    project=entity_name if entity_type != "list" else None,
                    list_id=entity_id if entity_type == "list" else None,
                    include_archived=include_archived,
                )

            elif report_type == "progress":
                # Progress since start of current period
                if period_type == "this_week":
                    from .status_helpers import get_current_week_dates

                    since_date = get_current_week_dates()[0]
                elif period_type == "this_month":
                    from datetime import datetime

                    since_date = datetime.now().strftime("%Y-%m-01")
                elif custom_start:
                    since_date = custom_start
                else:
                    since_date = (datetime.now() - timedelta(days=7)).strftime(
                        "%Y-%m-%d"
                    )

                return get_progress_since(
                    since_date=since_date,
                    project=entity_name if entity_type != "list" else None,
                    list_id=entity_id if entity_type == "list" else None,
                    include_archived=include_archived,
                )

            else:
                return {
                    "error": f"Unknown report_type: {report_type}",
                    "valid_types": ["time_tracking", "weekly", "status", "progress"],
                }

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_project_report_universal: {e}")
            print(traceback.format_exc())
            return {"error": str(e), "traceback": traceback.format_exc()}

    @mcp.tool()
    def get_estimation_accuracy(
        project: Optional[str] = None, list_id: Optional[str] = None
    ) -> dict:
        """Analyze estimation vs actuals using robust metrics."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": "No context"}
            tasks = _fetch_all_tasks(list_ids, {})
            metrics = _calculate_task_metrics(tasks)

            est_total, spent_on_est, spent_unest = 0, 0, 0
            over, under, accurate = 0, 0, 0

            for t in tasks:
                m = metrics.get(t["id"], {})
                dt, de = m.get("tracked_direct", 0), m.get("est_direct", 0)

                if de > 0:
                    est_total += de
                    spent_on_est += dt
                    ratio = dt / de if de else 0
                    if dt == 0:
                        over += 1
                    elif ratio < 0.8:
                        over += 1
                    elif ratio > 1.2:
                        under += 1
                    else:
                        accurate += 1
                elif dt > 0:
                    spent_unest += dt

            return {
                "total_estimated": _format_duration(est_total),
                "spent_on_estimated": _format_duration(spent_on_est),
                "spent_unplanned": _format_duration(spent_unest),
                "accuracy_breakdown": {
                    "accurate": accurate,
                    "under_estimated": under,
                    "over_estimated": over,
                },
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_at_risk_tasks(
        project: Optional[str] = None, list_id: Optional[str] = None, risk_days: int = 3
    ) -> dict:
        """Find tasks overdue or due soon."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": "No context"}
            tasks = _fetch_all_tasks(list_ids, {})
            now = time.time() * 1000
            limit = now + (risk_days * 86400000)

            risks = []
            for t in tasks:
                status = (
                    t.get("status", {}) if isinstance(t.get("status"), dict) else {}
                )
                status_name = _extract_status_name(t)
                cat = get_status_category(status_name, status.get("type"))

                if cat in ["active", "not_started"]:
                    if due := t.get("due_date"):
                        due = int(due)
                        if due < now:
                            risks.append(
                                {
                                    "name": t["name"],
                                    "risk": "Overdue",
                                    "due": _ms_to_readable(due),
                                }
                            )
                        elif due <= limit:
                            risks.append(
                                {
                                    "name": t["name"],
                                    "risk": "Due Soon",
                                    "due": _ms_to_readable(due),
                                }
                            )

            return {"at_risk_count": len(risks), "tasks": risks}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_stale_tasks(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        stale_days: int = 7,
    ) -> dict:
        """Find tasks with no updates."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": "No context"}
            tasks = _fetch_all_tasks(list_ids, {})
            now = time.time() * 1000
            cutoff = now - (stale_days * 86400000)
            stale = []

            for t in tasks:
                status_name = _extract_status_name(t)
                cat = get_status_category(status_name)
                if cat not in ["done", "closed"]:
                    updated = int(t.get("date_updated") or 0)
                    if updated < cutoff:
                        stale.append(
                            {"name": t["name"], "last_update": _ms_to_readable(updated)}
                        )

            return {"stale_count": len(stale), "tasks": stale}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_untracked_tasks(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        status_filter: str = "in_progress",
    ) -> dict:
        """Find tasks with zero logged time."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": "No context"}
            tasks = _fetch_all_tasks(list_ids, {})
            metrics = _calculate_task_metrics(tasks)
            untracked = []

            for t in tasks:
                status_obj = (
                    t.get("status", {}) if isinstance(t.get("status"), dict) else {}
                )
                status_name = _extract_status_name(t)
                cat = get_status_category(status_name, status_obj.get("type"))

                check = (status_filter == "all") or (
                    status_filter == "in_progress" and cat == "active"
                )

                if check:
                    if metrics.get(t["id"], {}).get("tracked_direct", 0) == 0:
                        untracked.append({"name": t["name"], "status": status_name})

            return {"count": len(untracked), "tasks": untracked}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_inactive_assignees(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        inactive_days: int = 3,
    ) -> dict:
        """Identify inactive team members."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": "No context"}
            tasks = _fetch_all_tasks(list_ids, {})
            now = time.time() * 1000
            cutoff = now - (inactive_days * 86400000)
            activity_map = {}

            for t in tasks:
                last_act = _safe_int_from_dates(t, ["date_updated", "date_closed"])
                for u in t.get("assignees", []):
                    name = u["username"]
                    if name not in activity_map:
                        activity_map[name] = 0
                    activity_map[name] = max(activity_map[name], last_act)

            inactive = [
                {"user": k, "last_active": _ms_to_readable(v)}
                for k, v in activity_map.items()
                if v < cutoff
            ]
            return {"inactive_count": len(inactive), "users": inactive}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_status_summary(
        project: Optional[str] = None, list_id: Optional[str] = None
    ) -> dict:
        """Summary of task statuses."""
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": "No context"}
            tasks = _fetch_all_tasks(list_ids, {})
            counts = {}
            categories = {
                "not_started": 0,
                "active": 0,
                "done": 0,
                "closed": 0,
                "other": 0,
            }

            for t in tasks:
                status_obj = (
                    t.get("status", {}) if isinstance(t.get("status"), dict) else {}
                )
                name = _extract_status_name(t)
                cat = get_status_category(name, status_obj.get("type"))

                counts[name] = counts.get(name, 0) + 1
                if cat in categories:
                    categories[cat] += 1
                else:
                    categories["other"] += 1

            return {"total": len(tasks), "by_status": counts, "by_category": categories}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_space_time_report(
        space_name: Optional[str] = None,
        space_id: Optional[str] = None,
        group_by: str = "assignee",
        include_archived: bool = True,
    ) -> dict:
        """
        Time tracking report for entire SPACE (all folders and lists).

        Args:
            space_name: Space name
            space_id: Direct space ID
            group_by: assignee, folder, or status
            include_archived: Include archived tasks
        """
        try:
            if not space_id and not space_name:
                return {"error": "Provide either space_name or space_id"}

            if not space_id:
                team_id = _get_team_id()
                spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
                if not spaces_data:
                    return {"error": "Failed to fetch spaces"}

                for space in spaces_data.get("spaces", []):
                    if space["name"].lower() == space_name.lower():
                        space_id = space["id"]
                        break

                if not space_id:
                    return {"error": f"Space '{space_name}' not found"}

            list_ids = []

            resp, _ = _api_call("GET", f"/space/{space_id}/list")
            if resp:
                list_ids.extend([lst["id"] for lst in resp.get("lists", [])])

            resp2, _ = _api_call("GET", f"/space/{space_id}/folder")
            if resp2:
                for folder in resp2.get("folders", []):
                    list_ids.extend([lst["id"] for lst in folder.get("lists", [])])

            print(f"[DEBUG] Found {len(list_ids)} lists in space")

            all_tasks = _fetch_all_tasks(
                list_ids, {}, include_archived=include_archived
            )
            print(f"[DEBUG] Fetched {len(all_tasks)} tasks from space")

            metrics = _calculate_task_metrics(all_tasks)

            report = {}
            for t in all_tasks:
                m = metrics.get(t["id"], {})
                val_t = (
                    m.get("tracked_direct", 0)
                    if group_by == "assignee"
                    else m.get("tracked_total", 0)
                )
                val_e = (
                    m.get("est_direct", 0)
                    if group_by == "assignee"
                    else m.get("est_total", 0)
                )

                if val_t == 0 and val_e == 0:
                    continue

                if group_by == "assignee":
                    keys = [u["username"] for u in t.get("assignees", [])] or [
                        "Unassigned"
                    ]
                elif group_by == "folder":
                    folder_name = t.get("folder", {}).get("name") or "Folderless"
                    keys = [folder_name]
                else:  # status
                    keys = [_extract_status_name(t)]

                for k in keys:
                    r = report.setdefault(
                        k, {"tasks": 0, "time_tracked": 0, "time_estimate": 0}
                    )
                    r["tasks"] += 1
                    div = len(keys) if group_by == "assignee" else 1
                    r["time_tracked"] += val_t // div
                    r["time_estimate"] += val_e // div

            formatted = {
                k: {
                    **v,
                    "human_tracked": _format_duration(v["time_tracked"]),
                    "human_est": _format_duration(v["time_estimate"]),
                    "efficiency": f"{round(v['time_tracked'] / v['time_estimate'] * 100)}%"
                    if v["time_estimate"] > 0
                    else "N/A",
                }
                for k, v in report.items()
            }

            return {
                "space_name": space_name or "Unknown",
                "space_id": space_id,
                "report": formatted,
                "total_tasks": len(all_tasks),
                "grouped_by": group_by,
            }

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_space_time_report: {e}")
            print(traceback.format_exc())
            return {"error": str(e)}

    @mcp.tool()
    def get_space_folder_team_report(
        space_name: Optional[str] = None,
        space_id: Optional[str] = None,
        include_archived: bool = True,
    ) -> dict:
        """
        Hierarchical time report: Space > Folder > Team Member.

        Args:
            space_name: Space name
            space_id: Direct space ID
            include_archived: Include archived tasks
        """
        try:
            if not space_id and not space_name:
                return {"error": "Provide either space_name or space_id"}

            if not space_id:
                team_id = _get_team_id()
                spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
                if not spaces_data:
                    return {"error": "Failed to fetch spaces"}

                for space in spaces_data.get("spaces", []):
                    if space["name"].lower() == space_name.lower():
                        space_id = space["id"]
                        space_name = space["name"]
                        break

                if not space_id:
                    return {"error": f"Space '{space_name}' not found"}

            folders_report = []

            resp, _ = _api_call("GET", f"/space/{space_id}/folder")
            folders = resp.get("folders", []) if resp else []

            for folder in folders:
                folder_id = folder["id"]
                folder_name = folder["name"]
                list_ids = [lst["id"] for lst in folder.get("lists", [])]

                if not list_ids:
                    continue

                folder_tasks = _fetch_all_tasks(
                    list_ids, {}, include_archived=include_archived
                )
                metrics = _calculate_task_metrics(folder_tasks)

                team_report = {}
                for t in folder_tasks:
                    m = metrics.get(t["id"], {})
                    val_t = m.get("tracked_direct", 0)
                    val_e = m.get("est_direct", 0)

                    if val_t == 0 and val_e == 0:
                        continue

                    assignees = [u["username"] for u in t.get("assignees", [])] or [
                        "Unassigned"
                    ]
                    for member in assignees:
                        r = team_report.setdefault(
                            member, {"tasks": 0, "time_tracked": 0, "time_estimate": 0}
                        )
                        r["tasks"] += 1
                        r["time_tracked"] += val_t // len(assignees)
                        r["time_estimate"] += val_e // len(assignees)

                formatted_team = {
                    member: {
                        **data,
                        "human_tracked": _format_duration(data["time_tracked"]),
                        "human_est": _format_duration(data["time_estimate"]),
                    }
                    for member, data in team_report.items()
                }

                folders_report.append(
                    {
                        "folder_name": folder_name,
                        "folder_id": folder_id,
                        "total_tasks": len(folder_tasks),
                        "team_breakdown": formatted_team,
                    }
                )

            resp, _ = _api_call("GET", f"/space/{space_id}/list")
            folderless_lists = resp.get("lists", []) if resp else []

            if folderless_lists:
                list_ids = [lst["id"] for lst in folderless_lists]
                folderless_tasks = _fetch_all_tasks(
                    list_ids, {}, include_archived=include_archived
                )
                metrics = _calculate_task_metrics(folderless_tasks)

                team_report = {}
                for t in folderless_tasks:
                    m = metrics.get(t["id"], {})
                    val_t = m.get("tracked_direct", 0)
                    val_e = m.get("est_direct", 0)

                    if val_t == 0 and val_e == 0:
                        continue

                    assignees = [u["username"] for u in t.get("assignees", [])] or [
                        "Unassigned"
                    ]
                    for member in assignees:
                        r = team_report.setdefault(
                            member, {"tasks": 0, "time_tracked": 0, "time_estimate": 0}
                        )
                        r["tasks"] += 1
                        r["time_tracked"] += val_t // len(assignees)
                        r["time_estimate"] += val_e // len(assignees)

                formatted_team = {
                    member: {
                        **data,
                        "human_tracked": _format_duration(data["time_tracked"]),
                        "human_est": _format_duration(data["time_estimate"]),
                    }
                    for member, data in team_report.items()
                }

                folders_report.append(
                    {
                        "folder_name": "Folderless Lists",
                        "folder_id": "folderless",
                        "total_tasks": len(folderless_tasks),
                        "team_breakdown": formatted_team,
                    }
                )

            return {
                "space_name": space_name,
                "space_id": space_id,
                "folders": folders_report,
                "total_folders": len(folders_report),
            }

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_space_folder_team_report: {e}")
            print(traceback.format_exc())
            return {"error": str(e)}

    @mcp.tool()
    def get_time_report_by_period(
        report_type: str = "team_member",
        project: Optional[str] = None,
        space_name: Optional[str] = None,
        list_id: Optional[str] = None,
        week_selector: Optional[
            str
        ] = None,  # e.g., "current_week", "last_week", "2-weeks", "month", or custom "YYYY-MM-DD to YYYY-MM-DD"
        week_start: Optional[str] = None,  # YYYY-MM-DD format
        week_end: Optional[str] = None,  # YYYY-MM-DD format
        allow_multi_week: bool = False,  # Enable multi-week ranges
        async_job: bool = False,  # If True, run report in background and return job id
        job_id: Optional[str] = None,  # Internal use when running as background job
    ) -> dict:
        """
        Time report based on time entry intervals. Supports any date range (day, week, month, year, custom).

        Args:
            report_type: team_member, space, or space_folder_team
            project: Project name (for team_member)
            space_name: Space name (for space types)
            list_id: Direct list ID
            week_selector: current, previous, N-weeks-ago, YYYY-MM-DD, YYYY-WNN (or multi-week: 2-weeks, last-2-weeks, month)
            week_start, week_end: YYYY-MM-DD format (explicit dates)
            allow_multi_week: Enable multi-week ranges
            async_job: Run in background (recommended for 300+ tasks)

        Returns:
            Report with time tracked from time entries within date range
        """
        try:
            import sys

            # Early acknowledgment
            print("⏳ Processing time report request - this may take 1-3 minutes...")
            sys.stdout.flush()

            if not job_id:
                return _run_job_and_wait(
                    lambda: get_time_report_by_period(
                        report_type=report_type,
                        project=project,
                        space_name=space_name,
                        list_id=list_id,
                        week_selector=week_selector,
                        week_start=week_start,
                        week_end=week_end,
                        allow_multi_week=allow_multi_week,
                        async_job=False,
                        job_id="_bg_",
                    )
                )

            if week_start and week_end:
                # Priority 1: Explicit dates provided - validate and use them
                try:
                    validate_week_dates(
                        week_start, week_end, allow_multi_week=allow_multi_week
                    )
                    print(f"[DEBUG] Using explicit dates: {week_start} to {week_end}")
                    sys.stdout.flush()
                except ValueError as e:
                    return {
                        "error": f"Invalid week dates: {str(e)}",
                        "hint": "week_start must be Monday, week_end must be Sunday"
                        + (
                            " (multi-week allowed if allow_multi_week=True)"
                            if allow_multi_week
                            else ""
                        ),
                    }
            elif week_selector:
                # Priority 2: Use smart week selector
                try:
                    week_start, week_end = parse_week_input(
                        week_selector, allow_multi_week=allow_multi_week
                    )
                    print(
                        f"[DEBUG] Parsed '{week_selector}' → {week_start} to {week_end}"
                    )
                    sys.stdout.flush()
                except ValueError as e:
                    return {
                        "error": f"Invalid week_selector: {str(e)}",
                        "hint": "Supported: 'current', 'previous', 'N-weeks-ago', 'YYYY-MM-DD', 'YYYY-WNN'"
                        + (
                            " + multi-week: 'N-weeks', 'last-N-weeks', 'month'"
                            if allow_multi_week
                            else ""
                        ),
                        "examples": [
                            "current",
                            "previous",
                            "2-weeks-ago",
                            "2026-01-15",
                            "2026-W03",
                        ]
                        + (
                            ["2-weeks", "last-2-weeks", "month"]
                            if allow_multi_week
                            else []
                        ),
                    }
            else:
                # Priority 3: Default to current week
                week_start, week_end = get_current_week_dates()
                print(
                    f"[DEBUG] Using default (current week): {week_start} to {week_end}"
                )
                sys.stdout.flush()

            start_ms, end_ms = date_range_to_timestamps(week_start, week_end)

            from datetime import datetime

            start_date = datetime.strptime(week_start, "%Y-%m-%d")
            end_date = datetime.strptime(week_end, "%Y-%m-%d")
            num_weeks = ((end_date - start_date).days + 1) // 7

            print(
                f"[DEBUG] Report period: {week_start} to {week_end} ({num_weeks} week(s))"
            )
            print(f"[DEBUG] Timestamp range: {start_ms} to {end_ms}")
            sys.stdout.flush()

            if report_type == "space":
                if not space_name:
                    return {"error": "space_name required for space report"}

                # Get space report with ALL tasks (we'll filter time entries, not tasks)
                print(f"[PROGRESS] Step 1/5: Resolving space '{space_name}'...")
                sys.stdout.flush()

                team_id = _get_team_id()
                spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
                if not spaces_data:
                    return {"error": "Failed to fetch spaces"}

                space_id = None
                for space in spaces_data.get("spaces", []):
                    if space["name"].lower() == space_name.lower():
                        space_id = space["id"]
                        break

                if not space_id:
                    return {"error": f"Space '{space_name}' not found"}

                print("[PROGRESS] Step 2/5: Fetching space structure...")
                sys.stdout.flush()

                list_ids = []
                resp, _ = _api_call("GET", f"/space/{space_id}/list")
                if resp:
                    list_ids.extend([lst["id"] for lst in resp.get("lists", [])])
                resp2, _ = _api_call("GET", f"/space/{space_id}/folder")
                if resp2:
                    for folder in resp2.get("folders", []):
                        list_ids.extend([lst["id"] for lst in folder.get("lists", [])])

                print(
                    f"[PROGRESS] Step 3/5: Fetching tasks from {len(list_ids)} lists..."
                )
                sys.stdout.flush()

                all_tasks = _fetch_all_tasks(list_ids, {}, include_archived=True)

                print(f"[PROGRESS] Found {len(all_tasks)} total tasks in space")
                sys.stdout.flush()

                if len(all_tasks) >= 300 and not job_id:
                    print(
                        f"⚡ AUTO-ASYNC: {len(all_tasks)} tasks detected. Switching to background mode to prevent timeout."
                    )
                    sys.stdout.flush()
                    # Recursively call with async_job=True
                    return get_time_report_by_period(
                        report_type=report_type,
                        space_name=space_name,
                        week_selector=week_selector,
                        week_start=week_start,
                        week_end=week_end,
                        allow_multi_week=allow_multi_week,
                        async_job=True,
                    )

                # Fetch time entries for all tasks
                print(
                    f"[PROGRESS] Step 4/5: Fetching time entries for {len(all_tasks)} tasks..."
                )
                print(
                    "[PROGRESS] This may take a few minutes for large datasets. Please wait..."
                )
                sys.stdout.flush()

                timed_tasks = [
                    t for t in all_tasks if int(t.get("time_spent") or 0) > 0
                ]
                task_ids = [t["id"] for t in timed_tasks]
                print(
                    f"[PROGRESS] {len(task_ids):,}/{len(all_tasks):,} tasks have time tracked"
                    f" -- skipping {len(all_tasks) - len(task_ids):,} with zero time_spent"
                )
                sys.stdout.flush()
                time_entries_map = _fetch_time_entries_smart(task_ids, start_ms, end_ms)

                print(
                    "[PROGRESS] Step 5/5: Processing time entries and building report..."
                )
                sys.stdout.flush()

                metrics = _calculate_task_metrics(all_tasks)

                report = {}
                tasks_with_time_in_range = 0

                for t in all_tasks:
                    task_id = t["id"]
                    time_entries = time_entries_map.get(task_id, [])

                    if not time_entries:
                        continue

                    # Filter time entries by date range
                    time_in_range, filtered_intervals = (
                        filter_time_entries_by_date_range(
                            time_entries, start_ms, end_ms
                        )
                    )

                    if time_in_range == 0:
                        continue

                    tasks_with_time_in_range += 1

                    # Split time among assignees and also add estimated time
                    assignees = [u["username"] for u in t.get("assignees", [])] or [
                        "Unassigned"
                    ]
                    time_per_assignee = time_in_range // len(assignees)

                    # Pull task-level direct estimate (0 if absent)
                    est_direct = metrics.get(task_id, {}).get("est_direct", 0)
                    est_per_assignee = est_direct // len(assignees) if est_direct else 0

                    for member in assignees:
                        r = report.setdefault(
                            member,
                            {
                                "tasks": 0,
                                "time_tracked": 0,
                                "time_estimate": 0,
                                "intervals": [],
                            },
                        )
                        r["tasks"] += 1
                        r["time_tracked"] += time_per_assignee
                        r["time_estimate"] += est_per_assignee
                        r["intervals"].extend(filtered_intervals)

                formatted = {
                    member: {
                        "tasks": data["tasks"],
                        "time_tracked": data["time_tracked"],
                        "human_tracked": _format_duration(data["time_tracked"]),
                        "time_estimate": data.get("time_estimate", 0),
                        "human_est": _format_duration(data.get("time_estimate", 0)),
                        "intervals_count": len(data["intervals"]),
                    }
                    for member, data in report.items()
                }

                return {
                    "report_type": "space_weekly",
                    "space_name": space_name,
                    "week_start": week_start,
                    "week_end": week_end,
                    "num_weeks": num_weeks,
                    "period_description": f"{num_weeks} week(s) from {week_start} to {week_end}",
                    "report": formatted,
                    "total_tasks_in_space": len(all_tasks),
                    "tasks_with_time_in_range": tasks_with_time_in_range,
                }

            elif report_type == "team_member":
                if not project and not list_id:
                    return {"error": "Provide project or list_id"}

                print("[PROGRESS] Step 1/5: Resolving project/list...")
                sys.stdout.flush()

                list_ids = _resolve_to_list_ids(project, list_id)
                if not list_ids:
                    return {"error": "No context found"}

                print(
                    f"[PROGRESS] Step 2/5: Fetching tasks from {len(list_ids)} lists..."
                )
                sys.stdout.flush()

                all_tasks = _fetch_all_tasks(list_ids, {}, include_archived=True)

                print(f"[PROGRESS] Found {len(all_tasks)} total tasks")
                sys.stdout.flush()

                if len(all_tasks) >= 300 and not job_id:
                    print(
                        f"⚡ AUTO-ASYNC: {len(all_tasks)} tasks detected. Switching to background mode to prevent timeout."
                    )
                    sys.stdout.flush()
                    # Recursively call with async_job=True
                    return get_time_report_by_period(
                        report_type=report_type,
                        project=project,
                        list_id=list_id,
                        week_selector=week_selector,
                        week_start=week_start,
                        week_end=week_end,
                        allow_multi_week=allow_multi_week,
                        async_job=True,
                    )

                # Fetch time entries for all tasks
                print(
                    f"[PROGRESS] Step 3/5: Fetching time entries for {len(all_tasks)} tasks..."
                )
                print(
                    "[PROGRESS] This may take a few minutes for large datasets. Please wait..."
                )
                sys.stdout.flush()

                timed_tasks = [
                    t for t in all_tasks if int(t.get("time_spent") or 0) > 0
                ]
                task_ids = [t["id"] for t in timed_tasks]
                print(
                    f"[PROGRESS] {len(task_ids):,}/{len(all_tasks):,} tasks have time tracked"
                    f" -- skipping {len(all_tasks) - len(task_ids):,} with zero time_spent"
                )
                sys.stdout.flush()
                time_entries_map = _fetch_time_entries_smart(task_ids, start_ms, end_ms)

                print("[PROGRESS] Step 4/5: Processing time entries...")
                sys.stdout.flush()

                metrics = _calculate_task_metrics(all_tasks)

                report = {}
                tasks_with_time_in_range = 0

                for t in all_tasks:
                    task_id = t["id"]
                    time_entries = time_entries_map.get(task_id, [])

                    if not time_entries:
                        continue

                    # Filter time entries by date range
                    time_in_range, filtered_intervals = (
                        filter_time_entries_by_date_range(
                            time_entries, start_ms, end_ms
                        )
                    )

                    if time_in_range == 0:
                        continue

                    tasks_with_time_in_range += 1

                    # Split time among assignees and also add estimated time
                    assignees = [u["username"] for u in t.get("assignees", [])] or [
                        "Unassigned"
                    ]
                    time_per_assignee = time_in_range // len(assignees)

                    # Pull task-level direct estimate (0 if absent)
                    est_direct = metrics.get(task_id, {}).get("est_direct", 0)
                    est_per_assignee = est_direct // len(assignees) if est_direct else 0

                    for member in assignees:
                        r = report.setdefault(
                            member,
                            {
                                "tasks": 0,
                                "time_tracked": 0,
                                "time_estimate": 0,
                                "intervals": [],
                            },
                        )
                        r["tasks"] += 1
                        r["time_tracked"] += time_per_assignee
                        r["time_estimate"] += est_per_assignee
                        r["intervals"].extend(filtered_intervals)

                formatted = {
                    member: {
                        "tasks": data["tasks"],
                        "time_tracked": data["time_tracked"],
                        "human_tracked": _format_duration(data["time_tracked"]),
                        "time_estimate": data.get("time_estimate", 0),
                        "human_est": _format_duration(data.get("time_estimate", 0)),
                        "intervals_count": len(data["intervals"]),
                    }
                    for member, data in report.items()
                }

                print("[PROGRESS] Step 5/5: Report complete!")
                sys.stdout.flush()

                return {
                    "report_type": "team_member_weekly",
                    "project": project or "Direct list",
                    "week_start": week_start,
                    "week_end": week_end,
                    "num_weeks": num_weeks,
                    "period_description": f"{num_weeks} week(s) from {week_start} to {week_end}",
                    "report": formatted,
                    "total_tasks": len(all_tasks),
                    "tasks_with_time_in_range": tasks_with_time_in_range,
                }

            else:
                return {"error": f"Invalid report_type: {report_type}"}

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_time_report_by_period: {e}")
            print(traceback.format_exc())
            return {"error": str(e)}

    @mcp.tool()
    def get_async_report_status(job_id: str) -> dict:
        """Check status for an async report job. HARD LIMIT: 5 polls per job.

        Returns:
            - status: 'queued', 'running', 'finished', or 'failed'
            - poll_count: How many times this job has been polled
            - polls_remaining: How many more polls before hard stop
            - STOP_POLLING: True when max polls reached — you MUST stop calling this tool

        IMPORTANT: If STOP_POLLING is True, do NOT call this tool again.
        Instead, tell the user to try 'get_async_report_result' in a minute.
        """
        j = JOBS.get(job_id)
        if not j:
            return {"error": "job_id not found"}

        # Track poll count
        poll_count = j.get("_poll_count", 0) + 1
        j["_poll_count"] = poll_count
        max_polls = 5

        status = j.get("status")

        # If finished or failed, always return immediately
        if status in ("finished", "failed"):
            resp = {
                "job_id": job_id,
                "status": status,
                "error": j.get("error"),
                "poll_count": poll_count,
                "polls_remaining": 0,
            }
            # Auto-include result if finished to save a round trip
            if status == "finished":
                resp["result"] = j.get("result")
                resp["message"] = (
                    "Job complete! Result included — no need to call get_async_report_result."
                )
            return resp

        # Enforce hard poll limit
        if poll_count >= max_polls:
            return {
                "job_id": job_id,
                "status": status,
                "poll_count": poll_count,
                "polls_remaining": 0,
                "STOP_POLLING": True,
                "message": (
                    "STOP: Maximum poll limit (5) reached. "
                    "Do NOT call get_async_report_status again. "
                    "Tell the user: 'The report is still processing. "
                    "Please ask me to check the result in 1-2 minutes using: "
                    f'get_async_report_result(job_id="{job_id}")\''
                ),
            }

        return {
            "job_id": job_id,
            "status": status,
            "error": j.get("error"),
            "poll_count": poll_count,
            "polls_remaining": max_polls - poll_count,
            "message": f"Job still {status}. You have {max_polls - poll_count} status checks remaining. Wait 50-60 seconds before checking again.",
        }

    @mcp.tool()
    def get_async_report_result(job_id: str) -> dict:
        """Return result for finished async report job. Safe to call anytime."""
        j = JOBS.get(job_id)
        if not j:
            return {"error": "job_id not found"}
        if j.get("status") != "finished":
            return {
                "status": j.get("status"),
                "message": f"Result not ready yet (status: {j.get('status')}). Wait and try again later.",
            }
        return {"status": "finished", "result": j.get("result")}

    @mcp.tool()
    def get_space_time_report_by_period(
        space_name: str,
        week_selector: Optional[str] = None,
        week_start: Optional[str] = None,
        week_end: Optional[str] = None,
        allow_multi_week: bool = False,
        async_job: bool = False,
    ) -> dict:
        """Space time report with async support. Works for any period (day, week, month, etc).

        Args:
            space_name: ClickUp space name
            week_selector: current, previous, N-weeks-ago, YYYY-MM-DD, YYYY-WNN
            week_start, week_end: YYYY-MM-DD format
            allow_multi_week: Enable multi-week ranges
            async_job: Run in background (use for 300+ tasks)

        Returns:
            Report dict or job_id if async mode
        """
        return get_time_report_by_period(
            report_type="space",
            space_name=space_name,
            week_selector=week_selector,
            week_start=week_start,
            week_end=week_end,
            allow_multi_week=allow_multi_week,
            async_job=async_job,
        )

    @mcp.tool()
    def get_space_project_time_report(
        space_name: str,
        period_type: str = "this_month",
        custom_start: Optional[str] = None,
        custom_end: Optional[str] = None,
        rolling_days: Optional[int] = None,
        include_archived: bool = True,
        async_job: bool = False,
        job_id: Optional[str] = None,
    ) -> dict:
        """
        Generate a time report for a SPACE grouped by PROJECT (folder/list).

        Identifies projects in a space based on its hierarchical structure:
        - Space -> Folder (project name) -> Lists: each folder = a project
        - Space -> Folderless Lists: each list = a project
        - Mixed: folders are projects + standalone lists are projects

        For each project, shows:
        - Total time tracked (within the date range)
        - Total time estimated
        - Number of tasks worked on
        - Team member breakdown within each project

        Args:
            space_name: ClickUp space name (e.g., "AIX", "JewelleryOS")
            period_type: today, yesterday, this_week, last_week, this_month,
                         last_month, this_year, last_30_days, rolling, custom
            custom_start, custom_end: YYYY-MM-DD format for custom period
            rolling_days: Number of days (1-365) for rolling period
            include_archived: Include archived tasks (default: True)
            async_job: Run in background (auto-triggers at 500+ tasks)
            job_id: Internal use for background jobs

        Returns:
            Report with per-project time tracked, estimates, and team breakdown

        Examples:
            get_space_project_time_report(space_name="AIX")
            get_space_project_time_report(space_name="AIX", period_type="this_month")
            get_space_project_time_report(space_name="AIX", period_type="custom",
                                          custom_start="2026-01-01", custom_end="2026-01-31")
        """
        try:
            from clickup_mcp.status_helpers import (
                parse_time_period_filter,
                filter_time_entries_by_user_and_date_range,
            )
            import sys

            print(f"⌛ Generating project-wise time report for space '{space_name}'...")
            sys.stdout.flush()

            # ================================================================
            # ASYNC JOB SUPPORT — result returned directly, no LLM polling
            # ================================================================
            if not job_id:
                return _run_job_and_wait(
                    lambda: get_space_project_time_report(
                        space_name=space_name,
                        period_type=period_type,
                        custom_start=custom_start,
                        custom_end=custom_end,
                        rolling_days=rolling_days,
                        include_archived=include_archived,
                        async_job=False,
                        job_id="_bg_",
                    )
                )

            # ================================================================
            # STEP 1: Parse date range
            # ================================================================
            try:
                start_date, end_date = parse_time_period_filter(
                    period_type=period_type,
                    custom_start=custom_start,
                    custom_end=custom_end,
                    rolling_days=rolling_days,
                )
            except ValueError as e:
                return {"error": f"Invalid period specification: {str(e)}"}

            print(
                f"[DEBUG] Period: {period_type}, Date range: {start_date} to {end_date}"
            )
            sys.stdout.flush()

            start_ms, end_ms = date_range_to_timestamps(start_date, end_date)

            # ================================================================
            # STEP 2: Resolve space
            # ================================================================
            team_id = _get_team_id()
            spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
            if not spaces_data:
                return {"error": "Failed to fetch spaces"}

            space_id = None
            for space in spaces_data.get("spaces", []):
                if space["name"].lower() == space_name.lower():
                    space_id = space["id"]
                    space_name = space["name"]  # Use exact API name
                    break

            if not space_id:
                return {"error": f"Space '{space_name}' not found"}

            # ================================================================
            # STEP 3: Discover space structure — identify projects
            # Projects = folders (their name is the project) + folderless lists
            # ================================================================
            print("[PROGRESS] Discovering space structure (folders & lists)...")
            sys.stdout.flush()

            # project_key -> { "name": str, "type": "folder"|"list",
            #                   "id": str, "list_ids": [str] }
            projects = {}

            # 3a. Folders → each folder is a project
            resp_folders, _ = _api_call("GET", f"/space/{space_id}/folder")
            folders = resp_folders.get("folders", []) if resp_folders else []

            for folder in folders:
                folder_id = folder["id"]
                folder_name = folder.get("name", "Unnamed Folder")
                folder_list_ids = [lst["id"] for lst in folder.get("lists", [])]

                if folder_list_ids:
                    projects[f"folder_{folder_id}"] = {
                        "name": folder_name,
                        "type": "folder",
                        "id": folder_id,
                        "list_ids": folder_list_ids,
                    }

            # 3b. Folderless lists → each list is a project
            resp_lists, _ = _api_call("GET", f"/space/{space_id}/list")
            folderless_lists = resp_lists.get("lists", []) if resp_lists else []

            for lst in folderless_lists:
                list_id_val = lst["id"]
                list_name = lst.get("name", "Unnamed List")

                projects[f"list_{list_id_val}"] = {
                    "name": list_name,
                    "type": "list",
                    "id": list_id_val,
                    "list_ids": [list_id_val],
                }

            if not projects:
                return {
                    "error": f"No projects (folders or lists) found in space '{space_name}'"
                }

            print(
                f"[DEBUG] Found {len(projects)} projects "
                f"({sum(1 for p in projects.values() if p['type'] == 'folder')} folders, "
                f"{sum(1 for p in projects.values() if p['type'] == 'list')} standalone lists)"
            )
            sys.stdout.flush()

            # ================================================================
            # STEP 4: Fetch ALL tasks across the space at once (efficient)
            # ================================================================
            all_list_ids = []
            list_id_to_project = {}  # map list_id → project key
            for proj_key, proj in projects.items():
                for lid in proj["list_ids"]:
                    all_list_ids.append(lid)
                    list_id_to_project[lid] = proj_key

            print(
                f"[PROGRESS] Fetching tasks from {len(all_list_ids)} lists across {len(projects)} projects..."
            )
            sys.stdout.flush()

            all_tasks = _fetch_all_tasks(
                all_list_ids, {}, include_archived=include_archived
            )

            print(f"[DEBUG] Fetched {len(all_tasks)} total tasks in space")
            sys.stdout.flush()

            if not all_tasks:
                return {
                    "space_name": space_name,
                    "period": f"{start_date} to {end_date}",
                    "message": "No tasks found in this space",
                    "projects": {},
                }

            # ================================================================
            # STEP 5: Fetch time entries for tasks that have actual time logged
            # ================================================================
            timed_tasks = [t for t in all_tasks if int(t.get("time_spent") or 0) > 0]
            task_ids = [t["id"] for t in timed_tasks]
            print(
                f"[PROGRESS] Fetching time entries for {len(task_ids):,}/{len(all_tasks):,} tasks "
                f"({len(all_tasks) - len(task_ids):,} skipped — zero time_spent)"
            )
            sys.stdout.flush()
            time_entries_map = _fetch_time_entries_smart(task_ids, start_ms, end_ms)

            # ================================================================
            # STEP 6: Calculate metrics & build report per project
            # ================================================================
            print("[PROGRESS] Building project-wise report...")
            sys.stdout.flush()

            metrics = _calculate_task_metrics(all_tasks)

            # Initialize project reports
            project_reports = {}
            for proj_key, proj in projects.items():
                project_reports[proj_key] = {
                    "project_name": proj["name"],
                    "project_type": proj["type"],
                    "project_id": proj["id"],
                    "total_tasks": 0,
                    "tasks_with_time_in_range": 0,
                    "time_tracked_ms": 0,
                    "time_estimate_ms": 0,
                    "team_members": {},
                }

            for task in all_tasks:
                task_id = task["id"]
                task_list_id = task.get("list", {}).get("id", "")

                # Determine which project this task belongs to
                proj_key = list_id_to_project.get(task_list_id)
                if not proj_key or proj_key not in project_reports:
                    continue

                report = project_reports[proj_key]
                report["total_tasks"] += 1

                # Get time entries and filter by date range
                task_time_entries = time_entries_map.get(task_id, [])
                if not task_time_entries:
                    continue

                # Get per-user time in range
                user_time_map = filter_time_entries_by_user_and_date_range(
                    task_time_entries, start_ms, end_ms
                )

                total_time_in_range = sum(user_time_map.values())
                if total_time_in_range == 0:
                    continue

                report["tasks_with_time_in_range"] += 1
                report["time_tracked_ms"] += total_time_in_range

                # Add estimated time (direct to avoid double-counting)
                est_direct = metrics.get(task_id, {}).get("est_direct", 0)
                report["time_estimate_ms"] += est_direct

                # Per-member breakdown
                for username, time_tracked in user_time_map.items():
                    member = report["team_members"].setdefault(
                        username,
                        {"tasks": 0, "time_tracked_ms": 0, "time_estimate_ms": 0},
                    )
                    member["tasks"] += 1
                    member["time_tracked_ms"] += time_tracked
                    # Split estimate among users who tracked time
                    if user_time_map:
                        member["time_estimate_ms"] += est_direct // len(user_time_map)

            # ================================================================
            # STEP 7: Format the final output
            # ================================================================
            formatted_projects = []
            grand_total_tracked = 0
            grand_total_estimated = 0
            grand_total_tasks = 0

            for proj_key, report in project_reports.items():
                tracked = report["time_tracked_ms"]
                estimated = report["time_estimate_ms"]
                grand_total_tracked += tracked
                grand_total_estimated += estimated
                grand_total_tasks += report["tasks_with_time_in_range"]

                # Format team members
                formatted_members = {}
                for member_name, member_data in report["team_members"].items():
                    formatted_members[member_name] = {
                        "tasks": member_data["tasks"],
                        "time_tracked": _format_duration(
                            member_data["time_tracked_ms"]
                        ),
                        "time_estimate": _format_duration(
                            member_data["time_estimate_ms"]
                        ),
                    }

                proj_entry = {
                    "project_name": report["project_name"],
                    "project_type": report["project_type"],
                    "total_tasks_in_project": report["total_tasks"],
                    "tasks_with_time_in_range": report["tasks_with_time_in_range"],
                    "time_tracked": _format_duration(tracked),
                    "time_estimate": _format_duration(estimated),
                    "team_breakdown": formatted_members,
                    "_sort_ms": tracked,  # internal sort key, removed below
                }

                # Only include projects that have activity in the date range
                if report["tasks_with_time_in_range"] > 0:
                    formatted_projects.append(proj_entry)

            # Sort projects by time tracked (descending) using raw ms stored above
            formatted_projects.sort(key=lambda p: p.pop("_sort_ms", 0), reverse=True)

            # Also include projects with no activity for completeness
            inactive_projects = []
            for proj_key, report in project_reports.items():
                if report["tasks_with_time_in_range"] == 0:
                    inactive_projects.append(
                        {
                            "project_name": report["project_name"],
                            "project_type": report["project_type"],
                            "total_tasks_in_project": report["total_tasks"],
                            "note": "No time tracked in the selected period",
                        }
                    )

            result = {
                "space_name": space_name,
                "period": f"{start_date} to {end_date}",
                "period_type": period_type,
                "grand_total_time_tracked": _format_duration(grand_total_tracked),
                "grand_total_time_estimate": _format_duration(grand_total_estimated),
                "total_projects": len(projects),
                "active_projects": len(formatted_projects),
                "total_tasks_with_time": grand_total_tasks,
                "projects": formatted_projects,
                "note": (
                    "Projects are identified as: folders (folder name = project) "
                    "and standalone folderless lists (list name = project). "
                    "Only tasks/subtasks worked on during the date range are included."
                ),
            }

            if inactive_projects:
                result["inactive_projects"] = inactive_projects

            print(
                f"✅ Report complete: {len(formatted_projects)} active projects, "
                f"{len(inactive_projects)} inactive projects"
            )
            sys.stdout.flush()

            return result

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_space_project_time_report: {e}")
            print(traceback.format_exc())
            return {"error": str(e), "traceback": traceback.format_exc()}

    @mcp.tool()
    def get_time_report_detailed(
        report_type: str = "team_member",
        project: Optional[str] = None,
        space_name: Optional[str] = None,
        list_id: Optional[str] = None,
        week_selector: Optional[str] = None,
        week_start: Optional[str] = None,
        week_end: Optional[str] = None,
        timezone_offset: str = "+05:30",  # IST by default
        group_by_date: bool = True,  # Group intervals by date for cleaner output
    ) -> dict:

        try:
            from clickup_mcp.status_helpers import (
                format_time_entry_interval,
                format_duration_simple,
                group_intervals_by_date,
            )

            # ================================================================
            # STEP 1: Calculate week boundaries (same as original)
            # ================================================================
            if week_start and week_end:
                is_valid, error_msg = is_valid_monday_sunday_range(week_start, week_end)
                if not is_valid:
                    return {"error": f"Invalid week dates: {error_msg}"}
            elif week_selector:
                try:
                    week_start, week_end = parse_week_input(week_selector)
                except ValueError as e:
                    return {"error": f"Invalid week_selector: {str(e)}"}
            else:
                week_start, week_end = get_current_week_dates()

            start_ms, end_ms = date_range_to_timestamps(week_start, week_end)

            print(
                f"[DEBUG] Detailed report: {week_start} to {week_end} ({timezone_offset})"
            )

            # ================================================================
            # STEP 2: Resolve lists based on report type
            # ================================================================
            if report_type == "team_member":
                if not project and not list_id:
                    return {
                        "error": "Provide project or list_id for team_member report"
                    }
                list_ids = _resolve_to_list_ids(project, list_id)
                if not list_ids:
                    return {"error": "No lists found for project/list_id"}

            elif report_type == "space":
                if not space_name:
                    return {"error": "space_name required for space report"}

                team_id = _get_team_id()
                spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
                if not spaces_data:
                    return {"error": "Failed to fetch spaces"}

                space_id = None
                for space in spaces_data.get("spaces", []):
                    if space["name"].lower() == space_name.lower():
                        space_id = space["id"]
                        break

                if not space_id:
                    return {"error": f"Space '{space_name}' not found"}

                list_ids = []
                resp, _ = _api_call("GET", f"/space/{space_id}/list")
                if resp:
                    list_ids.extend([lst["id"] for lst in resp.get("lists", [])])

                resp2, _ = _api_call("GET", f"/space/{space_id}/folder")
                if resp2:
                    for folder in resp2.get("folders", []):
                        list_ids.extend([lst["id"] for lst in folder.get("lists", [])])

                if not list_ids:
                    return {"error": f"No lists found in space '{space_name}'"}
            else:
                return {
                    "error": f"Invalid report_type: '{report_type}'. Use 'team_member' or 'space'"
                }

            # ================================================================
            # STEP 3: Fetch all tasks and their time entries
            # ================================================================
            all_tasks = _fetch_all_tasks(list_ids, {}, include_archived=True)
            print(f"[DEBUG] Found {len(all_tasks)} total tasks")

            if not all_tasks:
                return {
                    "week_start": week_start,
                    "week_end": week_end,
                    "report": {},
                    "message": "No tasks found",
                }

            timed_tasks = [t for t in all_tasks if int(t.get("time_spent") or 0) > 0]
            task_ids = [t["id"] for t in timed_tasks]
            print(
                f"[DEBUG] Fetching time entries for {len(task_ids):,}/{len(all_tasks):,} tasks"
                f" ({len(all_tasks) - len(task_ids):,} skipped — zero time_spent)"
            )
            time_entries_map = _fetch_time_entries_smart(task_ids, start_ms, end_ms)

            # ================================================================
            # STEP 4: Build detailed report by person
            # ================================================================
            report = {}

            for task in all_tasks:
                task_id = task["id"]
                task_name = task.get("name", "Unnamed Task")
                task_url = task.get("url", f"https://app.clickup.com/t/{task_id}")
                task_status = task.get("status", {})
                status_name = (
                    task_status.get("status", "Unknown")
                    if isinstance(task_status, dict)
                    else "Unknown"
                )

                time_entries = time_entries_map.get(task_id, [])
                if not time_entries:
                    continue

                task_intervals_in_range = []
                task_time_in_range = 0

                for entry in time_entries:
                    for interval in entry.get("intervals", []):
                        interval_start = interval.get("start")
                        if not interval_start:
                            continue

                        try:
                            interval_start = int(interval_start)
                        except (ValueError, TypeError):
                            continue

                        if start_ms <= interval_start <= end_ms:
                            duration = interval.get("time", 0)
                            task_time_in_range += int(duration)

                            formatted_interval = format_time_entry_interval(
                                interval, timezone_offset
                            )
                            task_intervals_in_range.append(formatted_interval)

                if task_time_in_range == 0:
                    continue

                assignees = [u["username"] for u in task.get("assignees", [])] or [
                    "Unassigned"
                ]

                for member in assignees:
                    if member not in report:
                        report[member] = {
                            "summary": {
                                "total_time_tracked_ms": 0,
                                "total_tasks": 0,
                                "total_intervals": 0,
                            },
                            "tasks": [],
                        }

                    if group_by_date and task_intervals_in_range:
                        intervals_by_date = group_intervals_by_date(
                            task_intervals_in_range
                        )
                    else:
                        intervals_by_date = None

                    task_entry = {
                        "task_id": task_id,
                        "task_name": task_name,
                        "task_url": task_url,
                        "task_status": status_name,
                        "time_on_task_ms": task_time_in_range,
                        "time_on_task": format_duration_simple(task_time_in_range),
                        "intervals_count": len(task_intervals_in_range),
                    }

                    if intervals_by_date:
                        task_entry["by_date"] = intervals_by_date
                    else:
                        task_entry["time_entries"] = task_intervals_in_range

                    report[member]["tasks"].append(task_entry)

                    report[member]["summary"]["total_time_tracked_ms"] += (
                        task_time_in_range
                    )
                    report[member]["summary"]["total_tasks"] += 1
                    report[member]["summary"]["total_intervals"] += len(
                        task_intervals_in_range
                    )

            # ================================================================
            # STEP 5: Format summaries and sort tasks
            # ================================================================
            for member, data in report.items():
                data["summary"]["total_time_tracked"] = format_duration_simple(
                    data["summary"]["total_time_tracked_ms"]
                )

                data["tasks"].sort(key=lambda x: x["time_on_task_ms"], reverse=True)

            return {
                "report_type": f"{report_type}_detailed",
                "week_start": week_start,
                "week_end": week_end,
                "timezone": timezone_offset,
                "report": report,
                "total_people": len(report),
                "note": "Time entries are filtered by when they were logged, not when tasks were updated",
            }

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_time_report_detailed: {e}")
            print(traceback.format_exc())
            return {"error": str(e), "traceback": traceback.format_exc()}

    # ============================================================================
    # CONVENIENCE WRAPPER: Get task list for a specific person
    # ============================================================================

    @mcp.tool()
    def get_person_tasks_with_time(
        person_name: str,
        project: Optional[str] = None,
        space_name: Optional[str] = None,
        week_selector: Optional[str] = None,
        week_start: Optional[str] = None,
        week_end: Optional[str] = None,
    ) -> dict:
        """
        Get tasks a person worked on with timestamps (wrapper for detailed report).

        Args:
            person_name: Team member name
            project: Project name
            space_name: Space name
            week_selector: Week selector (previous, 2-weeks-ago, etc.)
            week_start, week_end: YYYY-MM-DD format

        Returns:
            Filtered report for specified person's tasks
        """
        if project:
            report_type = "team_member"
        elif space_name:
            report_type = "space"
        else:
            return {"error": "Provide either project or space_name"}

        full_report = get_time_report_detailed(
            report_type=report_type,
            project=project,
            space_name=space_name,
            week_selector=week_selector,
            week_start=week_start,
            week_end=week_end,
        )

        if "error" in full_report:
            return full_report

        person_data = full_report.get("report", {}).get(person_name)

        if not person_data:
            available_people = list(full_report.get("report", {}).keys())
            return {
                "error": f"Person '{person_name}' not found in report",
                "available_people": available_people,
                "hint": "Check the spelling or try one of the available names",
            }

        return {
            "person_name": person_name,
            "week_start": full_report["week_start"],
            "week_end": full_report["week_end"],
            "summary": person_data["summary"],
            "tasks": person_data["tasks"],
        }

    @mcp.tool()
    def get_task_status_distribution(
        project: Optional[str] = None,
        list_id: Optional[str] = None,
        include_archived: bool = True,
    ) -> dict:
        """
        Get actual task status distribution.

        Args:
            project: Project name
            list_id: Direct list ID
            include_archived: Include archived tasks

        Returns:
            Distribution of tasks across statuses
        """
        try:
            if not (list_ids := _resolve_to_list_ids(project, list_id)):
                return {"error": "No context found"}

            all_tasks = _fetch_all_tasks(
                list_ids, {}, include_archived=include_archived
            )

            status_counts = {}
            category_counts = {
                "not_started": 0,
                "active": 0,
                "done": 0,
                "closed": 0,
                "other": 0,
            }

            for t in all_tasks:
                status_obj = (
                    t.get("status", {}) if isinstance(t.get("status"), dict) else {}
                )
                status_name = _extract_status_name(t)
                cat = get_status_category(status_name, status_obj.get("type"))

                status_counts[status_name] = status_counts.get(status_name, 0) + 1

                if cat in category_counts:
                    category_counts[cat] += 1
                else:
                    category_counts["other"] += 1

            return {
                "project": project or "Direct list",
                "total_tasks": len(all_tasks),
                "status_distribution": status_counts,
                "category_distribution": category_counts,
                "include_archived": include_archived,
            }

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_task_status_distribution: {e}")
            print(traceback.format_exc())
            return {"error": str(e)}

    @mcp.tool()
    def get_space_time_report_comprehensive(
        space_name: Optional[str] = None,
        space_id: Optional[str] = None,
        period_type: str = "this_week",
        custom_start: Optional[str] = None,
        custom_end: Optional[str] = None,
        rolling_days: Optional[int] = None,
        group_by: str = "assignee",
        include_archived: bool = True,
        async_job: bool = False,
        job_id: Optional[str] = None,
    ) -> dict:
        """
        Time tracking report for a SPACE with period filters.

        Args:
            space_name: Name of the ClickUp space (e.g., "JewelleryOS")
            space_id: Direct space ID
            period_type: today, yesterday, this_week, last_week, this_month, last_month, this_year, last_30_days, rolling, custom
            custom_start, custom_end: YYYY-MM-DD format for custom period
            rolling_days: Number of days (1-365) for rolling period
            group_by: assignee, folder, or status
            include_archived: Include archived tasks
            async_job: Run in background (auto-triggers at 500+ tasks)
            job_id: Internal use for background jobs

        Returns:
            Report with time tracked, estimates, task counts grouped by assignee/folder/status (or job_id if async)

        Example:
            get_space_time_report_comprehensive(space_name="JewelleryOS", period_type="this_week", group_by="assignee")
        """
        try:
            from clickup_mcp.status_helpers import parse_time_period_filter
            import sys

            print(
                "⌛ Processing space report - this may take 1-3 minutes for large spaces..."
            )
            sys.stdout.flush()

            # Result returned directly — no LLM polling needed
            if not job_id:
                return _run_job_and_wait(
                    lambda: get_space_time_report_comprehensive(
                        space_name=space_name,
                        space_id=space_id,
                        period_type=period_type,
                        custom_start=custom_start,
                        custom_end=custom_end,
                        rolling_days=rolling_days,
                        group_by=group_by,
                        include_archived=include_archived,
                        async_job=False,
                        job_id="_bg_",
                    )
                )

            if not space_id and not space_name:
                return {"error": "Provide either space_name or space_id"}

            # Parse the time period to get date range
            try:
                start_date, end_date = parse_time_period_filter(
                    period_type=period_type,
                    custom_start=custom_start,
                    custom_end=custom_end,
                    rolling_days=rolling_days,
                )
            except ValueError as e:
                return {"error": f"Invalid period specification: {str(e)}"}

            print(
                f"[DEBUG] Period: {period_type}, Date range: {start_date} to {end_date}"
            )

            if not space_id:
                team_id = _get_team_id()
                spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
                if not spaces_data:
                    return {"error": "Failed to fetch spaces"}

                for space in spaces_data.get("spaces", []):
                    if space["name"].lower() == space_name.lower():
                        space_id = space["id"]
                        space_name = space["name"]  # Use exact name from API
                        break

                if not space_id:
                    return {"error": f"Space '{space_name}' not found"}

            # Get all lists in space
            list_ids = []

            # Folderless lists
            resp, _ = _api_call("GET", f"/space/{space_id}/list")
            if resp:
                list_ids.extend([lst["id"] for lst in resp.get("lists", [])])

            # Lists from folders
            resp2, _ = _api_call("GET", f"/space/{space_id}/folder")
            folder_map = {}  # Map list_id to folder_name for grouping
            if resp2:
                for folder in resp2.get("folders", []):
                    folder_name = folder.get("name", "Unknown Folder")
                    for lst in folder.get("lists", []):
                        list_ids.append(lst["id"])
                        folder_map[lst["id"]] = folder_name

            if not list_ids:
                return {"error": f"No lists found in space '{space_name}'"}

            print(f"[DEBUG] Found {len(list_ids)} lists in space")

            # Fetch all tasks
            all_tasks = _fetch_all_tasks(
                list_ids, {}, include_archived=include_archived
            )
            print(f"[DEBUG] Fetched {len(all_tasks)} tasks from space")

            if not all_tasks:
                return {
                    "space_name": space_name,
                    "space_id": space_id,
                    "period_type": period_type,
                    "start_date": start_date,
                    "end_date": end_date,
                    "message": "No tasks found in this space",
                    "report": {},
                }

            timed_tasks = [t for t in all_tasks if int(t.get("time_spent") or 0) > 0]
            task_ids = [t["id"] for t in timed_tasks]
            print(
                f"[DEBUG] Fetching time entries for {len(task_ids):,}/{len(all_tasks):,} tasks"
                f" ({len(all_tasks) - len(task_ids):,} skipped — zero time_spent)"
            )
            # Convert date range to timestamps
            start_ms, end_ms = date_range_to_timestamps(start_date, end_date)
            time_entries_map = _fetch_time_entries_smart(task_ids, start_ms, end_ms)

            from clickup_mcp.status_helpers import (
                filter_time_entries_by_user_and_date_range,
            )

            report = {}
            metrics = _calculate_task_metrics(all_tasks)

            for task in all_tasks:
                task_id = task["id"]
                task_time_entries = time_entries_map.get(task_id, [])

                m = metrics.get(task_id, {})
                est_direct = m.get("est_direct", 0)

                if group_by == "assignee":
                    user_time_map = filter_time_entries_by_user_and_date_range(
                        task_time_entries, start_ms, end_ms
                    )

                    for username, time_tracked in user_time_map.items():
                        if username not in report:
                            report[username] = {
                                "tasks": 0,
                                "time_tracked": 0,
                                "time_estimate": 0,
                            }
                        report[username]["tasks"] += 1
                        report[username]["time_tracked"] += time_tracked
                        report[username]["time_estimate"] += est_direct

                elif group_by == "folder":
                    total_time_ms, filtered_intervals = (
                        filter_time_entries_by_date_range(
                            task_time_entries, start_ms, end_ms
                        )
                    )
                    list_id = task.get("list", {}).get("id")
                    folder_name = folder_map.get(list_id, "Folderless")

                    if total_time_ms > 0:
                        if folder_name not in report:
                            report[folder_name] = {
                                "tasks": 0,
                                "time_tracked": 0,
                                "time_estimate": 0,
                            }
                        report[folder_name]["tasks"] += 1
                        report[folder_name]["time_tracked"] += total_time_ms
                        report[folder_name]["time_estimate"] += est_direct
                else:  # status
                    total_time_ms, filtered_intervals = (
                        filter_time_entries_by_date_range(
                            task_time_entries, start_ms, end_ms
                        )
                    )
                    status_name = _extract_status_name(task)

                    if total_time_ms > 0:
                        if status_name not in report:
                            report[status_name] = {
                                "tasks": 0,
                                "time_tracked": 0,
                                "time_estimate": 0,
                                "intervals_count": 0,
                            }
                        report[status_name]["tasks"] += 1
                        report[status_name]["time_tracked"] += total_time_ms
                        report[status_name]["time_estimate"] += est_direct
                        report[status_name]["intervals_count"] += len(
                            filtered_intervals
                        )

            # Format the report with ONLY human-readable values
            formatted = {
                key: {
                    "tasks": value["tasks"],
                    "time_tracked": _format_duration(value["time_tracked"]),
                    "time_estimate": _format_duration(value["time_estimate"]),
                }
                for key, value in report.items()
            }

            total_tracked = sum(v["time_tracked"] for v in report.values())
            total_estimate = sum(v["time_estimate"] for v in report.values())

            return {
                "period": f"{start_date} to {end_date}",
                "space": space_name,
                "group_by": group_by,
                "total_time_tracked": _format_duration(total_tracked),
                "total_time_estimate": _format_duration(total_estimate),
                "note": "Only includes tasks/subtasks worked on during the date range. Estimates use task-level values to prevent double-counting.",
                "report": formatted,
            }

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_space_time_report_comprehensive: {e}")
            print(traceback.format_exc())
            return {"error": str(e)}

    @mcp.tool()
    def get_folder_time_report_comprehensive(
        folder_name: Optional[str] = None,
        folder_id: Optional[str] = None,
        space_name: Optional[str] = None,
        period_type: str = "this_week",
        custom_start: Optional[str] = None,
        custom_end: Optional[str] = None,
        rolling_days: Optional[int] = None,
        group_by: str = "assignee",
        include_archived: bool = True,
        async_job: bool = False,
        job_id: Optional[str] = None,
    ) -> dict:
        """
        Time tracking report for a FOLDER with period filters.

        Args:
            folder_name: Folder name (requires space_name)
            folder_id: Direct folder ID
            space_name: Space name (required with folder_name)
            period_type: today, yesterday, this_week, last_week, this_month, last_month, this_year, last_30_days, rolling, custom
            custom_start, custom_end: YYYY-MM-DD format for custom period
            rolling_days: Number of days (1-365) for rolling period
            group_by: assignee, list, or status
            include_archived: Include archived tasks
            async_job: Run in background (auto-triggers at 500+ tasks)
            job_id: Internal use for background jobs

        Returns:
            Report with time tracked, estimates, task counts grouped by assignee/list/status (or job_id if async)

        Example:
            get_folder_time_report_comprehensive(folder_name="Luminique", space_name="JewelleryOS", period_type="this_week")
        """
        try:
            from clickup_mcp.status_helpers import parse_time_period_filter
            import sys

            print(
                "⌛ Processing folder report - this may take 1-3 minutes for large folders..."
            )
            sys.stdout.flush()

            if not job_id:
                return _run_job_and_wait(
                    lambda: get_folder_time_report_comprehensive(
                        folder_name=folder_name,
                        folder_id=folder_id,
                        space_name=space_name,
                        period_type=period_type,
                        custom_start=custom_start,
                        custom_end=custom_end,
                        rolling_days=rolling_days,
                        group_by=group_by,
                        include_archived=include_archived,
                        async_job=False,
                        job_id="_bg_",
                    )
                )

            if not folder_id and not folder_name:
                return {"error": "Provide either folder_name or folder_id"}

            if folder_name and not space_name:
                return {"error": "space_name is required when using folder_name"}

            try:
                start_date, end_date = parse_time_period_filter(
                    period_type=period_type,
                    custom_start=custom_start,
                    custom_end=custom_end,
                    rolling_days=rolling_days,
                )
            except ValueError as e:
                return {"error": f"Invalid period specification: {str(e)}"}

            print(
                f"[DEBUG] Period: {period_type}, Date range: {start_date} to {end_date}"
            )

            if not folder_id:
                team_id = _get_team_id()
                spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
                if not spaces_data:
                    return {"error": "Failed to fetch spaces"}

                space_id = None
                for space in spaces_data.get("spaces", []):
                    if space["name"].lower() == space_name.lower():
                        space_id = space["id"]
                        break

                if not space_id:
                    return {"error": f"Space '{space_name}' not found"}

                folders_resp, _ = _api_call("GET", f"/space/{space_id}/folder")
                if not folders_resp:
                    return {"error": f"Failed to fetch folders in space '{space_name}'"}

                for folder in folders_resp.get("folders", []):
                    if folder["name"].lower() == folder_name.lower():
                        folder_id = folder["id"]
                        folder_name = folder["name"]  # Use exact name from API
                        break

                if not folder_id:
                    return {
                        "error": f"Folder '{folder_name}' not found in space '{space_name}'"
                    }

            lists_resp, _ = _api_call("GET", f"/folder/{folder_id}/list")
            if not lists_resp:
                return {"error": "Failed to fetch lists in folder"}

            list_ids = []
            list_map = {}  # Map list_id to list_name for grouping
            for lst in lists_resp.get("lists", []):
                list_id = lst["id"]
                list_ids.append(list_id)
                list_map[list_id] = lst.get("name", "Unnamed List")

            if not list_ids:
                return {
                    "error": f"No lists found in folder '{folder_name or folder_id}'"
                }

            print(f"[DEBUG] Found {len(list_ids)} lists in folder")

            all_tasks = _fetch_all_tasks(
                list_ids, {}, include_archived=include_archived
            )
            print(f"[DEBUG] Fetched {len(all_tasks)} tasks from folder")

            if not all_tasks:
                return {
                    "folder_name": folder_name or "Unknown",
                    "folder_id": folder_id,
                    "period_type": period_type,
                    "start_date": start_date,
                    "end_date": end_date,
                    "message": "No tasks found in this folder",
                    "report": {},
                }

            if len(all_tasks) >= 500 and not job_id:
                print(
                    f"⚡ AUTO-ASYNC: {len(all_tasks)} tasks detected. Switching to background mode to prevent timeout."
                )
                sys.stdout.flush()
                return get_folder_time_report_comprehensive(
                    folder_name=folder_name,
                    folder_id=folder_id,
                    space_name=space_name,
                    period_type=period_type,
                    custom_start=custom_start,
                    custom_end=custom_end,
                    rolling_days=rolling_days,
                    group_by=group_by,
                    include_archived=include_archived,
                    async_job=True,
                )

            timed_tasks = [t for t in all_tasks if int(t.get("time_spent") or 0) > 0]
            task_ids = [t["id"] for t in timed_tasks]
            print(
                f"[DEBUG] Fetching time entries for {len(task_ids):,}/{len(all_tasks):,} tasks"
                f" ({len(all_tasks) - len(task_ids):,} skipped — zero time_spent)"
            )
            # Convert date range to timestamps
            start_ms, end_ms = date_range_to_timestamps(start_date, end_date)
            time_entries_map = _fetch_time_entries_smart(task_ids, start_ms, end_ms)

            from clickup_mcp.status_helpers import (
                filter_time_entries_by_user_and_date_range,
            )

            report = {}
            metrics = _calculate_task_metrics(all_tasks)

            for task in all_tasks:
                task_id = task["id"]
                task_time_entries = time_entries_map.get(task_id, [])

                m = metrics.get(task_id, {})
                est_direct = m.get("est_direct", 0)

                if group_by == "assignee":
                    user_time_map = filter_time_entries_by_user_and_date_range(
                        task_time_entries, start_ms, end_ms
                    )

                    for username, time_tracked in user_time_map.items():
                        if username not in report:
                            report[username] = {
                                "tasks": 0,
                                "time_tracked": 0,
                                "time_estimate": 0,
                            }
                        report[username]["tasks"] += 1
                        report[username]["time_tracked"] += time_tracked
                        report[username]["time_estimate"] += est_direct

                elif group_by == "list":
                    total_time_ms, filtered_intervals = (
                        filter_time_entries_by_date_range(
                            task_time_entries, start_ms, end_ms
                        )
                    )
                    list_id = task.get("list", {}).get("id")
                    list_name = list_map.get(list_id, "Unknown List")

                    if total_time_ms > 0:
                        if list_name not in report:
                            report[list_name] = {
                                "tasks": 0,
                                "time_tracked": 0,
                                "time_estimate": 0,
                            }
                        report[list_name]["tasks"] += 1
                        report[list_name]["time_tracked"] += total_time_ms
                        report[list_name]["time_estimate"] += est_direct
                else:  # status
                    total_time_ms, filtered_intervals = (
                        filter_time_entries_by_date_range(
                            task_time_entries, start_ms, end_ms
                        )
                    )
                    status_name = _extract_status_name(task)

                    if total_time_ms > 0:
                        if status_name not in report:
                            report[status_name] = {
                                "tasks": 0,
                                "time_tracked": 0,
                                "time_estimate": 0,
                                "intervals_count": 0,
                            }
                        report[status_name]["tasks"] += 1
                        report[status_name]["time_tracked"] += total_time_ms
                        report[status_name]["time_estimate"] += est_direct
                        report[status_name]["intervals_count"] += len(
                            filtered_intervals
                        )

            # Format the report with ONLY human-readable values
            formatted = {
                key: {
                    "tasks": value["tasks"],
                    "time_tracked": _format_duration(value["time_tracked"]),
                    "time_estimate": _format_duration(value["time_estimate"]),
                }
                for key, value in report.items()
            }

            total_tracked = sum(v["time_tracked"] for v in report.values())
            total_estimate = sum(v["time_estimate"] for v in report.values())

            return {
                "period": f"{start_date} to {end_date}",
                "folder": folder_name or "Unknown",
                "space": space_name,
                "group_by": group_by,
                "total_time_tracked": _format_duration(total_tracked),
                "total_time_estimate": _format_duration(total_estimate),
                "note": "Only includes tasks/subtasks worked on during the date range. Estimates use task-level values to prevent double-counting.",
                "report": formatted,
            }

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_folder_time_report_comprehensive: {e}")
            print(traceback.format_exc())
            return {"error": str(e)}

    # ============================================================================
    # EMPLOYEE DAILY TIME REPORT (Timesheet + Time Reporting)
    # ============================================================================

    @mcp.tool()
    def get_employee_daily_time_report(
        period_type: str = "this_month",
        custom_start: Optional[str] = None,
        custom_end: Optional[str] = None,
        rolling_days: Optional[int] = None,
        space_name: Optional[str] = None,
        space_id: Optional[str] = None,
        folder_name: Optional[str] = None,
        folder_id: Optional[str] = None,
        list_id: Optional[str] = None,
        assignee_names: Optional[List[str]] = None,
        async_job: bool = False,
        job_id: Optional[str] = None,
    ) -> dict:
        """
        Employee daily time report — replicates ClickUp's Timesheet & Time Reporting.

        Generates a matrix of Employee × Day showing how much time each team member
        tracked on each day, with task-level detail, over a given period.

        Default period is this_month (most common use case for timesheet review).

        Args:
            period_type: today, yesterday, this_week, last_week, this_month, last_month,
                         this_year, last_30_days, rolling, custom (default: this_month)
            custom_start, custom_end: YYYY-MM-DD format for custom period
            rolling_days: Number of days (1-365) for rolling period
            space_name: Optional filter — only entries in this space
            space_id: Optional filter — direct space ID
            folder_name: Optional filter — only entries in this folder (requires space_name)
            folder_id: Optional filter — direct folder ID
            list_id: Optional filter — only entries in this list
            assignee_names: Optional list of usernames to filter (default: all members)
            async_job: Run in background to prevent timeout (default: False)
            job_id: Internal use for background jobs

        Returns:
            Employee-wise daily time report with:
            - timesheet: Employee × Day matrix (daily hours)
            - summary: Per-employee totals (time tracked, estimated, tasks, avg daily)
            - daily_totals: Team-wide totals per day
            - task_details: Per-employee per-task breakdown
            (or job_id if async)

        Examples:
            get_employee_daily_time_report()
            get_employee_daily_time_report(period_type="last_month")
            get_employee_daily_time_report(space_name="JewelleryOS", period_type="this_week")
            get_employee_daily_time_report(folder_name="Luminique", space_name="JewelleryOS")
            get_employee_daily_time_report(assignee_names=["Arya", "Mehul"])
            get_employee_daily_time_report(period_type="this_month", async_job=True)
        """
        try:
            from clickup_mcp.status_helpers import (
                parse_time_period_filter,
                get_workspace_members,
            )
            from clickup_mcp.time_stamp_helpers import format_duration_simple
            from collections import defaultdict
            import sys

            print("⌛ Processing employee daily time report...")
            sys.stdout.flush()

            # ================================================================
            # ASYNC JOB SUPPORT: Always run async (task-based fetch is heavy)
            # The team-level endpoint is not available with our token, so we
            # must use the slower task-based approach which always needs async.
            # ================================================================
            if not job_id:
                return _run_job_and_wait(
                    lambda: get_employee_daily_time_report(
                        period_type=period_type,
                        custom_start=custom_start,
                        custom_end=custom_end,
                        rolling_days=rolling_days,
                        space_name=space_name,
                        space_id=space_id,
                        folder_name=folder_name,
                        folder_id=folder_id,
                        list_id=list_id,
                        assignee_names=assignee_names,
                        async_job=False,
                        job_id="_bg_",
                    )
                )

            # ================================================================
            # STEP 1: Parse date range
            # ================================================================
            try:
                start_date, end_date = parse_time_period_filter(
                    period_type=period_type,
                    custom_start=custom_start,
                    custom_end=custom_end,
                    rolling_days=rolling_days,
                )
            except ValueError as e:
                return {"error": f"Invalid period specification: {str(e)}"}

            print(
                f"[DEBUG] Period: {period_type}, Date range: {start_date} to {end_date}"
            )
            sys.stdout.flush()

            start_ms, end_ms = date_range_to_timestamps(start_date, end_date)

            # ================================================================
            # STEP 2: Get workspace members to build assignee filter
            # ================================================================
            team_id = _get_team_id()
            members_result = get_workspace_members()

            if "error" in members_result:
                # Retry up to 2 more times — ClickUp API can be flaky
                for retry in range(2):
                    print(
                        f"[WARN] Workspace members fetch failed (attempt {retry + 1}/3): {members_result['error']}. Retrying..."
                    )
                    sys.stdout.flush()
                    time.sleep(2)
                    members_result = get_workspace_members()
                    if "error" not in members_result:
                        break
                else:
                    return {
                        "error": f"Failed to get workspace members after 3 attempts: {members_result['error']}"
                    }

            all_members = members_result.get("members", [])
            # Build user ID → username map
            user_id_to_name = {}
            user_name_to_id = {}
            for m in all_members:
                uid = str(m.get("id", ""))
                uname = m.get("username", "Unknown")
                user_id_to_name[uid] = uname
                user_name_to_id[uname.lower()] = uid

            # Filter to specific assignees if requested
            if assignee_names:
                target_user_ids = []
                not_found = []
                for name in assignee_names:
                    uid = user_name_to_id.get(name.lower())
                    if uid:
                        target_user_ids.append(uid)
                    else:
                        # Fuzzy match: check if any username contains the search term
                        matched = False
                        for uname_lower, uid_val in user_name_to_id.items():
                            if name.lower() in uname_lower:
                                target_user_ids.append(uid_val)
                                matched = True
                                break
                        if not matched:
                            not_found.append(name)

                if not_found:
                    return {
                        "error": f"Assignees not found: {not_found}",
                        "available_members": [m["username"] for m in all_members],
                        "hint": "Use exact username or a substring match",
                    }
            else:
                # All members
                target_user_ids = [str(m["id"]) for m in all_members if m.get("id")]

            if not target_user_ids:
                return {"error": "No team members found to query"}

            # ================================================================
            # STEP 3: Resolve location filter (space/folder/list)
            # ================================================================
            location_filter = {}

            if list_id:
                location_filter["list_id"] = list_id
            elif folder_id:
                location_filter["folder_id"] = folder_id
            elif folder_name and space_name:
                # Resolve folder ID from space
                if not space_id:
                    spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
                    if spaces_data:
                        for s in spaces_data.get("spaces", []):
                            if s["name"].lower() == space_name.lower():
                                space_id = s["id"]
                                break
                    if not space_id:
                        return {"error": f"Space '{space_name}' not found"}

                folders_resp, _ = _api_call("GET", f"/space/{space_id}/folder")
                if folders_resp:
                    for f in folders_resp.get("folders", []):
                        if f["name"].lower() == folder_name.lower():
                            location_filter["folder_id"] = f["id"]
                            break
                    if "folder_id" not in location_filter:
                        return {
                            "error": f"Folder '{folder_name}' not found in space '{space_name}'"
                        }
            elif space_name or space_id:
                if not space_id:
                    spaces_data, _ = _api_call("GET", f"/team/{team_id}/space")
                    if spaces_data:
                        for s in spaces_data.get("spaces", []):
                            if s["name"].lower() == space_name.lower():
                                space_id = s["id"]
                                break
                    if not space_id:
                        return {"error": f"Space '{space_name}' not found"}
                location_filter["space_id"] = space_id

            # ================================================================
            # STEP 4: Fetch time entries via task-based approach
            # (Team-level endpoint requires admin token which we don't have)
            # ================================================================
            all_time_entries = []
            local_task_map = {}

            print("⏳ Step 4a: Resolving lists for task-based time entry fetch...")
            sys.stdout.flush()

            # Resolve list IDs based on location filter
            target_list_ids = []

            if list_id:
                target_list_ids = [list_id]
            elif "folder_id" in location_filter:
                fid = location_filter["folder_id"]
                r, _ = _api_call("GET", f"/folder/{fid}/list")
                if r:
                    target_list_ids = [lst["id"] for lst in r.get("lists", [])]
            elif "space_id" in location_filter:
                sid = location_filter["space_id"]
                # Folderless lists
                r, _ = _api_call("GET", f"/space/{sid}/list")
                if r:
                    target_list_ids.extend([lst["id"] for lst in r.get("lists", [])])
                # Lists inside folders
                r, _ = _api_call("GET", f"/space/{sid}/folder")
                if r:
                    for folder in r.get("folders", []):
                        fr, _ = _api_call("GET", f"/folder/{folder['id']}/list")
                        if fr:
                            target_list_ids.extend(
                                [lst["id"] for lst in fr.get("lists", [])]
                            )
            else:
                # ENTIRE WORKSPACE (All Spaces)
                print(
                    "⚠️ Fetching ALL lists from all spaces in workspace... This is a heavy operation."
                )
                sys.stdout.flush()
                r, _ = _api_call("GET", f"/team/{team_id}/space")
                if r:
                    for space in r.get("spaces", []):
                        sid = space["id"]
                        lr, _ = _api_call("GET", f"/space/{sid}/list")
                        if lr:
                            target_list_ids.extend(
                                [lst["id"] for lst in lr.get("lists", [])]
                            )
                        fr, _ = _api_call("GET", f"/space/{sid}/folder")
                        if fr:
                            for folder in fr.get("folders", []):
                                flr, _ = _api_call(
                                    "GET", f"/folder/{folder['id']}/list"
                                )
                                if flr:
                                    target_list_ids.extend(
                                        [lst["id"] for lst in flr.get("lists", [])]
                                    )

            print(f"[DEBUG] Found {len(target_list_ids)} lists to scan.")
            sys.stdout.flush()

            if target_list_ids:
                # Fetch all tasks (including archived + closed + subtasks)
                print(
                    f"⏳ Step 4b: Fetching tasks from {len(target_list_ids)} lists..."
                )
                sys.stdout.flush()
                all_tasks = _fetch_all_tasks(target_list_ids, {}, include_archived=True)
                print(f"[DEBUG] Fetched {len(all_tasks)} total tasks")
                sys.stdout.flush()

                local_task_map = {t["id"]: t for t in all_tasks}

                timed_tasks = [
                    t for t in all_tasks if int(t.get("time_spent") or 0) > 0
                ]
                task_ids = [t["id"] for t in timed_tasks]
                print(
                    f"⏳ Step 4c: Fetching time entries for {len(task_ids):,}/{len(all_tasks):,} tasks"
                    f" ({len(all_tasks) - len(task_ids):,} skipped -- zero time_spent)..."
                )
                sys.stdout.flush()
                task_entries_map = _fetch_time_entries_smart(task_ids, start_ms, end_ms)

                # Flatten results
                for entries in task_entries_map.values():
                    all_time_entries.extend(entries)

            print(f"[DEBUG] Total time entries fetched: {len(all_time_entries)}")
            sys.stdout.flush()

            if not all_time_entries:
                return {
                    "period": f"{start_date} to {end_date}",
                    "period_type": period_type,
                    "location_filter": location_filter or "entire workspace",
                    "message": "No time entries found for the specified period and filters",
                    "timesheet": {},
                    "summary": {},
                }

            # ================================================================
            # STEP 5: Build the Employee × Day matrix
            # ================================================================
            # Structure: { username: { "YYYY-MM-DD": { ms, tasks } } }
            employee_daily = defaultdict(
                lambda: defaultdict(
                    lambda: {
                        "time_ms": 0,
                        "tasks": defaultdict(
                            lambda: {
                                "time_ms": 0,
                                "task_name": "",
                                "task_id": "",
                                "intervals": 0,
                            }
                        ),
                    }
                )
            )

            # Also track per-employee totals and task estimation
            employee_totals = defaultdict(
                lambda: {
                    "total_time_ms": 0,
                    "total_intervals": 0,
                    "task_ids": set(),
                    "days_worked": set(),
                }
            )

            all_dates = set()

            for entry in all_time_entries:
                # Extract user
                user = entry.get("user", {})
                user_id = str(user.get("id", ""))
                username = user.get("username") or user_id_to_name.get(
                    user_id, "Unknown"
                )

                # Extract task info
                task = entry.get("task", {}) or {}
                task_id = task.get("id", "no_task")
                task_name = task.get("name", "")

                # Enrich task name if missing (common in fallback task-based fetch)
                if not task_name and task_id in local_task_map:
                    task_name = local_task_map[task_id].get("name", "Unknown Task")

                if not task_name:
                    task_name = "No Task"

                # Process intervals — handle both formats:
                # Format A (team-level): entry has "intervals" array
                # Format B (task-level): entry itself has start/end/duration (intervals may be empty)
                intervals = entry.get("intervals", [])

                # If no intervals array (or empty), treat the entry itself as a single interval
                if not intervals:
                    entry_start = entry.get("start")
                    entry_duration = entry.get("duration") or entry.get("time", 0)
                    if entry_start:
                        intervals = [
                            {
                                "start": entry_start,
                                "end": entry.get("end"),
                                "time": entry_duration,
                            }
                        ]

                for interval in intervals:
                    interval_start = interval.get("start")
                    interval_end = interval.get("end")
                    duration = interval.get("time", 0)

                    if not interval_start:
                        continue

                    try:
                        interval_start_ms = int(interval_start)
                        duration_ms = int(duration) if duration else 0
                    except (ValueError, TypeError):
                        continue

                    # If duration is 0 but we have start+end, compute it
                    if duration_ms == 0 and interval_end:
                        try:
                            duration_ms = int(interval_end) - interval_start_ms
                        except (ValueError, TypeError):
                            pass

                    if duration_ms <= 0:
                        continue

                    # Only include intervals within the date range
                    if interval_start_ms < start_ms or interval_start_ms > end_ms:
                        continue

                    # Determine the date from the start timestamp
                    dt = datetime.fromtimestamp(
                        interval_start_ms / 1000, tz=timezone.utc
                    )
                    date_str = dt.strftime("%Y-%m-%d")
                    all_dates.add(date_str)

                    # Accumulate
                    day_data = employee_daily[username][date_str]
                    day_data["time_ms"] += duration_ms

                    task_data = day_data["tasks"][task_id]
                    task_data["time_ms"] += duration_ms
                    task_data["task_name"] = task_name
                    task_data["task_id"] = task_id
                    task_data["intervals"] += 1

                    # Totals
                    employee_totals[username]["total_time_ms"] += duration_ms
                    employee_totals[username]["total_intervals"] += 1
                    employee_totals[username]["task_ids"].add(task_id)
                    employee_totals[username]["days_worked"].add(date_str)

            # Sort dates chronologically
            sorted_dates = sorted(all_dates)

            # ================================================================
            # STEP 6: Format the timesheet matrix
            # ================================================================
            timesheet = {}
            for username in sorted(employee_daily.keys()):
                daily_data = employee_daily[username]
                timesheet[username] = {}
                for date_str in sorted_dates:
                    day = daily_data.get(date_str)
                    if day and day["time_ms"] > 0:
                        timesheet[username][date_str] = format_duration_simple(
                            day["time_ms"]
                        )
                    else:
                        timesheet[username][date_str] = "-"

            # ================================================================
            # STEP 7: Build employee summary (Time Reporting view)
            # ================================================================
            summary = {}
            for username, totals in sorted(employee_totals.items()):
                total_ms = totals["total_time_ms"]
                days_count = len(totals["days_worked"])
                avg_daily_ms = total_ms // days_count if days_count > 0 else 0

                summary[username] = {
                    "time_tracked": format_duration_simple(total_ms),
                    "time_tracked_ms": total_ms,
                    "total_tasks": len(totals["task_ids"]),
                    "total_intervals": totals["total_intervals"],
                    "days_worked": days_count,
                    "avg_daily": format_duration_simple(avg_daily_ms),
                }

            # ================================================================
            # STEP 8: Daily totals across the whole team
            # ================================================================
            daily_totals = {}
            for date_str in sorted_dates:
                day_total_ms = 0
                day_members_active = 0
                for username in employee_daily:
                    day = employee_daily[username].get(date_str)
                    if day and day["time_ms"] > 0:
                        day_total_ms += day["time_ms"]
                        day_members_active += 1

                # Get day name for readability
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                day_name = dt.strftime("%a")

                daily_totals[date_str] = {
                    "day": day_name,
                    "total_time": format_duration_simple(day_total_ms),
                    "active_members": day_members_active,
                }

            # ================================================================
            # STEP 9: Build per-employee task-level detail
            # ================================================================
            task_details = {}
            for username in sorted(employee_daily.keys()):
                # Aggregate across all days for this employee
                employee_tasks = defaultdict(
                    lambda: {"time_ms": 0, "task_name": "", "intervals": 0, "days": []}
                )
                for date_str in sorted_dates:
                    day = employee_daily[username].get(date_str)
                    if not day:
                        continue
                    for tid, tdata in day["tasks"].items():
                        if tdata["time_ms"] > 0:
                            employee_tasks[tid]["time_ms"] += tdata["time_ms"]
                            employee_tasks[tid]["task_name"] = tdata["task_name"]
                            employee_tasks[tid]["intervals"] += tdata["intervals"]
                            employee_tasks[tid]["days"].append(date_str)

                # Sort tasks by time tracked (descending)
                sorted_tasks = sorted(
                    employee_tasks.items(),
                    key=lambda x: x[1]["time_ms"],
                    reverse=True,
                )

                task_details[username] = [
                    {
                        "task_id": tid,
                        "task_name": tdata["task_name"],
                        "time_tracked": format_duration_simple(tdata["time_ms"]),
                        "intervals": tdata["intervals"],
                        "days_count": len(set(tdata["days"])),
                    }
                    for tid, tdata in sorted_tasks
                ]

            # ================================================================
            # STEP 10: Grand totals
            # ================================================================
            grand_total_ms = sum(t["total_time_ms"] for t in employee_totals.values())
            total_team_tasks = len(
                set().union(*(t["task_ids"] for t in employee_totals.values()))
            )

            return {
                "period": f"{start_date} to {end_date}",
                "period_type": period_type,
                "location_filter": location_filter or "entire workspace",
                "total_members": len(summary),
                "total_days": len(sorted_dates),
                "grand_total_time": format_duration_simple(grand_total_ms),
                "grand_total_tasks": total_team_tasks,
                "dates": sorted_dates,
                "timesheet": timesheet,
                "summary": summary,
                "daily_totals": daily_totals,
                "task_details": task_details,
                "note": "Timesheet shows daily tracked time per employee. '-' means no time tracked that day.",
            }

        except Exception as e:
            import traceback

            print(f"[DEBUG] Error in get_employee_daily_time_report: {e}")
            print(traceback.format_exc())
            return {"error": str(e), "traceback": traceback.format_exc()}
