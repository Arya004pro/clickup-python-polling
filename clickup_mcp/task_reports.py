"""
Task Report Tools for ClickUp MCP Server
=========================================
PM-focused daily report tools covering the full reporting suite:

1. get_space_task_report       — Space-wise summary with per-project breakdown
2. get_project_task_report     — Project-wise with per-team-member breakdown
3. get_member_task_report      — Individual team member task report
4. get_low_hours_report        — Employees who tracked < 8 h on any day (with occurrence count)
5. get_missing_estimation_report — Employees whose tasks have no time estimate
6. get_overtime_report         — Employees where tracked time > estimated time

All period-based tools support:
  today | yesterday | this_week | last_week | this_month | last_month |
  this_year | last_30_days | rolling (+rolling_days) | custom (+custom_start/end YYYY-MM-DD)
"""

from __future__ import annotations

import json
import os
import sys
import time
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any

from fastmcp import FastMCP

from app.config import BASE_URL
from .api_client import client as _client
from .status_helpers import (
    date_range_to_timestamps,
    filter_time_entries_by_user_and_date_range,
    parse_time_period_filter,
    get_workspace_members,
)
from .pm_analytics import (
    _api_call,
    _get_team_id,
    _fetch_all_tasks,
    _fetch_time_entries_smart,
    _calculate_task_metrics,
    _format_duration,
    _extract_status_name,
    get_status_category,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_8H_MS = 8 * 60 * 60 * 1000  # 28 800 000 ms


def _ms_to_date_ist(ms: int, tz_offset_hours: float = 5.5) -> str:
    """Convert epoch-ms to YYYY-MM-DD adjusted for IST (+05:30)."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) + timedelta(
        hours=tz_offset_hours
    )
    return dt.strftime("%Y-%m-%d")


def _resolve_space_id(space_name: str) -> Optional[str]:
    team_id = _get_team_id()
    data, _ = _api_call("GET", f"/team/{team_id}/space")
    if not data:
        return None
    for s in data.get("spaces", []):
        if s["name"].lower() == space_name.lower():
            return s["id"]
    return None


def _resolve_space_lists(space_id: str) -> Dict[str, List[str]]:
    """
    Returns {"folder_name": [list_id, ...], "Folderless": [list_id, ...]}
    so callers know which project each list belongs to.
    """
    projects: Dict[str, List[str]] = {}

    resp_f, _ = _api_call("GET", f"/space/{space_id}/folder")
    for folder in (resp_f or {}).get("folders", []):
        lid_list = [lst["id"] for lst in folder.get("lists", [])]
        if lid_list:
            projects[folder["name"]] = lid_list

    resp_l, _ = _api_call("GET", f"/space/{space_id}/list")
    folderless = [lst["id"] for lst in (resp_l or {}).get("lists", [])]
    if folderless:
        projects["Folderless Lists"] = folderless

    return projects


def _list_ids_for_project(project_name: str) -> List[str]:
    """Resolve a project name (space/folder/list) → flat list of list IDs."""
    from .pm_analytics import _resolve_to_list_ids

    return _resolve_to_list_ids(project_name, None)


def _fetch_entries_by_period(
    task_ids: List[str],
    period_type: str,
    custom_start: Optional[str],
    custom_end: Optional[str],
    rolling_days: Optional[int],
):
    """Fetch time entries restricted to the requested period."""
    start_date, end_date = parse_time_period_filter(
        period_type=period_type,
        custom_start=custom_start,
        custom_end=custom_end,
        rolling_days=rolling_days,
    )
    start_ms, end_ms = date_range_to_timestamps(start_date, end_date)
    entries_map = _fetch_time_entries_smart(task_ids, start_ms, end_ms)
    return entries_map, start_date, end_date, start_ms, end_ms


# ---------------------------------------------------------------------------
# Registration function
# ---------------------------------------------------------------------------


def register_task_report_tools(mcp: FastMCP):
    """Register all task-report tools on the provided FastMCP instance."""

    # --- File-backed JOBS store (survives server restarts) ---
    _JOBS_FILE = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", ".jobs_cache.json"
    )
    _jobs_lock = threading.Lock()

    def _load_jobs() -> Dict[str, Dict]:
        """Load jobs from disk."""
        try:
            if os.path.exists(_JOBS_FILE):
                with open(_JOBS_FILE, "r") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_jobs(jobs: Dict[str, Dict]):
        """Persist jobs to disk."""
        try:
            with open(_JOBS_FILE, "w") as f:
                json.dump(jobs, f)
        except Exception:
            pass

    def _get_job(job_id: str) -> Optional[Dict]:
        """Get a single job by ID."""
        with _jobs_lock:
            jobs = _load_jobs()
            return jobs.get(job_id)

    def _set_job(job_id: str, data: Dict):
        """Set/update a single job."""
        with _jobs_lock:
            jobs = _load_jobs()
            jobs[job_id] = data
            # Prune old jobs — keep only the last 50
            if len(jobs) > 50:
                sorted_ids = sorted(
                    jobs.keys(),
                    key=lambda k: jobs[k].get("_created", 0),
                )
                for old_id in sorted_ids[: len(jobs) - 50]:
                    del jobs[old_id]
            _save_jobs(jobs)

    JOBS = _load_jobs()  # In-memory reference for backward compat

    # -------------------------------------------------------------------------
    # Unified async dispatcher — used by every report tool.
    #
    #   Hybrid: wait up to QUICK_WAIT seconds (under LM Studio's ~30s timeout).
    #   If done → return result directly. If still running → return job_id.
    # -------------------------------------------------------------------------
    QUICK_WAIT = 45.0  # seconds to wait inline before returning job_id

    def _dispatch(
        fn, async_job: bool = False, poll_interval: float = 5.0, max_wait: float = 600.0
    ) -> dict:
        """
        Run fn() in a background thread.
        - Waits up to QUICK_WAIT seconds inline.
        - If the report finishes within QUICK_WAIT: return the result directly.
        - If still running: return {job_id, still_running} — model calls
          get_task_report_job_result(job_id=...) once when ready.
        Jobs are persisted to disk so they survive server restarts.
        """
        jid = str(uuid.uuid4())
        _set_job(
            jid,
            {
                "status": "running",
                "result": None,
                "error": None,
                "_created": time.time(),
            },
        )

        done_event = threading.Event()

        def _bg() -> None:
            try:
                result = fn()
                _set_job(
                    jid,
                    {
                        "status": "finished",
                        "result": result,
                        "error": None,
                        "_created": time.time(),
                    },
                )
            except Exception as exc:
                import traceback

                traceback.print_exc()
                _set_job(
                    jid,
                    {
                        "status": "failed",
                        "result": None,
                        "error": str(exc),
                        "_created": time.time(),
                    },
                )
            finally:
                done_event.set()

        threading.Thread(target=_bg, daemon=True).start()

        # Wait up to QUICK_WAIT — return directly if fast enough
        done_event.wait(timeout=QUICK_WAIT)

        j = _get_job(jid) or {}
        status = j.get("status")
        if status == "finished":
            return j.get("result", {})
        if status == "failed":
            return {"error": j.get("error", "Report generation failed")}

        # Still running — return job reference (report continues in background)
        return {
            "job_id": jid,
            "status": "still_running",
            "INSTRUCTION": (
                "The report is still generating. "
                "In your VERY NEXT response call IMMEDIATELY (do NOT wait, do NOT tell the user to wait): "
                f"get_task_report_job_result(job_id='{jid}'). "
                "It will wait internally until the result is ready. "
                "NEVER ask the user to check — you MUST call the tool yourself right now."
            ),
        }

    # =========================================================================
    # 1. SPACE-WISE TASK REPORT
    # =========================================================================

    @mcp.tool()
    def get_space_task_report(
        space_name: str,
        period_type: str = "yesterday",
        custom_start: Optional[str] = None,
        custom_end: Optional[str] = None,
        rolling_days: Optional[int] = None,
        include_archived: bool = True,
        async_job: bool = True,
        job_id: Optional[str] = None,
    ) -> dict:
        """
        Space-Wise Task Report.

        Returns a two-level summary:
          • Space level  — total tasks worked on, time tracked, time estimated
          • Project level — per folder/list breakdown showing team-member activity

        Each project section lists every assignee with their task count, time
        tracked, and time estimated so the PM can see at a glance what each
        project team delivered.

        Args:
            space_name:     ClickUp space name (e.g. "AIX", "JewelleryOS")
            period_type:    today | yesterday | this_week | last_week |
                            this_month | last_month | this_year | last_30_days |
                            rolling | custom
            custom_start:   YYYY-MM-DD (required when period_type="custom")
            custom_end:     YYYY-MM-DD (required when period_type="custom")
            rolling_days:   Number of days for rolling window (1-365)
            include_archived: Include archived tasks (default True)
            async_job:      Run in background — always set True for large spaces

        Returns:
            {
              space_name, period, grand_total_time_tracked,
              grand_total_time_estimate, total_projects, active_projects,
              projects: [
                {
                  project_name, project_type,
                  time_tracked, time_estimate, tasks_worked_on,
                  team_breakdown: { member: {tasks, time_tracked, time_estimate} }
                }
              ],
              formatted_output: <ready-to-display markdown table>
            }
        """
        try:
            print(f"⌛ Space task report: '{space_name}' / {period_type}")
            sys.stdout.flush()

            # Dispatch: async_job=True (default) returns job_id immediately;
            # async_job=False blocks until done and returns result directly.
            if not job_id:

                def _work():
                    return get_space_task_report(
                        space_name=space_name,
                        period_type=period_type,
                        custom_start=custom_start,
                        custom_end=custom_end,
                        rolling_days=rolling_days,
                        include_archived=include_archived,
                        async_job=False,
                        job_id="_bg_",
                    )

                return _dispatch(_work, async_job)

            # --- Parse period ---
            start_date, end_date = parse_time_period_filter(
                period_type=period_type,
                custom_start=custom_start,
                custom_end=custom_end,
                rolling_days=rolling_days,
            )
            start_ms, end_ms = date_range_to_timestamps(start_date, end_date)

            # --- Resolve space ---
            space_id = _resolve_space_id(space_name)
            if not space_id:
                return {"error": f"Space '{space_name}' not found"}

            # --- Discover projects (folders + folderless lists) ---
            project_map = _resolve_space_lists(space_id)  # {project_name: [list_id]}
            if not project_map:
                return {"error": f"No projects found in space '{space_name}'"}

            all_list_ids = [lid for lids in project_map.values() for lid in lids]
            list_to_project = {
                lid: pname for pname, lids in project_map.items() for lid in lids
            }

            # --- Fetch tasks ---
            print(f"[PROGRESS] Fetching tasks from {len(all_list_ids)} lists...")
            sys.stdout.flush()
            all_tasks = _fetch_all_tasks(
                all_list_ids, {}, include_archived=include_archived
            )
            print(f"[DEBUG] {len(all_tasks)} total tasks")
            sys.stdout.flush()

            # --- Fetch time entries ---
            timed = [t for t in all_tasks if int(t.get("time_spent") or 0) > 0]
            entries_map = _fetch_time_entries_smart(
                [t["id"] for t in timed], start_ms, end_ms
            )
            metrics = _calculate_task_metrics(all_tasks)

            # --- Build per-project report ---
            project_reports: Dict[str, Dict] = {
                pname: {
                    "project_name": pname,
                    "project_type": "folder" if pname != "Folderless Lists" else "list",
                    "tasks_worked_on": 0,
                    "time_tracked_ms": 0,
                    "time_estimate_ms": 0,
                    "team_members": defaultdict(
                        lambda: {
                            "tasks": 0,
                            "time_tracked_ms": 0,
                            "time_estimate_ms": 0,
                        }
                    ),
                }
                for pname in project_map
            }

            for task in all_tasks:
                task_id = task["id"]
                list_id = task.get("list", {}).get("id", "")
                pname = list_to_project.get(list_id)
                if not pname:
                    continue

                user_time = filter_time_entries_by_user_and_date_range(
                    entries_map.get(task_id, []), start_ms, end_ms
                )
                total_ms = sum(user_time.values())
                if total_ms == 0:
                    continue

                pr = project_reports[pname]
                pr["tasks_worked_on"] += 1
                pr["time_tracked_ms"] += total_ms
                est = metrics.get(task_id, {}).get("est_direct", 0)
                pr["time_estimate_ms"] += est

                for username, t_ms in user_time.items():
                    mb = pr["team_members"][username]
                    mb["tasks"] += 1
                    mb["time_tracked_ms"] += t_ms
                    if user_time:
                        mb["time_estimate_ms"] += est // len(user_time)

            # --- Format output ---
            formatted_projects = []
            grand_tracked = 0
            grand_est = 0

            for pname, pr in project_reports.items():
                if pr["tasks_worked_on"] == 0:
                    continue
                grand_tracked += pr["time_tracked_ms"]
                grand_est += pr["time_estimate_ms"]

                team_fmt = {
                    mb: {
                        "tasks": d["tasks"],
                        "time_tracked": _format_duration(d["time_tracked_ms"]),
                        "time_estimate": _format_duration(d["time_estimate_ms"]),
                    }
                    for mb, d in pr["team_members"].items()
                }
                formatted_projects.append(
                    {
                        "project_name": pr["project_name"],
                        "project_type": pr["project_type"],
                        "tasks_worked_on": pr["tasks_worked_on"],
                        "time_tracked": _format_duration(pr["time_tracked_ms"]),
                        "time_estimate": _format_duration(pr["time_estimate_ms"]),
                        "team_breakdown": team_fmt,
                    }
                )

            formatted_projects.sort(
                key=lambda p: next(
                    (
                        v["time_tracked_ms"]
                        for v in [project_reports.get(p["project_name"], {})]
                    ),
                    0,
                ),
                reverse=True,
            )

            # --- Markdown formatted_output ---
            lines = [
                f"## Space Report: {space_name}",
                f"**Period:** {start_date} → {end_date}",
                f"**Total Time Tracked:** {_format_duration(grand_tracked)}  |  "
                f"**Total Estimated:** {_format_duration(grand_est)}",
                "",
            ]
            for fp in formatted_projects:
                lines.append(f"### {fp['project_name']} ({fp['project_type']})")
                lines.append(
                    f"Tasks worked on: **{fp['tasks_worked_on']}**  |  "
                    f"Tracked: **{fp['time_tracked']}**  |  "
                    f"Estimated: **{fp['time_estimate']}**"
                )
                if fp["team_breakdown"]:
                    lines.append("")
                    lines.append("| Member | Tasks | Time Tracked | Time Estimate |")
                    lines.append("|--------|------:|-------------:|--------------:|")
                    tot_tasks = 0
                    tot_tracked_ms = 0
                    for mb, d in sorted(fp["team_breakdown"].items()):
                        lines.append(
                            f"| {mb} | {d['tasks']} | {d['time_tracked']} | {d['time_estimate']} |"
                        )
                        tot_tasks += d["tasks"]
                    lines.append("")

            return {
                "space_name": space_name,
                "period": f"{start_date} to {end_date}",
                "period_type": period_type,
                "grand_total_time_tracked": _format_duration(grand_tracked),
                "grand_total_time_estimate": _format_duration(grand_est),
                "total_projects": len(project_map),
                "active_projects": len(formatted_projects),
                "projects": formatted_projects,
                "formatted_output": "\n".join(lines),
            }

        except Exception as e:
            import traceback

            print(traceback.format_exc())
            return {"error": str(e)}

    # =========================================================================
    # 2. PROJECT-WISE TASK REPORT
    # =========================================================================

    @mcp.tool()
    def get_project_task_report(
        project_name: str,
        period_type: str = "yesterday",
        custom_start: Optional[str] = None,
        custom_end: Optional[str] = None,
        rolling_days: Optional[int] = None,
        include_archived: bool = True,
        async_job: bool = True,
        job_id: Optional[str] = None,
    ) -> dict:
        """
        Project-Wise Task Report.

        For every team member who worked on this project in the selected period,
        shows:
          • How many tasks they completed / worked on
          • Total time they tracked
          • Total time estimated for those tasks
          • List of specific tasks with individual time breakdown

        Args:
            project_name:   Project name — can be a space, folder, or list
            period_type:    today | yesterday | this_week | last_week |
                            this_month | last_month | this_year | last_30_days |
                            rolling | custom
            custom_start:   YYYY-MM-DD (required when period_type="custom")
            custom_end:     YYYY-MM-DD (required when period_type="custom")
            rolling_days:   Number of days for rolling window (1-365)
            include_archived: Include archived tasks (default True)
            async_job:      Run in background (recommended for 300+ tasks)

        Returns:
            {
              project_name, period,
              total_members, total_tasks_worked,
              total_time_tracked, total_time_estimate,
              team_report: {
                member_name: {
                  tasks_count, time_tracked, time_estimate,
                  tasks: [{task_name, status, time_tracked, time_estimate}]
                }
              },
              formatted_output: <ready-to-display markdown table>
            }
        """
        try:
            print(f"⌛ Project task report: '{project_name}' / {period_type}")
            sys.stdout.flush()

            if not job_id:

                def _work():
                    return get_project_task_report(
                        project_name=project_name,
                        period_type=period_type,
                        custom_start=custom_start,
                        custom_end=custom_end,
                        rolling_days=rolling_days,
                        include_archived=include_archived,
                        async_job=False,
                        job_id="_bg_",
                    )

                return _dispatch(_work, async_job)

            start_date, end_date = parse_time_period_filter(
                period_type=period_type,
                custom_start=custom_start,
                custom_end=custom_end,
                rolling_days=rolling_days,
            )
            start_ms, end_ms = date_range_to_timestamps(start_date, end_date)

            list_ids = _list_ids_for_project(project_name)
            if not list_ids:
                return {"error": f"Project '{project_name}' not found"}

            all_tasks = _fetch_all_tasks(
                list_ids, {}, include_archived=include_archived
            )
            if not all_tasks:
                return {
                    "project_name": project_name,
                    "period": f"{start_date} to {end_date}",
                    "message": "No tasks found",
                }

            # Auto-promote to async for large datasets
            if len(all_tasks) >= 300 and not job_id and not async_job:
                return get_project_task_report(
                    project_name=project_name,
                    period_type=period_type,
                    custom_start=custom_start,
                    custom_end=custom_end,
                    rolling_days=rolling_days,
                    include_archived=include_archived,
                    async_job=True,
                )

            timed = [t for t in all_tasks if int(t.get("time_spent") or 0) > 0]
            entries_map = _fetch_time_entries_smart(
                [t["id"] for t in timed], start_ms, end_ms
            )
            metrics = _calculate_task_metrics(all_tasks)

            # Build per-member report with task detail
            team_report: Dict[str, Dict] = {}
            grand_tracked_ms = 0
            grand_est_ms = 0
            total_tasks_worked = 0

            for task in all_tasks:
                task_id = task["id"]
                user_time = filter_time_entries_by_user_and_date_range(
                    entries_map.get(task_id, []), start_ms, end_ms
                )
                total_ms = sum(user_time.values())
                if total_ms == 0:
                    continue

                total_tasks_worked += 1
                grand_tracked_ms += total_ms
                est = metrics.get(task_id, {}).get("est_direct", 0)
                grand_est_ms += est

                status = _extract_status_name(task)
                task_est_str = _format_duration(est)

                for username, t_ms in user_time.items():
                    if username not in team_report:
                        team_report[username] = {
                            "tasks_count": 0,
                            "time_tracked_ms": 0,
                            "time_estimate_ms": 0,
                            "tasks": [],
                        }
                    mb = team_report[username]
                    mb["tasks_count"] += 1
                    mb["time_tracked_ms"] += t_ms
                    mb["time_estimate_ms"] += est // len(user_time) if user_time else 0
                    mb["tasks"].append(
                        {
                            "task_name": task.get("name", "Unnamed"),
                            "status": status,
                            "time_tracked": _format_duration(t_ms),
                            "time_estimate": task_est_str,
                        }
                    )

            # Format final output
            formatted_team = {}
            for mb, d in team_report.items():
                # Sort tasks by time tracked (desc)
                d["tasks"].sort(
                    key=lambda x: _duration_to_ms(x["time_tracked"]), reverse=True
                )
                formatted_team[mb] = {
                    "tasks_count": d["tasks_count"],
                    "time_tracked": _format_duration(d["time_tracked_ms"]),
                    "time_estimate": _format_duration(d["time_estimate_ms"]),
                    "tasks": d["tasks"],
                }

            # --- Markdown formatted_output ---
            lines = [
                f"## Project Report: {project_name}",
                f"**Period:** {start_date} → {end_date}",
                f"**Tasks worked on:** {total_tasks_worked}  |  "
                f"**Time Tracked:** {_format_duration(grand_tracked_ms)}  |  "
                f"**Estimated:** {_format_duration(grand_est_ms)}",
                "",
                "| Member | Tasks | Time Tracked | Time Estimate |",
                "|--------|------:|-------------:|--------------:|",
            ]
            for mb, d in sorted(
                formatted_team.items(),
                key=lambda x: _duration_to_ms(x[1]["time_tracked"]),
                reverse=True,
            ):
                lines.append(
                    f"| {mb} | {d['tasks_count']} | {d['time_tracked']} | {d['time_estimate']} |"
                )
            lines.append(
                f"| **Total** | **{total_tasks_worked}** | "
                f"**{_format_duration(grand_tracked_ms)}** | "
                f"**{_format_duration(grand_est_ms)}** |"
            )

            return {
                "project_name": project_name,
                "period": f"{start_date} to {end_date}",
                "period_type": period_type,
                "total_members": len(formatted_team),
                "total_tasks_worked": total_tasks_worked,
                "total_time_tracked": _format_duration(grand_tracked_ms),
                "total_time_estimate": _format_duration(grand_est_ms),
                "team_report": formatted_team,
                "formatted_output": "\n".join(lines),
            }

        except Exception as e:
            import traceback

            print(traceback.format_exc())
            return {"error": str(e)}

    # =========================================================================
    # 3. TEAM MEMBER-WISE TASK REPORT
    # =========================================================================

    @mcp.tool()
    def get_member_task_report(
        member_name: str,
        project_name: Optional[str] = None,
        space_name: Optional[str] = None,
        period_type: str = "yesterday",
        custom_start: Optional[str] = None,
        custom_end: Optional[str] = None,
        rolling_days: Optional[int] = None,
        include_archived: bool = True,
        async_job: bool = True,
        job_id: Optional[str] = None,
    ) -> dict:
        """
        Team Member-Wise Task Report.

        Shows every task a specific team member worked on during the period,
        with time tracked, time estimated, and task status — giving the PM a
        full picture of that individual's day/week.

        Args:
            member_name:    Exact or partial ClickUp username
            project_name:   Narrow to a specific project/folder (optional)
            space_name:     Narrow to a specific space (optional)
            period_type:    today | yesterday | this_week | last_week |
                            this_month | last_month | this_year | last_30_days |
                            rolling | custom
            custom_start:   YYYY-MM-DD (required when period_type="custom")
            custom_end:     YYYY-MM-DD (required when period_type="custom")
            rolling_days:   Number of days for rolling window (1-365)
            include_archived: Include archived tasks (default True)

        Returns:
            {
              member_name, period,
              summary: {total_tasks, time_tracked, time_estimate, days_active},
              tasks: [{task_name, status, time_tracked, time_estimate, date}],
              formatted_output: <markdown>
            }
        """
        try:
            print(f"⌛ Member task report: '{member_name}' / {period_type}")
            sys.stdout.flush()

            if not job_id:

                def _work():
                    return get_member_task_report(
                        member_name=member_name,
                        project_name=project_name,
                        space_name=space_name,
                        period_type=period_type,
                        custom_start=custom_start,
                        custom_end=custom_end,
                        rolling_days=rolling_days,
                        include_archived=include_archived,
                        async_job=False,
                        job_id="_bg_",
                    )

                return _dispatch(_work, async_job)

            start_date, end_date = parse_time_period_filter(
                period_type=period_type,
                custom_start=custom_start,
                custom_end=custom_end,
                rolling_days=rolling_days,
            )
            start_ms, end_ms = date_range_to_timestamps(start_date, end_date)

            # Resolve list IDs
            if project_name:
                list_ids = _list_ids_for_project(project_name)
                scope = project_name
            elif space_name:
                sid = _resolve_space_id(space_name)
                if not sid:
                    return {"error": f"Space '{space_name}' not found"}
                proj_map = _resolve_space_lists(sid)
                list_ids = [lid for lids in proj_map.values() for lid in lids]
                scope = space_name
            else:
                return {"error": "Provide either project_name or space_name"}

            if not list_ids:
                return {"error": f"No lists found for scope '{scope}'"}

            all_tasks = _fetch_all_tasks(
                list_ids, {}, include_archived=include_archived
            )
            if not all_tasks:
                return {
                    "member_name": member_name,
                    "period": f"{start_date} to {end_date}",
                    "message": "No tasks found",
                }

            timed = [t for t in all_tasks if int(t.get("time_spent") or 0) > 0]
            entries_map = _fetch_time_entries_smart(
                [t["id"] for t in timed], start_ms, end_ms
            )
            metrics = _calculate_task_metrics(all_tasks)

            member_lower = member_name.lower()
            total_ms = 0
            total_est_ms = 0
            task_list = []
            days_active = set()

            for task in all_tasks:
                task_id = task["id"]
                user_time = filter_time_entries_by_user_and_date_range(
                    entries_map.get(task_id, []), start_ms, end_ms
                )

                # Match member (case-insensitive, partial)
                matched_ms = 0
                for uname, t_ms in user_time.items():
                    if member_lower in uname.lower():
                        matched_ms += t_ms

                if matched_ms == 0:
                    continue

                total_ms += matched_ms
                est = metrics.get(task_id, {}).get("est_direct", 0)
                total_est_ms += est

                # Collect which days the work happened (from raw intervals)
                raw_entries = entries_map.get(task_id, [])
                for entry in raw_entries:
                    uname = (entry.get("user") or {}).get("username", "")
                    if member_lower not in uname.lower():
                        continue
                    for iv in entry.get("intervals", []):
                        iv_start = int(iv.get("start") or 0)
                        if start_ms <= iv_start <= end_ms:
                            days_active.add(_ms_to_date_ist(iv_start))

                task_list.append(
                    {
                        "task_name": task.get("name", "Unnamed"),
                        "status": _extract_status_name(task),
                        "time_tracked": _format_duration(matched_ms),
                        "time_estimate": _format_duration(est),
                        "time_tracked_ms": matched_ms,
                    }
                )

            task_list.sort(key=lambda x: x.pop("time_tracked_ms"), reverse=True)

            # --- Markdown formatted_output ---
            lines = [
                f"## Member Report: {member_name}",
                f"**Scope:** {scope}  |  **Period:** {start_date} → {end_date}",
                f"**Time Tracked:** {_format_duration(total_ms)}  |  "
                f"**Estimated:** {_format_duration(total_est_ms)}  |  "
                f"**Tasks:** {len(task_list)}  |  **Days active:** {len(days_active)}",
                "",
                "| Task | Status | Tracked | Estimated |",
                "|------|--------|--------:|----------:|",
            ]
            for t in task_list:
                lines.append(
                    f"| {t['task_name']} | {t['status']} | "
                    f"{t['time_tracked']} | {t['time_estimate']} |"
                )

            return {
                "member_name": member_name,
                "scope": scope,
                "period": f"{start_date} to {end_date}",
                "summary": {
                    "total_tasks": len(task_list),
                    "time_tracked": _format_duration(total_ms),
                    "time_estimate": _format_duration(total_est_ms),
                    "days_active": len(days_active),
                    "active_dates": sorted(days_active),
                },
                "tasks": task_list,
                "formatted_output": "\n".join(lines),
            }

        except Exception as e:
            import traceback

            print(traceback.format_exc())
            return {"error": str(e)}

    # =========================================================================
    # 4. LOW HOURS REPORT — Employees tracking < 8 hours on any day
    # =========================================================================

    @mcp.tool()
    def get_low_hours_report(
        period_type: str = "this_week",
        custom_start: Optional[str] = None,
        custom_end: Optional[str] = None,
        rolling_days: Optional[int] = None,
        min_hours: float = 8.0,
        space_name: Optional[str] = None,
        project_name: Optional[str] = None,
        async_job: bool = True,
        job_id: Optional[str] = None,
    ) -> dict:
        """
        Low Hours Report — employees who tracked fewer than N hours on any working day.

        Identifies team members who logged under the expected daily hours (default 8h)
        and shows exactly which days were short, by how much, and how many times this
        occurred — useful for spotting attendance or time-logging issues.

        Args:
            period_type:    today | yesterday | this_week | last_week |
                            this_month | last_month | this_year | last_30_days |
                            rolling | custom
            custom_start:   YYYY-MM-DD (required when period_type="custom")
            custom_end:     YYYY-MM-DD (required when period_type="custom")
            rolling_days:   Number of days for rolling window (1-365)
            min_hours:      Threshold in hours (default 8.0).  Days with tracked
                            time LESS THAN this value are flagged.
            space_name:     Narrow to a specific space (optional)
            project_name:   Narrow to a specific project (optional)
            async_job:      Run in background to avoid timeout

        Returns:
            {
              period, threshold_hours,
              flagged_members: [
                {
                  member_name,
                  total_days_below_threshold,  ← occurrence count
                  short_days: [
                    {date, tracked, shortfall, day_of_week}
                  ]
                }
              ],
              clean_members: [members with no short days],
              formatted_output: <markdown>
            }
        """
        try:
            print(f"⌛ Low hours report / {period_type} / threshold={min_hours}h")
            sys.stdout.flush()

            if not job_id:

                def _work():
                    return get_low_hours_report(
                        period_type=period_type,
                        custom_start=custom_start,
                        custom_end=custom_end,
                        rolling_days=rolling_days,
                        min_hours=min_hours,
                        space_name=space_name,
                        project_name=project_name,
                        async_job=False,
                        job_id="_bg_",
                    )

                return _dispatch(_work, async_job)

            start_date, end_date = parse_time_period_filter(
                period_type=period_type,
                custom_start=custom_start,
                custom_end=custom_end,
                rolling_days=rolling_days,
            )
            start_ms, end_ms = date_range_to_timestamps(start_date, end_date)
            threshold_ms = int(min_hours * 3600 * 1000)

            # Resolve list IDs
            if project_name:
                list_ids = _list_ids_for_project(project_name)
                scope = project_name
            elif space_name:
                sid = _resolve_space_id(space_name)
                if not sid:
                    return {"error": f"Space '{space_name}' not found"}
                proj_map = _resolve_space_lists(sid)
                list_ids = [lid for lids in proj_map.values() for lid in lids]
                scope = space_name
            else:
                # Entire workspace
                team_id = _get_team_id()
                data, _ = _api_call("GET", f"/team/{team_id}/space")
                list_ids = []
                for s in (data or {}).get("spaces", []):
                    pm = _resolve_space_lists(s["id"])
                    list_ids += [lid for lids in pm.values() for lid in lids]
                scope = "entire workspace"

            if not list_ids:
                return {"error": f"No lists found for scope '{scope}'"}

            all_tasks = _fetch_all_tasks(list_ids, {}, include_archived=True)

            if len(all_tasks) >= 300 and not job_id and not async_job:
                return get_low_hours_report(
                    period_type=period_type,
                    custom_start=custom_start,
                    custom_end=custom_end,
                    rolling_days=rolling_days,
                    min_hours=min_hours,
                    space_name=space_name,
                    project_name=project_name,
                    async_job=True,
                )

            timed = [t for t in all_tasks if int(t.get("time_spent") or 0) > 0]
            entries_map = _fetch_time_entries_smart(
                [t["id"] for t in timed], start_ms, end_ms
            )

            # Build per-member, per-day time map
            # { username: { "YYYY-MM-DD": total_ms } }
            member_day: Dict[str, Dict[str, int]] = defaultdict(
                lambda: defaultdict(int)
            )

            for task_entries in entries_map.values():
                for entry in task_entries:
                    uname = (entry.get("user") or {}).get("username", "Unknown")
                    for iv in entry.get("intervals", []):
                        iv_start = int(iv.get("start") or 0)
                        if start_ms <= iv_start <= end_ms:
                            duration = int(iv.get("time") or 0)
                            date_key = _ms_to_date_ist(iv_start)
                            member_day[uname][date_key] += duration

            flagged = []
            clean = []

            for uname, day_map in sorted(member_day.items()):
                short_days = []
                for date_str, day_ms in sorted(day_map.items()):
                    if day_ms < threshold_ms:
                        shortfall_ms = threshold_ms - day_ms
                        dow = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
                        short_days.append(
                            {
                                "date": date_str,
                                "day_of_week": dow,
                                "tracked": _format_duration(day_ms),
                                "shortfall": _format_duration(shortfall_ms),
                            }
                        )
                if short_days:
                    flagged.append(
                        {
                            "member_name": uname,
                            "total_days_below_threshold": len(short_days),
                            "short_days": short_days,
                        }
                    )
                else:
                    clean.append(uname)

            flagged.sort(key=lambda x: x["total_days_below_threshold"], reverse=True)

            # --- Markdown formatted_output ---
            lines = [
                f"## Low Hours Report (< {min_hours}h)",
                f"**Period:** {start_date} → {end_date}  |  **Scope:** {scope}",
                f"**{len(flagged)} member(s) with short days** | "
                f"**{len(clean)} member(s) fully compliant**",
                "",
            ]
            if flagged:
                lines.append("| Member | Occurrences | Worst Day (tracked) |")
                lines.append("|--------|------------:|---------------------|")
                for f in flagged:
                    worst = min(f["short_days"], key=lambda x: x["tracked"])
                    lines.append(
                        f"| {f['member_name']} | {f['total_days_below_threshold']} "
                        f"| {worst['date']} ({worst['tracked']}) |"
                    )
                lines.append("")
                for f in flagged:
                    lines.append(
                        f"### {f['member_name']} — {f['total_days_below_threshold']} short day(s)"
                    )
                    lines.append("| Date | Day | Tracked | Shortfall |")
                    lines.append("|------|-----|--------:|----------:|")
                    for sd in f["short_days"]:
                        lines.append(
                            f"| {sd['date']} | {sd['day_of_week']} | "
                            f"{sd['tracked']} | {sd['shortfall']} |"
                        )
                    lines.append("")

            if clean:
                lines.append(f"**Compliant members:** {', '.join(clean)}")

            return {
                "period": f"{start_date} to {end_date}",
                "period_type": period_type,
                "scope": scope,
                "threshold_hours": min_hours,
                "flagged_members": flagged,
                "clean_members": clean,
                "formatted_output": "\n".join(lines),
            }

        except Exception as e:
            import traceback

            print(traceback.format_exc())
            return {"error": str(e)}

    # =========================================================================
    # 5. MISSING ESTIMATION REPORT
    # =========================================================================

    @mcp.tool()
    def get_missing_estimation_report(
        project_name: Optional[str] = None,
        space_name: Optional[str] = None,
        period_type: Optional[str] = None,
        custom_start: Optional[str] = None,
        custom_end: Optional[str] = None,
        rolling_days: Optional[int] = None,
        include_done: bool = True,
        include_archived: bool = True,
        async_job: bool = True,
        job_id: Optional[str] = None,
    ) -> dict:
        """
        Missing Time Estimation Report.

        Lists tasks that have no time estimate, grouped by the person who tracked
        time on them.  For each member the report shows:
        - how many tasks are missing an estimate
        - how many of those tasks have time TRACKED (worked on but forgot to estimate)
        - total time tracked on unestimated tasks
        - a sample of task names (top 5 by status)

        When a period_type is given the report finds tasks that had time entries
        logged within that period and are missing an estimate.  This intentionally
        uses the TIME-ENTRY DATE (not the task creation date) so that old tasks
        worked on during the period are correctly included regardless of when
        they were created.

        When no period_type is given the report falls back to scanning every task
        in scope and groups them by task assignee instead.

        Args:
            project_name:   Narrow to a specific project/folder (optional)
            space_name:     Narrow to a specific space (optional)
            period_type:    Filter by the date time was TRACKED (not task creation):
                            today | yesterday | this_week | last_week |
                            this_month | last_month | this_year | last_30_days |
                            rolling | custom
                            Leave None to include all tasks regardless of date.
            custom_start:   YYYY-MM-DD (required when period_type="custom")
            custom_end:     YYYY-MM-DD (required when period_type="custom")
            rolling_days:   Number of days for rolling window (1-365)
            include_done:   Include tasks in done/closed status (default True)
            include_archived: Include archived tasks (default True)

        Returns:
            {
              scope, period, total_tasks_checked, total_missing_estimate,
              members: {
                member_name: {
                  missing_count, tasks_with_time_tracked, total_time_tracked,
                  total_time_tracked_ms,
                  tasks: [{task_name, status, list_name, task_id, time_tracked, time_tracked_ms}]
                }
              },
              unassigned_tasks: [{task_name, status, list_name, time_tracked}],
              formatted_output: <markdown>
            }
        """
        try:
            if not job_id:

                def _work():
                    return get_missing_estimation_report(
                        project_name=project_name,
                        space_name=space_name,
                        period_type=period_type,
                        custom_start=custom_start,
                        custom_end=custom_end,
                        rolling_days=rolling_days,
                        include_done=include_done,
                        include_archived=include_archived,
                        async_job=False,
                        job_id="_bg_",
                    )

                return _dispatch(_work, async_job)

            # --- Parse period filter (optional) ---
            start_ms: Optional[int] = None
            end_ms: Optional[int] = None
            period_label = "all time"

            if period_type:
                start_date, end_date = parse_time_period_filter(
                    period_type=period_type,
                    custom_start=custom_start,
                    custom_end=custom_end,
                    rolling_days=rolling_days,
                )
                start_ms, end_ms = date_range_to_timestamps(start_date, end_date)
                period_label = f"{start_date} → {end_date}"

            # --- Resolve scope ---
            if project_name:
                list_ids = _list_ids_for_project(project_name)
                scope = project_name
            elif space_name:
                sid = _resolve_space_id(space_name)
                if not sid:
                    return {"error": f"Space '{space_name}' not found"}
                proj_map = _resolve_space_lists(sid)
                list_ids = [lid for lids in proj_map.values() for lid in lids]
                scope = space_name
            else:
                return {"error": "Provide either project_name or space_name"}

            if not list_ids:
                return {"error": f"No lists found for scope '{scope}'"}

            all_tasks = _fetch_all_tasks(
                list_ids, {}, include_archived=include_archived
            )

            # Build a quick id→task lookup
            task_map: Dict[str, dict] = {t["id"]: t for t in all_tasks}

            # Bottom-up metrics: considers subtask time_estimates so a parent
            # with time_estimate=0 but estimated subtasks is NOT flagged.
            # est_total = own_estimate + all_subtask_estimates (recursive)
            metrics = _calculate_task_metrics(all_tasks)

            members: Dict[str, Dict] = {}
            unassigned = []
            total_missing = 0

            # ----------------------------------------------------------------
            # MODE A — period filter provided:
            #   Fetch time entries logged in the period → find tasks worked on
            #   in the period that have no estimate → group by tracker (not
            #   task assignee). A task created months ago will appear here if
            #   someone tracked time on it yesterday.
            #
            #   Subtask time entries ARE included because _fetch_all_tasks uses
            #   subtasks=true, so subtasks appear as individual tasks in
            #   all_tasks with their own time_spent ≠ 0, and are thus included
            #   in candidate_ids below.
            # ----------------------------------------------------------------
            if start_ms is not None:
                # Include tasks/subtasks that have any time tracked at all
                # (subtasks are already flattened into all_tasks via subtasks=true)
                candidate_ids = [
                    t["id"] for t in all_tasks if int(t.get("time_spent") or 0) > 0
                ]
                entries_map = _fetch_time_entries_smart(candidate_ids, start_ms, end_ms)

                # task_id → { username: tracked_ms_in_period }
                task_tracker_time: Dict[str, Dict[str, int]] = {}
                for tid, entries in entries_map.items():
                    for entry in entries:
                        uname = (entry.get("user") or {}).get("username", "Unknown")
                        intervals = entry.get("intervals") or []
                        if intervals:
                            # Timer-based entry: accumulate each interval that
                            # falls within the requested period.
                            for iv in intervals:
                                iv_start = int(iv.get("start") or 0)
                                if start_ms <= iv_start <= end_ms:
                                    duration = int(iv.get("time") or 0)
                                    if tid not in task_tracker_time:
                                        task_tracker_time[tid] = {}
                                    task_tracker_time[tid][uname] = (
                                        task_tracker_time[tid].get(uname, 0) + duration
                                    )
                        else:
                            # Manual entry: no intervals — use top-level
                            # start + duration fields instead.
                            entry_start = int(entry.get("start") or 0)
                            if start_ms <= entry_start <= end_ms:
                                duration = int(entry.get("duration") or 0)
                                if tid not in task_tracker_time:
                                    task_tracker_time[tid] = {}
                                task_tracker_time[tid][uname] = (
                                    task_tracker_time[tid].get(uname, 0) + duration
                                )

                # FIRST PASS: tasks with time entries in the period
                seen_task_ids: set = set()
                for tid, tracker_times in task_tracker_time.items():
                    task = task_map.get(tid)
                    if not task:
                        continue

                    # Optionally skip done/closed tasks
                    if not include_done:
                        sname = _extract_status_name(task)
                        status_obj = (
                            task.get("status", {})
                            if isinstance(task.get("status"), dict)
                            else {}
                        )
                        cat = get_status_category(sname, status_obj.get("type"))
                        if cat in ("done", "closed"):
                            continue

                    # Only flag tasks/subtasks that have NO estimate at any level.
                    # Use metrics est_total which rolls up estimates from all
                    # nested subtasks — prevents false positives where a parent
                    # task has est=0 but its subtasks have estimates set.
                    if metrics.get(tid, {}).get("est_total", 0) > 0:
                        continue

                    seen_task_ids.add(tid)
                    total_missing += 1
                    total_task_tracked_ms = sum(tracker_times.values())

                    task_entry = {
                        "task_name": task.get("name", "Unnamed"),
                        "status": _extract_status_name(task),
                        "list_name": task.get("list", {}).get("name", "Unknown"),
                        "task_id": tid,
                        "time_tracked": _format_duration(total_task_tracked_ms),
                        "time_tracked_ms": total_task_tracked_ms,
                    }

                    if not tracker_times:
                        unassigned.append(task_entry)
                    else:
                        for uname, user_tracked_ms in tracker_times.items():
                            if uname not in members:
                                members[uname] = {
                                    "missing_count": 0,
                                    "tasks_with_time_tracked": 0,
                                    "total_time_tracked_ms": 0,
                                    "tasks": [],
                                }
                            members[uname]["missing_count"] += 1
                            if user_tracked_ms > 0:
                                members[uname]["tasks_with_time_tracked"] += 1
                            members[uname]["total_time_tracked_ms"] += user_tracked_ms
                            task_entry_copy = dict(task_entry)
                            task_entry_copy["time_tracked"] = _format_duration(
                                user_tracked_ms
                            )
                            task_entry_copy["time_tracked_ms"] = user_tracked_ms
                            members[uname]["tasks"].append(task_entry_copy)

                # When a period filter is active we ONLY report tasks that had
                # time entries in that period — the second-pass sweep of all
                # unestimated tasks is intentionally skipped so the report
                # reflects "tasks actually worked on during this period that
                # are missing an estimate", not every unestimated task ever.
                checked_count = len(all_tasks)

            # ----------------------------------------------------------------
            # MODE B — no period filter: scan every task, group by assignee
            # ----------------------------------------------------------------
            else:
                for task in all_tasks:
                    # Optionally skip done/closed
                    if not include_done:
                        sname = _extract_status_name(task)
                        status_obj = (
                            task.get("status", {})
                            if isinstance(task.get("status"), dict)
                            else {}
                        )
                        cat = get_status_category(sname, status_obj.get("type"))
                        if cat in ("done", "closed"):
                            continue

                    # Only flag tasks with no estimate at any level (own + subtasks).
                    # est_total covers subtask estimates so parent tasks where
                    # only subtasks have estimates are not incorrectly flagged.
                    task_id_b = task["id"]
                    if metrics.get(task_id_b, {}).get("est_total", 0) > 0:
                        continue

                    total_missing += 1
                    task_time_spent = int(task.get("time_spent") or 0)
                    task_entry = {
                        "task_name": task.get("name", "Unnamed"),
                        "status": _extract_status_name(task),
                        "list_name": task.get("list", {}).get("name", "Unknown"),
                        "task_id": task["id"],
                        "time_tracked": _format_duration(task_time_spent),
                        "time_tracked_ms": task_time_spent,
                    }

                    assignees = task.get("assignees", [])
                    if not assignees:
                        unassigned.append(task_entry)
                    else:
                        for u in assignees:
                            uname = u.get("username", "Unknown")
                            if uname not in members:
                                members[uname] = {
                                    "missing_count": 0,
                                    "tasks_with_time_tracked": 0,
                                    "total_time_tracked_ms": 0,
                                    "tasks": [],
                                }
                            members[uname]["missing_count"] += 1
                            if task_time_spent > 0:
                                members[uname]["tasks_with_time_tracked"] += 1
                            members[uname]["total_time_tracked_ms"] += task_time_spent
                            members[uname]["tasks"].append(task_entry)

                checked_count = len(all_tasks)

            # Add formatted total_time_tracked to each member
            for mb_data in members.values():
                mb_data["total_time_tracked"] = _format_duration(
                    mb_data["total_time_tracked_ms"]
                )
                # Sort tasks: tasks with time tracked first, then by tracked ms desc
                mb_data["tasks"].sort(
                    key=lambda x: x.get("time_tracked_ms", 0), reverse=True
                )

            # Sort members by missing_count desc
            sorted_members = dict(
                sorted(
                    members.items(), key=lambda x: x[1]["missing_count"], reverse=True
                )
            )

            # --- Compute grand totals for the summary ---
            grand_tracked_ms = sum(
                d["total_time_tracked_ms"] for d in sorted_members.values()
            )
            grand_tasks_with_time = sum(
                d["tasks_with_time_tracked"] for d in sorted_members.values()
            )

            # --- Markdown formatted_output ---
            lines = [
                "## Missing Estimation Report",
                f"**Scope:** {scope}  |  **Period:** {period_label}",
                f"**Tasks checked:** {checked_count}  |  "
                f"**Tasks without estimate:** {total_missing}  |  "
                f"**Of which have time tracked:** {grand_tasks_with_time}  |  "
                f"**Total unestimated time:** {_format_duration(grand_tracked_ms)}",
                "",
                "| Member | Missing Est. | With Time Tracked | Time Tracked (No Est) |",
                "|--------|------------:|-----------------:|---------------------:|",
            ]
            for mb, d in sorted_members.items():
                lines.append(
                    f"| {mb} | {d['missing_count']} "
                    f"| {d['tasks_with_time_tracked']} "
                    f"| {d['total_time_tracked']} |"
                )
            if unassigned:
                unassigned_tracked_ms = sum(
                    t.get("time_tracked_ms", 0) for t in unassigned
                )
                unassigned_with_time = sum(
                    1 for t in unassigned if t.get("time_tracked_ms", 0) > 0
                )
                lines.append(
                    f"| *(Unassigned)* | {len(unassigned)} "
                    f"| {unassigned_with_time} "
                    f"| {_format_duration(unassigned_tracked_ms)} |"
                )
            lines.append(
                f"| **Total** | **{total_missing}** "
                f"| **{grand_tasks_with_time}** "
                f"| **{_format_duration(grand_tracked_ms)}** |"
            )

            # --- Per-member task samples (top 5 tasks with time tracked) ---
            lines.append("")
            lines.append(
                "### Per-Member Task Samples (tasks with time tracked but no estimate)"
            )
            lines.append("")
            for mb, d in sorted_members.items():
                tasks_with_time = [
                    t for t in d["tasks"] if t.get("time_tracked_ms", 0) > 0
                ]
                if not tasks_with_time:
                    continue
                sample = tasks_with_time[:5]
                lines.append(
                    f"**{mb}** ({d['tasks_with_time_tracked']} tasks with time tracked, "
                    f"total: {d['total_time_tracked']}):"
                )
                lines.append("")
                lines.append("| Task | Status | Time Tracked |")
                lines.append("|------|--------|------------:|")
                for t in sample:
                    lines.append(
                        f"| {t['task_name']} | {t['status']} | {t['time_tracked']} |"
                    )
                remaining = len(tasks_with_time) - len(sample)
                if remaining > 0:
                    lines.append(f"| *...and {remaining} more tasks* | | |")
                lines.append("")

            # --- Tasks with NO time tracked summary ---
            no_time_tasks_count = total_missing - grand_tasks_with_time
            if no_time_tasks_count > 0:
                lines.append(
                    f"### Additionally, **{no_time_tasks_count}** tasks have "
                    f"neither time estimate nor any time tracked."
                )
                lines.append("")

            return {
                "scope": scope,
                "period": period_label,
                "total_tasks_checked": checked_count,
                "total_missing_estimate": total_missing,
                "total_with_time_tracked": grand_tasks_with_time,
                "total_time_tracked_on_unestimated": _format_duration(grand_tracked_ms),
                "members": sorted_members,
                "unassigned_tasks": unassigned,
                "formatted_output": "\n".join(lines),
            }

        except Exception as e:
            import traceback

            print(traceback.format_exc())
            return {"error": str(e)}

    # =========================================================================
    # 6. OVERTIME REPORT — Tracked > Estimated
    # =========================================================================

    @mcp.tool()
    def get_overtime_report(
        project_name: Optional[str] = None,
        space_name: Optional[str] = None,
        period_type: str = "this_week",
        custom_start: Optional[str] = None,
        custom_end: Optional[str] = None,
        rolling_days: Optional[int] = None,
        min_overage_minutes: int = 15,
        include_archived: bool = True,
        async_job: bool = True,
        job_id: Optional[str] = None,
    ) -> dict:
        """
        Overtime Report — employees where tracked time exceeds estimated time.

        Identifies tasks and team members where actual time logged exceeded the
        estimate, showing how much over they went.  Only tasks with both an
        estimate AND logged time are included.

        Args:
            project_name:        Narrow to a specific project (optional)
            space_name:          Narrow to a specific space (optional)
            period_type:         today | yesterday | this_week | last_week |
                                 this_month | last_month | this_year | last_30_days |
                                 rolling | custom
            custom_start:        YYYY-MM-DD (required when period_type="custom")
            custom_end:          YYYY-MM-DD (required when period_type="custom")
            rolling_days:        Number of days for rolling window (1-365)
            min_overage_minutes: Minimum overage in minutes to flag a task (default 15)
            include_archived:    Include archived tasks (default True)
            async_job:           Run in background (recommended for spaces)

        Returns:
            {
              scope, period, total_overtime_tasks,
              members: {
                member_name: {
                  overtime_tasks, total_overtime,
                  tasks: [{task_name, estimated, tracked, overage, status}]
                }
              },
              summary_table: [{member, tasks, total_overtime}],
              formatted_output: <markdown>
            }
        """
        try:
            print(f"⌛ Overtime report / {period_type}")
            sys.stdout.flush()

            if not job_id:

                def _work():
                    return get_overtime_report(
                        project_name=project_name,
                        space_name=space_name,
                        period_type=period_type,
                        custom_start=custom_start,
                        custom_end=custom_end,
                        rolling_days=rolling_days,
                        min_overage_minutes=min_overage_minutes,
                        include_archived=include_archived,
                        async_job=False,
                        job_id="_bg_",
                    )

                return _dispatch(_work, async_job)

            start_date, end_date = parse_time_period_filter(
                period_type=period_type,
                custom_start=custom_start,
                custom_end=custom_end,
                rolling_days=rolling_days,
            )
            start_ms, end_ms = date_range_to_timestamps(start_date, end_date)
            min_overage_ms = min_overage_minutes * 60 * 1000

            # Resolve scope
            if project_name:
                list_ids = _list_ids_for_project(project_name)
                scope = project_name
            elif space_name:
                sid = _resolve_space_id(space_name)
                if not sid:
                    return {"error": f"Space '{space_name}' not found"}
                proj_map = _resolve_space_lists(sid)
                list_ids = [lid for lids in proj_map.values() for lid in lids]
                scope = space_name
            else:
                return {"error": "Provide either project_name or space_name"}

            if not list_ids:
                return {"error": f"No lists found for scope '{scope}'"}

            all_tasks = _fetch_all_tasks(
                list_ids, {}, include_archived=include_archived
            )

            if len(all_tasks) >= 300 and not job_id and not async_job:
                return get_overtime_report(
                    project_name=project_name,
                    space_name=space_name,
                    period_type=period_type,
                    custom_start=custom_start,
                    custom_end=custom_end,
                    rolling_days=rolling_days,
                    min_overage_minutes=min_overage_minutes,
                    include_archived=include_archived,
                    async_job=True,
                )

            timed = [t for t in all_tasks if int(t.get("time_spent") or 0) > 0]
            entries_map = _fetch_time_entries_smart(
                [t["id"] for t in timed], start_ms, end_ms
            )
            metrics = _calculate_task_metrics(all_tasks)

            members: Dict[str, Dict] = {}
            total_overtime_tasks = 0

            for task in all_tasks:
                task_id = task["id"]
                est_ms = metrics.get(task_id, {}).get("est_direct", 0)

                # Only tasks with an estimate
                if est_ms == 0:
                    continue

                user_time = filter_time_entries_by_user_and_date_range(
                    entries_map.get(task_id, []), start_ms, end_ms
                )
                total_tracked_ms = sum(user_time.values())

                if total_tracked_ms == 0:
                    continue

                overage_ms = total_tracked_ms - est_ms
                if overage_ms < min_overage_ms:
                    continue  # Within tolerance

                total_overtime_tasks += 1
                status = _extract_status_name(task)

                for username, t_ms in user_time.items():
                    per_user_overage = t_ms - (
                        est_ms // len(user_time) if user_time else est_ms
                    )
                    if per_user_overage < min_overage_ms:
                        continue
                    if username not in members:
                        members[username] = {
                            "overtime_tasks": 0,
                            "total_overtime_ms": 0,
                            "tasks": [],
                        }
                    mb = members[username]
                    mb["overtime_tasks"] += 1
                    mb["total_overtime_ms"] += per_user_overage
                    mb["tasks"].append(
                        {
                            "task_name": task.get("name", "Unnamed"),
                            "status": status,
                            "estimated": _format_duration(
                                est_ms // len(user_time) if user_time else est_ms
                            ),
                            "tracked": _format_duration(t_ms),
                            "overage": _format_duration(per_user_overage),
                            "overage_ms": per_user_overage,
                        }
                    )

            # Format output
            formatted_members = {}
            for mb, d in sorted(
                members.items(),
                key=lambda x: x[1]["total_overtime_ms"],
                reverse=True,
            ):
                d["tasks"].sort(key=lambda x: x.pop("overage_ms"), reverse=True)
                formatted_members[mb] = {
                    "overtime_tasks": d["overtime_tasks"],
                    "total_overtime": _format_duration(d["total_overtime_ms"]),
                    "tasks": d["tasks"],
                }

            summary_table = [
                {
                    "member": mb,
                    "overtime_tasks": d["overtime_tasks"],
                    "total_overtime": d["total_overtime"],
                }
                for mb, d in formatted_members.items()
            ]

            # --- Markdown formatted_output ---
            lines = [
                "## Overtime Report (Tracked > Estimated)",
                f"**Scope:** {scope}  |  **Period:** {start_date} → {end_date}",
                f"**Flagged tasks:** {total_overtime_tasks}  |  "
                f"**Min overage threshold:** {min_overage_minutes} min",
                "",
                "| Member | Overtime Tasks | Total Overtime |",
                "|--------|---------------:|---------------:|",
            ]
            for row in summary_table:
                lines.append(
                    f"| {row['member']} | {row['overtime_tasks']} | {row['total_overtime']} |"
                )
            lines.append("")

            for mb, d in formatted_members.items():
                lines.append(f"### {mb} — {d['total_overtime']} over")
                lines.append("| Task | Status | Estimated | Tracked | Overage |")
                lines.append("|------|--------|----------:|--------:|--------:|")
                for t in d["tasks"]:
                    lines.append(
                        f"| {t['task_name']} | {t['status']} | "
                        f"{t['estimated']} | {t['tracked']} | {t['overage']} |"
                    )
                lines.append("")

            return {
                "scope": scope,
                "period": f"{start_date} to {end_date}",
                "period_type": period_type,
                "total_overtime_tasks": total_overtime_tasks,
                "members": formatted_members,
                "summary_table": summary_table,
                "formatted_output": "\n".join(lines),
            }

        except Exception as e:
            import traceback

            print(traceback.format_exc())
            return {"error": str(e)}

    # =========================================================================
    # ASYNC STATUS/RESULT (shared pool — reuses JOBS dict defined above)
    # =========================================================================

    @mcp.tool()
    def get_task_report_job_status(job_id: str) -> dict:
        """
        OPTIONAL: Check raw status of a background task-report job.

        PREFERRED: Use get_task_report_job_result(job_id) instead — it waits
        internally for up to 50 seconds and returns the result automatically.

        Only use this tool if you need to confirm the job exists before
        committing to a longer wait.
        """
        j = _get_job(job_id)
        if not j:
            return {
                "error": "job_id not found — it may have been created by a different tool"
            }

        poll_count = j.get("_poll_count", 0) + 1
        j["_poll_count"] = poll_count
        _set_job(job_id, j)
        max_polls = 5
        status = j.get("status")

        if status in ("finished", "failed"):
            resp: Dict[str, Any] = {
                "job_id": job_id,
                "status": status,
                "error": j.get("error"),
                "poll_count": poll_count,
                "polls_remaining": 0,
            }
            if status == "finished":
                raw = j.get("result") or {}
                resp["result"] = raw
                # Hoist formatted_output to top level for direct model access
                if isinstance(raw, dict):
                    if raw.get("formatted_output"):
                        resp["formatted_output"] = raw["formatted_output"]
                        resp["DISPLAY_INSTRUCTION"] = (
                            "Copy the formatted_output field VERBATIM to the user. "
                            "Do NOT rewrite, summarise or invent any data."
                        )
                    for key in (
                        "scope",
                        "period",
                        "total_tasks_checked",
                        "total_missing_estimate",
                        "total_with_time_tracked",
                        "total_time_tracked_on_unestimated",
                    ):
                        if key in raw:
                            resp[key] = raw[key]
                resp["message"] = "Job complete! Use the formatted_output field above."
            return resp

        if poll_count >= max_polls:
            return {
                "job_id": job_id,
                "status": status,
                "poll_count": poll_count,
                "STOP_POLLING": True,
                "message": (
                    f"Max polls reached. Job still {status}. "
                    f"Use get_task_report_job_result(job_id='{job_id}') later."
                ),
            }

        return {
            "job_id": job_id,
            "status": status,
            "poll_count": poll_count,
            "polls_remaining": max_polls - poll_count,
            "message": f"Job still {status}. Wait 50-60s, then poll again.",
        }

    @mcp.tool()
    def get_task_report_job_result(job_id: str) -> dict:
        """
        PRIMARY tool to retrieve a background task-report result.

        Call this IMMEDIATELY after any tool returns a job_id or still_running.
        Waits internally up to 50 seconds for the job to finish, then returns
        the full result. If still running after 50s, returns a still_running
        response — call this tool again immediately in your next response.

        NEVER ask the user to wait. NEVER explain the status. Just call this
        tool repeatedly until status is 'finished', then display the result.
        """
        j = _get_job(job_id)
        if not j:
            return {"error": "job_id not found"}

        # If still running, wait up to 50s (under LM Studio ~60s timeout)
        if j.get("status") not in ("finished", "failed"):
            waited = 0.0
            while waited < 50.0:
                time.sleep(5.0)
                waited += 5.0
                j = _get_job(job_id)
                if not j:
                    return {"error": "job_id not found"}
                if j.get("status") in ("finished", "failed"):
                    break

        if j.get("status") == "finished":
            raw = j.get("result") or {}
            # Hoist formatted_output and key summary fields to the top level so
            # the model can find them without navigating nested JSON.
            resp: Dict[str, Any] = {
                "status": "finished",
                "result": raw,
            }
            if isinstance(raw, dict):
                if raw.get("formatted_output"):
                    resp["formatted_output"] = raw["formatted_output"]
                    resp["DISPLAY_INSTRUCTION"] = (
                        "Copy the formatted_output field VERBATIM to the user. "
                        "Do NOT rewrite, summarise or invent any data."
                    )
                for key in (
                    "scope",
                    "period",
                    "total_tasks_checked",
                    "total_missing_estimate",
                    "total_with_time_tracked",
                    "total_time_tracked_on_unestimated",
                ):
                    if key in raw:
                        resp[key] = raw[key]
            return resp
        if j.get("status") == "failed":
            return {"error": j.get("error", "Job failed")}
        return {
            "job_id": job_id,
            "status": "still_running",
            "INSTRUCTION": (
                "The report is STILL generating. "
                "Call get_task_report_job_result IMMEDIATELY in your next response: "
                f"get_task_report_job_result(job_id='{job_id}'). "
                "Do NOT tell the user to wait. Call the tool yourself right now."
            ),
        }


# ---------------------------------------------------------------------------
# Small helper used inside this module only
# ---------------------------------------------------------------------------


def _duration_to_ms(human: str) -> int:
    """Convert '2h 30m' back to milliseconds for sorting."""
    try:
        parts = human.replace("h", "").replace("m", "").split()
        hours = int(parts[0]) if len(parts) > 0 else 0
        mins = int(parts[1]) if len(parts) > 1 else 0
        return (hours * 60 + mins) * 60000
    except Exception:
        return 0
