"""
Report Generator — calls MCP server report tools via HTTP and formats results.

Uses SSE transport to call MCP tools, same as the AI client does.
For simplicity, we use direct ClickUp API calls instead, since the
report logic is well-understood and we avoid SSE complexity.
"""

import requests
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional

IST = timezone(timedelta(hours=5, minutes=30))


def _api_headers(token: str) -> dict:
    return {
        "Authorization": token,
        "Content-Type": "application/json",
    }


def _api_get(endpoint: str, token: str, params: dict = None) -> Optional[dict]:
    """Call ClickUp API directly with retry on 429 rate limit."""
    url = f"https://api.clickup.com/api/v2{endpoint}"
    for attempt in range(4):
        try:
            resp = requests.get(
                url, headers=_api_headers(token), params=params, timeout=30
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = min(2**attempt * 5, 30)
                time.sleep(wait)
                continue
            print(
                f"  [API ERROR] {resp.status_code} on {endpoint}: {resp.text[:200]}",
                flush=True,
            )
            return None
        except Exception as exc:
            print(f"  [API EXCEPTION] {endpoint}: {exc}", flush=True)
            return None
    return None


def _ms_to_readable(ms: int) -> str:
    """Convert milliseconds to 'Xh Ym' format."""
    if not ms or ms <= 0:
        return "0h 0m"
    hours = ms // 3_600_000
    minutes = (ms % 3_600_000) // 60_000
    return f"{hours}h {minutes}m"


def _ms_to_date_ist(ms: int) -> str:
    """Convert epoch-ms to YYYY-MM-DD IST."""
    if not ms:
        return "N/A"
    dt = datetime.fromtimestamp(ms / 1000, tz=IST)
    return dt.strftime("%Y-%m-%d")


def get_date_range(period: str) -> tuple:
    """
    Returns (start_date_str, end_date_str) for the given period.
    Supports: today, yesterday, this_week, last_week, this_month, last_month.
    """
    now = datetime.now(IST)
    if period == "yesterday":
        target = now - timedelta(days=1)
        start = target.replace(hour=0, minute=0, second=0, microsecond=0)
        end = target.replace(hour=23, minute=59, second=59, microsecond=999999)
    elif period == "this_week":
        monday = now - timedelta(days=now.weekday())
        start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    elif period == "last_week":
        last_monday = now - timedelta(days=now.weekday() + 7)
        last_sunday = last_monday + timedelta(days=6)
        start = last_monday.replace(hour=0, minute=0, second=0, microsecond=0)
        end = last_sunday.replace(hour=23, minute=59, second=59, microsecond=999999)
    elif period == "this_month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now
    elif period == "last_month":
        first_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_month_end = first_this_month - timedelta(minutes=1)
        start = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = last_month_end
    else:  # today (default)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


_spaces_cache: Optional[List[dict]] = None


def _get_monitored_projects_for_space(space_name: str) -> Dict[str, List[str]]:
    """
    Read monitoring_config.json and return {alias: [list_ids]} for the given space.
    Falls back to empty dict if config file is missing.
    """
    import json
    import os

    config_path = os.getenv("MONITORING_CONFIG_PATH", "/app/monitoring_config.json")
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except Exception:
        return {}

    projects: Dict[str, List[str]] = {}
    for p in cfg.get("monitored_projects", []):
        if p.get("space", "").lower() == space_name.lower():
            alias = p.get("alias", p.get("clickup_id", "Unknown"))
            projects[alias] = p.get("list_ids", [])
    return projects


def _resolve_space_id(space_name: str, token: str, team_id: str) -> Optional[str]:
    """Find space ID by name (caches the full spaces list)."""
    global _spaces_cache
    if _spaces_cache is None:
        data = _api_get(f"/team/{team_id}/space", token)
        _spaces_cache = data.get("spaces", []) if data else []
    for space in _spaces_cache:
        if space["name"].lower() == space_name.lower():
            return space["id"]
    return None


def _get_space_lists(space_id: str, token: str) -> Dict[str, List[str]]:
    """Get all list IDs grouped by project (folder or direct list)."""
    projects = {}

    # Folders
    folders_data = _api_get(f"/space/{space_id}/folder", token)
    if folders_data:
        for folder in folders_data.get("folders", []):
            list_ids = [lst["id"] for lst in folder.get("lists", [])]
            if list_ids:
                projects[folder["name"]] = list_ids

    # Folderless lists
    lists_data = _api_get(f"/space/{space_id}/list?archived=false", token)
    if lists_data:
        for lst in lists_data.get("lists", []):
            projects[lst["name"]] = [lst["id"]]

    return projects


def _fetch_tasks_from_lists(
    list_ids: List[str], token: str, include_closed: bool = True
) -> List[dict]:
    """Fetch all tasks from a list of list IDs using parallel workers."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch_list(list_id: str) -> List[dict]:
        tasks = []
        page = 0
        while True:
            params = {
                "page": page,
                "subtasks": "true",
                "include_closed": str(include_closed).lower(),
            }
            data = _api_get(f"/list/{list_id}/task", token, params)
            if not data:
                break
            batch = data.get("tasks", [])
            if not batch:
                break
            tasks.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return tasks

    all_tasks = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_fetch_list, lid): lid for lid in list_ids}
        for future in as_completed(futures):
            all_tasks.extend(future.result())
    return all_tasks


def _fetch_time_entries(
    task_ids: List[str], token: str, team_id: str, start_ms: int = 0, end_ms: int = 0
) -> Dict[str, List[dict]]:
    """Fetch time entries for the team in bulk with pagination, filter to relevant task_ids."""
    task_id_set = set(str(t) for t in task_ids)
    entries_by_task: Dict[str, List[dict]] = {}

    params: dict = {}
    if start_ms:
        params["start_date"] = str(start_ms)
    if end_ms:
        params["end_date"] = str(end_ms)

    # Paginate — ClickUp returns max 50 entries per call
    page = 0
    total_raw = 0
    while True:
        page_params = dict(params)
        page_params["page"] = str(page)
        data = _api_get(f"/team/{team_id}/time_entries", token, page_params)
        if data is None:
            print(f"  [TIME_ENTRIES] API call failed on page {page}", flush=True)
            break
        if "data" not in data:
            print(
                f"  [TIME_ENTRIES] Unexpected response keys: {list(data.keys())}",
                flush=True,
            )
            break
        batch = data["data"]
        total_raw += len(batch)
        for entry in batch:
            task_obj = entry.get("task") or {}
            tid = str(task_obj.get("id", ""))
            if tid and tid in task_id_set:
                entries_by_task.setdefault(tid, []).append(entry)
        if len(batch) < 50:
            break
        page += 1

    print(
        f"  [TIME_ENTRIES] {total_raw} raw entries fetched, "
        f"{len(entries_by_task)} tasks matched out of {len(task_id_set)} candidates",
        flush=True,
    )
    return entries_by_task


def generate_space_report(
    space_name: str,
    token: str,
    team_id: str,
    period: str = "today",
    scope: str = "full",
) -> dict:
    """
    Generate a space task report.

    Uses task.time_spent (cumulative) and task.date_updated for period filtering
    since manual time logging is used (no timer-based time entries).
    Active tasks = tasks updated within the period that have time_spent > 0.
    """
    start_date, end_date = get_date_range(period)

    # Convert dates to epoch ms for filtering
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=IST)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=IST
    )
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    # Resolve space (validates it exists; required even for monitored scope)
    space_id = _resolve_space_id(space_name, token, team_id)
    if not space_id:
        return {
            "space": space_name,
            "error": f"Space '{space_name}' not found",
            "period": f"{start_date} to {end_date}",
        }

    # Get projects (folders/lists) in the space
    if scope == "monitored":
        print(
            f"  [{space_name}] Using monitored scope (monitoring_config.json)...",
            flush=True,
        )
        projects = _get_monitored_projects_for_space(space_name)
        if not projects:
            print(
                f"  [{space_name}] WARNING: No monitored projects found, falling back to full scope",
                flush=True,
            )
            projects = _get_space_lists(space_id, token)
    else:
        print(f"  [{space_name}] Resolving lists...", flush=True)
        projects = _get_space_lists(space_id, token)

    if not projects:
        return {
            "space": space_name,
            "error": "No lists found in space",
            "period": f"{start_date} to {end_date}",
        }

    total_lists = sum(len(v) for v in projects.values())
    print(f"  [{space_name}] {len(projects)} projects, {total_lists} lists", flush=True)

    # Fetch all tasks across all projects
    print(f"  [{space_name}] Fetching tasks (parallel)...", flush=True)
    project_tasks: Dict[str, List[dict]] = {}
    total_task_count = 0
    for project_name, list_ids in projects.items():
        tasks = _fetch_tasks_from_lists(list_ids, token)
        if tasks:
            project_tasks[project_name] = tasks
            total_task_count += len(tasks)
    print(f"  [{space_name}] {total_task_count} tasks fetched", flush=True)

    # Build per-project reports using task.time_spent + task.date_updated for filtering
    project_reports = []
    grand_tracked_ms = 0
    grand_estimated_ms = 0
    grand_active_tasks = 0

    for project_name, tasks in project_tasks.items():
        member_data: Dict[str, dict] = {}
        project_tracked_ms = 0
        project_estimated_ms = 0
        active_task_count = 0

        for task in tasks:
            time_spent_ms = int(task.get("time_spent") or 0)
            time_estimate_ms = int(task.get("time_estimate") or 0)
            date_updated_ms = int(task.get("date_updated") or 0)

            project_estimated_ms += time_estimate_ms

            # Active in period = updated within the period AND has logged time
            if not (start_ms <= date_updated_ms <= end_ms):
                continue
            if time_spent_ms <= 0:
                continue

            active_task_count += 1
            project_tracked_ms += time_spent_ms

            # Attribute time to assignees (split evenly if multiple)
            assignees = task.get("assignees") or []
            if not assignees:
                assignees = [{"username": "Unassigned"}]
            share_ms = time_spent_ms // len(assignees)

            for assignee in assignees:
                user_name = (
                    assignee.get("username") or assignee.get("email") or "Unknown"
                )
                if user_name not in member_data:
                    member_data[user_name] = {"tracked_ms": 0, "tasks": set()}
                member_data[user_name]["tracked_ms"] += share_ms
                member_data[user_name]["tasks"].add(task["id"])

        if active_task_count == 0:
            continue

        members_list = [
            {
                "name": name,
                "tracked": _ms_to_readable(d["tracked_ms"]),
                "tracked_ms": d["tracked_ms"],
                "tasks_count": len(d["tasks"]),
            }
            for name, d in sorted(
                member_data.items(), key=lambda x: x[1]["tracked_ms"], reverse=True
            )
        ]

        project_reports.append(
            {
                "project": project_name,
                "active_tasks": active_task_count,
                "total_tasks": len(tasks),
                "tracked": _ms_to_readable(project_tracked_ms),
                "tracked_ms": project_tracked_ms,
                "estimated": _ms_to_readable(project_estimated_ms),
                "estimated_ms": project_estimated_ms,
                "members": members_list,
            }
        )

        grand_tracked_ms += project_tracked_ms
        grand_estimated_ms += project_estimated_ms
        grand_active_tasks += active_task_count

    print(
        f"  [{space_name}] Report done: {len(project_reports)} active projects, "
        f"{grand_active_tasks} tasks, tracked={_ms_to_readable(grand_tracked_ms)}",
        flush=True,
    )

    return {
        "space": space_name,
        "period": f"{start_date} to {end_date}",
        "total_projects": len(projects),
        "active_projects": len(project_reports),
        "total_active_tasks": grand_active_tasks,
        "total_tracked": _ms_to_readable(grand_tracked_ms),
        "total_estimated": _ms_to_readable(grand_estimated_ms),
        "projects": project_reports,
        "generated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "note": "Time shown is cumulative time_spent on tasks updated in this period.",
    }
