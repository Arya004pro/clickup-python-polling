"""
Task Report Tools for ClickUp MCP Server
=========================================
PM-focused daily report tools covering the full reporting suite:

1. get_space_task_report       — Space-wise summary with per-project breakdown
2. get_project_task_report     — Project-wise with per-team-member breakdown
3. get_member_task_report      — Individual team member task report
4. get_low_hours_report        — Employees who tracked < 8 h on any day (with occurrence count)
5. get_missing_estimation_report — Employees whose tasks have no time estimate
6. get_overtracked_report       — Employees where tracked time exceeds estimated time

All period-based tools support:
  today | yesterday | this_week | last_week | this_month | last_month |
  this_year | last_30_days | rolling (+rolling_days) | custom (+custom_start/end YYYY-MM-DD)

OVERTIME FIX (2025-02-25):
  Parent tasks now use est_total / tracked_total (rollup-consistent).
  Subtasks continue to use est_direct / tracked_direct.
  Per-user overage is proportional share of total task overage,
  NOT raw (t_ms - split_estimate), which was inflating numbers when
  time entries included rolled-up subtask time.
"""

from __future__ import annotations

import sys
import time
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple
import json as _json
import os as _os

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
        # Prefer live folder-list fetch to avoid stale embedded folder payloads.
        lid_list: List[str] = []
        live_lists, _ = _api_call("GET", f"/folder/{folder['id']}/list")
        if live_lists and live_lists.get("lists"):
            lid_list = [lst["id"] for lst in live_lists.get("lists", [])]
        else:
            lid_list = [lst["id"] for lst in folder.get("lists", [])]
        if lid_list:
            projects[folder["name"]] = lid_list

    resp_l, _ = _api_call("GET", f"/space/{space_id}/list")
    folderless = [lst["id"] for lst in (resp_l or {}).get("lists", [])]
    if folderless:
        projects["Folderless Lists"] = folderless

    return projects


# ---------------------------------------------------------------------------
# Monitoring config — scoped list-ID resolver
# ---------------------------------------------------------------------------


_MONITORING_CONFIG_PATH = _os.path.join(
    _os.path.dirname(__file__), "..", "monitoring_config.json"
)


def _split_monitored_scope(raw_name: str):
    """
    Parse monitored scope expressions.

    Examples:
      "monitored" -> (True, "")
      "Monitored AIX" -> (True, "AIX")
      "monitored:AIX" -> (True, "AIX")
      "AIX" -> (False, "AIX")
    """
    name = (raw_name or "").strip()
    lower = name.lower()
    if lower == "monitored":
        return True, ""
    if lower.startswith("monitored:"):
        return True, name.split(":", 1)[1].strip()
    if lower.startswith("monitored "):
        return True, name[len("monitored ") :].strip()
    return False, name


def _load_monitored_list_ids(project_name: str) -> Optional[List[str]]:
    """
    Check monitoring_config.json for a matching alias or the special keyword
    "monitored" (which aggregates ALL monitored projects).

    Returns a list of list IDs if found, or None to fall through to normal
    ClickUp API resolution.
    """
    try:
        with open(_MONITORING_CONFIG_PATH, "r") as _f:
            _cfg = _json.load(_f)
    except FileNotFoundError:
        return None

    _projects = _cfg.get("monitored_projects", [])
    _name = project_name.strip()
    _name_lower = _name.lower()
    _is_monitored_scope, _monitored_target = _split_monitored_scope(_name)

    def _collect_project_list_ids(_p: Dict[str, Any]) -> List[str]:
        _ids: List[str] = [str(_id) for _id in (_p.get("list_ids") or []) if _id]
        if _p.get("clickup_id") and _p.get("type") == "folder":
            _resp, _ = _api_call("GET", f"/folder/{_p['clickup_id']}/list")
            _ids.extend(
                [str(_l["id"]) for _l in (_resp or {}).get("lists", []) if _l.get("id")]
            )
        return list(dict.fromkeys(_ids))

    # Special keyword: aggregate every monitored project
    if _name_lower == "monitored":
        _all_ids: List[str] = []
        for _p in _projects:
            _all_ids.extend(_collect_project_list_ids(_p))
        return list(dict.fromkeys(_all_ids))

    # "Monitored <SpaceName>" / "monitored:<SpaceName>"
    # Example: "Monitored AIX" -> union of monitored projects in space AIX.
    if _is_monitored_scope and _monitored_target:
        _target_lower = _monitored_target.lower()

        # Exact monitored alias match first.
        for _p in _projects:
            if _p.get("alias", "").strip().lower() == _target_lower:
                return _collect_project_list_ids(_p)

        # Otherwise resolve as monitored SPACE scope.
        _all_ids: List[str] = []
        for _p in _projects:
            if str(_p.get("space", "")).strip().lower() == _target_lower:
                _all_ids.extend(_collect_project_list_ids(_p))

        # Return [] (not None) so callers do NOT fall back to broad API scope.
        return list(dict.fromkeys(_all_ids))

    # Exact alias match
    for _p in _projects:
        if _p["alias"].strip().lower() == _name_lower:
            _ids = _collect_project_list_ids(_p)
            return _ids
    return None


def _list_ids_for_project(project_name: str) -> List[str]:
    """Resolve a project name (space/folder/list) → flat list of list IDs.

    Checks monitoring_config.json first; falls back to full ClickUp resolution.
    """
    _monitored = _load_monitored_list_ids(project_name)
    if _monitored is not None:
        return _monitored

    from .pm_analytics import _resolve_to_list_ids

    return _resolve_to_list_ids(project_name, None)


def _resolve_space_project_map_for_reports(
    space_name: str, auto_monitored: bool = True
) -> Tuple[Optional[Dict[str, List[str]]], str, bool, Optional[str]]:
    """
    Resolve a space name into project->list mapping for reports.

    Supports explicit monitored scopes like "Monitored AIX" and can
    auto-apply monitored filtering when the space is present in
    monitoring_config.json.
    """
    requested_space_name = (space_name or "").strip()
    monitored_scope, monitored_target = _split_monitored_scope(requested_space_name)
    resolved_space_name = requested_space_name

    if monitored_scope:
        if not monitored_target:
            return (
                None,
                requested_space_name,
                False,
                "For monitored scope, provide a space name like 'Monitored AIX'.",
            )
        resolved_space_name = monitored_target

    sid = _resolve_space_id(resolved_space_name)
    if not sid:
        return (
            None,
            resolved_space_name,
            False,
            f"Space '{resolved_space_name}' not found",
        )

    project_map = _resolve_space_lists(sid)
    if not project_map:
        return (
            None,
            resolved_space_name,
            False,
            f"No lists found in space '{resolved_space_name}'",
        )

    monitored_ids = set(
        _load_monitored_list_ids(f"monitored {resolved_space_name}") or []
    )
    monitored_applied = monitored_scope or (auto_monitored and bool(monitored_ids))

    if monitored_applied:
        if not monitored_ids:
            return (
                None,
                resolved_space_name,
                True,
                f"No monitored list IDs configured for space '{resolved_space_name}'.",
            )

        filtered_map: Dict[str, List[str]] = {}
        for pname, lids in project_map.items():
            scoped_lids = [lid for lid in lids if lid in monitored_ids]
            if scoped_lids:
                filtered_map[pname] = scoped_lids
        project_map = filtered_map

        if not project_map:
            return (
                None,
                resolved_space_name,
                True,
                f"No monitored projects found in resolved space '{resolved_space_name}'.",
            )

    return project_map, resolved_space_name, monitored_applied, None


def _resolve_space_list_ids_for_reports(
    space_name: str, auto_monitored: bool = True
) -> Tuple[List[str], str, bool, Optional[str]]:
    """
    Resolve a space name into de-duplicated list IDs with monitoring safeguards.
    """
    project_map, resolved_space_name, monitored_applied, scope_error = (
        _resolve_space_project_map_for_reports(
            space_name=space_name, auto_monitored=auto_monitored
        )
    )
    if scope_error:
        return [], resolved_space_name, monitored_applied, scope_error

    all_list_ids = [lid for lids in (project_map or {}).values() for lid in lids]
    deduped = list(dict.fromkeys(str(lid) for lid in all_list_ids if lid))
    return deduped, resolved_space_name, monitored_applied, None


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


def _build_space_ai_summary(
    display_space_name: str,
    start_date: str,
    end_date: str,
    total_projects: int,
    active_projects: int,
    grand_tracked_ms: int,
    grand_est_ms: int,
    project_highlights: List[Dict[str, Any]],
    member_rollup: Dict[str, Dict[str, int]],
) -> str:
    if active_projects == 0:
        return (
            f"No tracked work was recorded in {display_space_name} from {start_date} to {end_date}. "
            "This likely indicates either no activity or delayed time logging, so project health cannot be inferred yet."
        )

    utilization_pct = (
        int(round((grand_tracked_ms / grand_est_ms) * 100))
        if grand_est_ms > 0
        else None
    )

    top_project = max(
        project_highlights,
        key=lambda item: item.get("tracked_ms", 0),
        default=None,
    )
    top_member = max(
        member_rollup.items(),
        key=lambda item: item[1].get("tracked_ms", 0),
        default=None,
    )

    overloaded_projects = [
        p
        for p in project_highlights
        if p.get("est_ms", 0) > 0 and p.get("tracked_ms", 0) > p.get("est_ms", 0)
    ]
    no_estimate_projects = [p for p in project_highlights if p.get("est_ms", 0) == 0]
    idle_projects = max(total_projects - active_projects, 0)

    lines: List[str] = []
    if utilization_pct is None:
        lines.append(
            f"From {start_date} to {end_date}, {display_space_name} logged {_format_duration(grand_tracked_ms)} across {active_projects} active project(s), but estimates are missing for the active scope."
        )
    else:
        lines.append(
            f"From {start_date} to {end_date}, {display_space_name} logged {_format_duration(grand_tracked_ms)} against {_format_duration(grand_est_ms)} ({utilization_pct}% of estimate) across {active_projects} active project(s)."
        )

    if top_project:
        lines.append(
            f"Work is most concentrated in {top_project['name']} with {_format_duration(top_project['tracked_ms'])} across {top_project['tasks']} task(s)."
        )

    if top_member:
        member_name, member_data = top_member
        lines.append(
            f"Top contributor is {member_name} with {_format_duration(member_data['tracked_ms'])} across {member_data['tasks']} task touch(es)."
        )

    risk_parts: List[str] = []
    if overloaded_projects:
        risk_parts.append(
            f"{len(overloaded_projects)} project(s) are over tracked versus estimate"
        )
    if no_estimate_projects:
        risk_parts.append(
            f"{len(no_estimate_projects)} active project(s) have no time estimate"
        )
    if idle_projects:
        risk_parts.append(f"{idle_projects} project(s) had no logged activity")

    if risk_parts:
        lines.append("Attention areas: " + "; ".join(risk_parts) + ".")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registration function
# ---------------------------------------------------------------------------


def register_task_report_tools(mcp: FastMCP):
    """Register all task-report tools on the provided FastMCP instance."""

    JOBS: Dict[str, Dict] = {}

    # -------------------------------------------------------------------------
    # Unified dispatcher — used by every report tool.
    # -------------------------------------------------------------------------
    def _dispatch(
        fn, async_job: bool = False, poll_interval: float = 5.0, max_wait: float = 360.0
    ) -> dict:
        """
        Run fn() in a background thread.

        async_job=True  (default for all public report tools):
            Starts the job and returns a job_id dict IMMEDIATELY so the MCP
            tool handler finishes within the transport timeout window.
            The LLM must call get_task_report_job_result(job_id=...) once
            the job is complete (typically 60-120 s for large spaces).

        async_job=False (used internally by _bg_ sub-calls):
            Blocks the calling thread, sleep-polls every poll_interval seconds,
            and returns the full result directly once done.
            Falls back to a human-readable message if max_wait seconds elapse.
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

        # --- async_job=True: return immediately so MCP transport doesn't timeout ---
        if async_job:
            return {
                "job_id": jid,
                "status": "queued",
                "message": (
                    f"Job started in background. "
                    f"Call get_task_report_job_result(job_id='{jid}') after 60-90 s "
                    "to retrieve the full result. "
                    "Use get_task_report_job_status(job_id=...) to check progress."
                ),
            }

        # --- async_job=False: block and return result inline ---
        elapsed = 0.0
        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval
            status = JOBS[jid]["status"]
            if status == "finished":
                return JOBS[jid]["result"]
            if status == "failed":
                return {"error": JOBS[jid].get("error", "Unknown error in report job")}

        return {
            "error": (
                "Report did not complete within the time limit. "
                f"Use get_task_report_job_result(job_id='{jid}') to retrieve it when ready."
            ),
            "job_id": jid,
        }

    # =========================================================================
    # 1. SPACE-WISE TASK REPORT
    # =========================================================================

    @mcp.tool()
    def get_space_task_report(
        space_name: str,
        period_type: str = "today",
        custom_start: Optional[str] = None,
        custom_end: Optional[str] = None,
        rolling_days: Optional[int] = None,
        include_archived: bool = True,
        async_job: bool = True,
        job_id: Optional[str] = None,
    ) -> dict:
        """
        Space-Wise Task Report.

        For every project (folder/list) inside the space, shows who worked on
        it, how many tasks they touched, and time tracked vs. estimated — all
        in the selected period.

        Args:
            space_name:     Name of the ClickUp space
            period_type:    today | yesterday | this_week | last_week |
                            this_month | last_month | this_year | last_30_days |
                            rolling | custom
            custom_start:   YYYY-MM-DD (required when period_type="custom")
            custom_end:     YYYY-MM-DD (required when period_type="custom")
            rolling_days:   Number of days for rolling window (1-365)
            include_archived: Include archived tasks (default True)

        Returns:
            {
              space_name, period,
              grand_total_time_tracked, grand_total_time_estimate,
              projects: [
                {
                  project_name, project_type,
                  tasks_worked_on, time_tracked, time_estimate,
                  team_breakdown: {
                    member_name: {tasks, time_tracked, time_estimate}
                  }
                }
              ],
              formatted_output: <ready-to-display markdown>
            }
        """
        try:
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
            print(f"⌛ Space task report: '{space_name}' / {period_type}")
            sys.stdout.flush()

            requested_space_name = space_name.strip()
            monitored_scope, _ = _split_monitored_scope(requested_space_name)

            start_date, end_date = parse_time_period_filter(
                period_type=period_type,
                custom_start=custom_start,
                custom_end=custom_end,
                rolling_days=rolling_days,
            )
            start_ms, end_ms = date_range_to_timestamps(start_date, end_date)

            project_map, resolved_space_name, monitored_applied, scope_error = (
                _resolve_space_project_map_for_reports(
                    requested_space_name, auto_monitored=True
                )
            )
            if scope_error:
                return {"error": scope_error}

            display_space_name = requested_space_name
            if monitored_applied and not monitored_scope:
                display_space_name = f"Monitored {resolved_space_name}"

            list_to_project = {
                lid: pname
                for pname, lids in (project_map or {}).items()
                for lid in lids
            }
            all_list_ids = list(list_to_project.keys())

            all_tasks = _fetch_all_tasks(
                all_list_ids, {}, include_archived=include_archived
            )

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
                    "status_counts": {
                        "not_started": 0,
                        "active": 0,
                        "done": 0,
                        "cancelled": 0,
                    },
                    "team_members": defaultdict(
                        lambda: {
                            "tasks": 0,
                            "time_tracked_ms": 0,
                            "time_estimate_ms": 0,
                            "task_list": [],
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

                task_name = task.get("name", "Unnamed")
                task_status = _extract_status_name(task)
                status_obj = task.get("status")
                status_type = (
                    status_obj.get("type") if isinstance(status_obj, dict) else None
                )
                status_category = get_status_category(task_status, status_type)
                if status_category == "not_started":
                    pr["status_counts"]["not_started"] += 1
                elif status_category == "done":
                    pr["status_counts"]["done"] += 1
                elif status_category == "closed":
                    pr["status_counts"]["cancelled"] += 1
                else:
                    pr["status_counts"]["active"] += 1

                for username, t_ms in user_time.items():
                    mb = pr["team_members"][username]
                    mb["tasks"] += 1
                    mb["time_tracked_ms"] += t_ms
                    member_est = est // len(user_time) if user_time else 0
                    mb["time_estimate_ms"] += member_est
                    mb["task_list"].append(
                        {
                            "task_name": task_name,
                            "status": task_status,
                            "time_tracked_ms": t_ms,
                            "time_tracked": _format_duration(t_ms),
                            "time_estimate": _format_duration(member_est),
                        }
                    )

            # Format output
            formatted_projects = []
            grand_tracked = 0
            grand_est = 0
            project_highlights: List[Dict[str, Any]] = []
            member_rollup: Dict[str, Dict[str, int]] = defaultdict(
                lambda: {"tracked_ms": 0, "tasks": 0, "estimate_ms": 0}
            )

            for pname, pr in project_reports.items():
                if pr["tasks_worked_on"] == 0:
                    continue
                grand_tracked += pr["time_tracked_ms"]
                grand_est += pr["time_estimate_ms"]
                project_highlights.append(
                    {
                        "name": pname,
                        "tracked_ms": pr["time_tracked_ms"],
                        "est_ms": pr["time_estimate_ms"],
                        "tasks": pr["tasks_worked_on"],
                    }
                )

                team_breakdown = {}
                for mb, d in pr["team_members"].items():
                    if d["time_tracked_ms"] == 0:
                        continue
                    member_rollup[mb]["tracked_ms"] += d["time_tracked_ms"]
                    member_rollup[mb]["tasks"] += d["tasks"]
                    member_rollup[mb]["estimate_ms"] += d["time_estimate_ms"]
                    sorted_tasks = sorted(
                        d["task_list"], key=lambda x: x["time_tracked_ms"], reverse=True
                    )
                    # Strip internal ms key from public output
                    public_tasks = [
                        {
                            "task_name": t["task_name"],
                            "status": t["status"],
                            "time_tracked": t["time_tracked"],
                            "time_estimate": t["time_estimate"],
                        }
                        for t in sorted_tasks
                    ]
                    team_breakdown[mb] = {
                        "tasks": d["tasks"],
                        "time_tracked": _format_duration(d["time_tracked_ms"]),
                        "time_estimate": _format_duration(d["time_estimate_ms"]),
                        "task_list": public_tasks,
                    }

                formatted_projects.append(
                    {
                        "project_name": pname,
                        "project_type": pr["project_type"],
                        "tasks_worked_on": pr["tasks_worked_on"],
                        "time_tracked": _format_duration(pr["time_tracked_ms"]),
                        "time_estimate": _format_duration(pr["time_estimate_ms"]),
                        "status_summary": pr["status_counts"],
                        "team_breakdown": team_breakdown,
                    }
                )

            formatted_projects.sort(
                key=lambda x: _duration_to_ms(x["time_tracked"]), reverse=True
            )

            show_cancelled_column = any(
                int(fp.get("status_summary", {}).get("cancelled", 0)) > 0
                for fp in formatted_projects
            )

            if not show_cancelled_column:
                for fp in formatted_projects:
                    status_summary = fp.get("status_summary") or {}
                    status_summary.pop("cancelled", None)

            employee_summary_table = [
                {
                    "member_name": member_name,
                    "tasks": data.get("tasks", 0),
                    "time_tracked": _format_duration(data.get("tracked_ms", 0)),
                    "time_estimate": _format_duration(data.get("estimate_ms", 0)),
                }
                for member_name, data in sorted(
                    member_rollup.items(),
                    key=lambda item: item[1].get("tracked_ms", 0),
                    reverse=True,
                )
            ]

            ai_summary = _build_space_ai_summary(
                display_space_name=display_space_name,
                start_date=start_date,
                end_date=end_date,
                total_projects=len(project_map or {}),
                active_projects=len(formatted_projects),
                grand_tracked_ms=grand_tracked,
                grand_est_ms=grand_est,
                project_highlights=project_highlights,
                member_rollup=member_rollup,
            )

            # --- Markdown formatted_output ---
            lines = [
                f"## Space Report: {display_space_name}",
                f"**Period:** {start_date} → {end_date}",
                f"**Total Tracked:** {_format_duration(grand_tracked)}  |  "
                f"**Total Estimated:** {_format_duration(grand_est)}",
                "",
                "### AI Summary",
            ]
            lines.extend(
                [f"- {line}" for line in ai_summary.split("\n") if line.strip()]
            )
            lines.append("")
            lines.append("### Status Summary by Project")
            if show_cancelled_column:
                lines.append("| Project | Not Started | Active | Done | Cancelled |")
                lines.append("|---------|------------:|-------:|-----:|----------:|")
            else:
                lines.append("| Project | Not Started | Active | Done |")
                lines.append("|---------|------------:|-------:|-----:|")
            for fp in formatted_projects:
                sc = fp.get("status_summary", {})
                if show_cancelled_column:
                    lines.append(
                        f"| {fp['project_name']} | {sc.get('not_started', 0)} | {sc.get('active', 0)} | {sc.get('done', 0)} | {sc.get('cancelled', 0)} |"
                    )
                else:
                    lines.append(
                        f"| {fp['project_name']} | {sc.get('not_started', 0)} | {sc.get('active', 0)} | {sc.get('done', 0)} |"
                    )
            lines.append("")

            lines.append("### Employee Summary")
            lines.append("| Member | Tasks | Time Tracked | Time Estimate |")
            lines.append("|--------|------:|-------------:|--------------:|")
            for row in employee_summary_table:
                lines.append(
                    f"| {row['member_name']} | {row['tasks']} | {row['time_tracked']} | {row['time_estimate']} |"
                )
            lines.append("")

            for fp in formatted_projects:
                lines.append(f"### {fp['project_name']} ({fp['project_type']})")
                lines.append(
                    f"Tasks worked on: **{fp['tasks_worked_on']}**  |  "
                    f"Tracked: **{fp['time_tracked']}**  |  "
                    f"Estimated: **{fp['time_estimate']}**"
                )
                if fp["team_breakdown"]:
                    for mb, d in sorted(fp["team_breakdown"].items()):
                        lines.append("")
                        lines.append(
                            f"**{mb}** — {d['tasks']} task(s)  |  "
                            f"Tracked: {d['time_tracked']}  |  "
                            f"Estimated: {d['time_estimate']}"
                        )
                        lines.append("")
                        lines.append("| Task | Status | Tracked | Estimated |")
                        lines.append("|------|--------|--------:|----------:|")
                        for t in d["task_list"]:
                            lines.append(
                                f"| {t['task_name']} | {t['status']} "
                                f"| {t['time_tracked']} | {t['time_estimate']} |"
                            )
                    lines.append("")

            return {
                "space_name": display_space_name,
                "period": f"{start_date} to {end_date}",
                "period_type": period_type,
                "grand_total_time_tracked": _format_duration(grand_tracked),
                "grand_total_time_estimate": _format_duration(grand_est),
                "total_projects": len(project_map or {}),
                "active_projects": len(formatted_projects),
                "projects": formatted_projects,
                "employee_summary_table": employee_summary_table,
                "status_summary_table": [
                    (
                        {
                            "project_name": fp["project_name"],
                            "not_started": fp.get("status_summary", {}).get(
                                "not_started", 0
                            ),
                            "active": fp.get("status_summary", {}).get("active", 0),
                            "done": fp.get("status_summary", {}).get("done", 0),
                            "cancelled": fp.get("status_summary", {}).get(
                                "cancelled", 0
                            ),
                        }
                        if show_cancelled_column
                        else {
                            "project_name": fp["project_name"],
                            "not_started": fp.get("status_summary", {}).get(
                                "not_started", 0
                            ),
                            "active": fp.get("status_summary", {}).get("active", 0),
                            "done": fp.get("status_summary", {}).get("done", 0),
                        }
                    )
                    for fp in formatted_projects
                ],
                "ai_summary": ai_summary,
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
                list_ids, resolved_space_name, monitored_applied, scope_error = (
                    _resolve_space_list_ids_for_reports(space_name, auto_monitored=True)
                )
                if scope_error:
                    return {"error": scope_error}
                scope = (
                    f"Monitored {resolved_space_name}"
                    if monitored_applied and not _split_monitored_scope(space_name)[0]
                    else space_name
                )
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
                  total_days_below_threshold,
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
            period_norm = (period_type or "").strip().lower()
            exclude_today_for_ongoing = period_norm in {
                "this_week",
                "current_week",
                "this_month",
                "current_month",
                "this_year",
                "current_year",
                "last_30_days",
                "rolling",
            }
            today_ist = (datetime.now(timezone.utc) + timedelta(hours=5.5)).strftime(
                "%Y-%m-%d"
            )
            effective_end_date = end_date

            # Resolve list IDs for scope
            if project_name:
                list_ids = _list_ids_for_project(project_name)
                scope = project_name
            elif space_name:
                list_ids, resolved_space_name, monitored_applied, scope_error = (
                    _resolve_space_list_ids_for_reports(space_name, auto_monitored=True)
                )
                if scope_error:
                    return {"error": scope_error}
                scope = (
                    f"Monitored {resolved_space_name}"
                    if monitored_applied and not _split_monitored_scope(space_name)[0]
                    else space_name
                )
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

            # Build per-member, per-day time map from ACTUAL entry user.
            # Works for unassigned tasks and multi-employee tasks naturally.
            member_day: Dict[str, Dict[str, int]] = defaultdict(
                lambda: defaultdict(int)
            )
            seen_interval_by_user: Dict[str, set] = defaultdict(set)

            for task_entries in entries_map.values():
                for entry in task_entries:
                    user_obj = entry.get("user") or {}
                    uname = user_obj.get("username") or user_obj.get("email") or ""
                    if not uname:
                        continue

                    intervals = entry.get("intervals", []) or []
                    # Fallback shape: interval data may be on the entry itself.
                    if not intervals:
                        e_start = entry.get("start")
                        e_time = entry.get("duration") or entry.get("time") or 0
                        if e_start:
                            intervals = [
                                {
                                    "start": e_start,
                                    "end": entry.get("end"),
                                    "time": e_time,
                                }
                            ]

                    for iv in intervals:
                        iv_start = int(iv.get("start") or 0)
                        if not (start_ms <= iv_start <= end_ms):
                            continue

                        iv_end = int(iv.get("end") or 0)
                        iv_time = int(iv.get("time") or 0)
                        if iv_time <= 0:
                            iv_time = iv_end - iv_start if iv_end > iv_start else 0
                        if iv_time <= 0:
                            continue

                        # Deduplicate by interval id per user across all tasks to avoid rollup double-counting.
                        iv_id = iv.get("id")
                        fp = (
                            f"id:{iv_id}"
                            if iv_id
                            else f"tuple:{iv_start}:{iv_end}:{iv_time}"
                        )
                        if fp in seen_interval_by_user[uname]:
                            continue
                        seen_interval_by_user[uname].add(fp)

                        date_str = _ms_to_date_ist(iv_start)
                        member_day[uname][date_str] += iv_time

            # Classify days
            flagged = []
            clean = []

            for uname, day_map in sorted(member_day.items()):
                short_days = []
                for date_str, tracked_ms in sorted(day_map.items()):
                    # Ongoing periods should evaluate complete days only.
                    # Excluding current IST date avoids false low-hour flags mid-day.
                    if exclude_today_for_ongoing and date_str == today_ist:
                        continue
                    if tracked_ms < threshold_ms:
                        shortfall_ms = threshold_ms - tracked_ms
                        dt = datetime.strptime(date_str, "%Y-%m-%d")
                        short_days.append(
                            {
                                "date": date_str,
                                "day_of_week": dt.strftime("%A"),
                                "tracked": _format_duration(tracked_ms),
                                "shortfall": _format_duration(shortfall_ms),
                                "tracked_ms": tracked_ms,
                            }
                        )

                if short_days:
                    short_days.sort(key=lambda x: x["date"])
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
                "## Low Hours Report",
                f"**Period:** {start_date} → {effective_end_date}  |  "
                f"**Threshold:** {min_hours}h/day  |  "
                f"**Scope:** {scope}",
                f"**{len(flagged)} member(s) with short days** | "
                f"**{len(clean)} member(s) fully compliant**",
                "",
            ]
            if exclude_today_for_ongoing:
                lines.append(
                    f"_Note: Current day ({today_ist}) excluded to avoid partial-day false positives._"
                )
                lines.append("")
            if flagged:
                lines.append("| Member | Days Below Target | Lowest Hours Day |")
                lines.append("|--------|------------------:|------------------|")
                for f in flagged:
                    worst = min(f["short_days"], key=lambda x: x["tracked_ms"])
                    lines.append(
                        f"| {f['member_name']} | {f['total_days_below_threshold']} "
                        f"| {worst['date']} ({worst['tracked']}) |"
                    )
                lines.append("")
                for f in flagged:
                    lines.append(
                        f"### {f['member_name']} — {f['total_days_below_threshold']} day(s) below target"
                    )
                    lines.append(
                        "| Date | Day | Hours Logged | Hours Missing To Reach Target |"
                    )
                    lines.append(
                        "|------|-----|-------------:|------------------------------:|"
                    )
                    for sd in f["short_days"]:
                        lines.append(
                            f"| {sd['date']} | {sd['day_of_week']} | "
                            f"{sd['tracked']} | {sd['shortfall']} |"
                        )
                    lines.append("")

            if clean:
                lines.append(f"**Compliant members:** {', '.join(clean)}")

            return {
                "period": f"{start_date} to {effective_end_date}",
                "period_type": period_type,
                "scope": scope,
                "threshold_hours": min_hours,
                "excluded_current_day": exclude_today_for_ongoing,
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

        Lists all tasks that have no time estimate set, grouped by the person
        who tracked time on them.

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
                  missing_count,
                  tasks: [{task_name, status, list_name, task_id}]
                }
              },
              unassigned_tasks: [{task_name, status, list_name}],
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
                list_ids, resolved_space_name, monitored_applied, scope_error = (
                    _resolve_space_list_ids_for_reports(space_name, auto_monitored=True)
                )
                if scope_error:
                    return {"error": scope_error}
                scope = (
                    f"Monitored {resolved_space_name}"
                    if monitored_applied and not _split_monitored_scope(space_name)[0]
                    else space_name
                )
            else:
                return {"error": "Provide either project_name or space_name"}

            if not list_ids:
                return {"error": f"No lists found for scope '{scope}'"}

            all_tasks = _fetch_all_tasks(
                list_ids, {}, include_archived=include_archived
            )

            # Build a quick id→task lookup
            task_map: Dict[str, dict] = {t["id"]: t for t in all_tasks}

            # Compute metrics per task (strips rolled-up subtask estimates
            # that ClickUp bakes into the parent's raw time_estimate API field).
            metrics = _calculate_task_metrics(all_tasks)

            # Thresholds for ratio-based flagging (TT / TE)
            RATIO_LOW = 0.25  # tracked < 25% of estimate → inflated / ghost estimate
            RATIO_HIGH = 2.0  # tracked > 2× estimate     → significant overtime

            members: Dict[str, Dict] = {}
            unassigned: list = []
            total_missing = 0

            # Ratio-outlier buckets (tasks that DO have an estimate but TT/TE
            # falls outside [RATIO_LOW, RATIO_HIGH])
            ratio_members: Dict[str, Dict] = {}
            ratio_unassigned: list = []
            ratio_total = 0

            # ----------------------------------------------------------------
            # MODE A — period filter provided:
            #   Fetch time entries logged in the period → find tasks worked on
            #   in the period that have no estimate → group by tracker
            # ----------------------------------------------------------------
            if start_ms is not None:
                candidate_ids = [
                    t["id"] for t in all_tasks if int(t.get("time_spent") or 0) > 0
                ]
                entries_map = _fetch_time_entries_smart(candidate_ids, start_ms, end_ms)

                # task_id → set of usernames who tracked in this period
                task_trackers: Dict[str, set] = {}
                for tid, entries in entries_map.items():
                    for entry in entries:
                        uname = (entry.get("user") or {}).get("username", "")

                        intervals = entry.get("intervals") or []
                        if intervals:
                            in_range = any(
                                start_ms <= int(iv.get("start") or 0) <= end_ms
                                for iv in intervals
                            )
                        else:
                            entry_start = int(entry.get("start") or 0)
                            in_range = (entry_start == 0) or (
                                start_ms <= entry_start <= end_ms
                            )

                        if not in_range:
                            continue

                        if uname:
                            task_trackers.setdefault(tid, set()).add(uname)
                        else:
                            task = task_map.get(tid)
                            if task:
                                for au in task.get("assignees", []):
                                    au_name = au.get("username", "")
                                    if au_name:
                                        task_trackers.setdefault(tid, set()).add(
                                            au_name
                                        )

                seen_task_ids: set = set()
                seen_ratio_ids: set = set()
                for tid, trackers in task_trackers.items():
                    task = task_map.get(tid)
                    if not task:
                        continue

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

                    # Main tasks compare against aggregate totals;
                    # subtasks use their own direct metrics.
                    is_subtask = bool(task.get("parent"))
                    m = metrics.get(tid, {})
                    metric_key = "direct" if is_subtask else "total"
                    est_ms = m.get(f"est_{metric_key}", 0)
                    tracked_ms = m.get(f"tracked_{metric_key}", 0)

                    if est_ms > 0:
                        if tracked_ms > 0 and tid not in seen_ratio_ids:
                            seen_ratio_ids.add(tid)
                            ratio = tracked_ms / est_ms
                            if ratio < RATIO_LOW or ratio > RATIO_HIGH:
                                ratio_total += 1
                                ratio_entry = {
                                    "task_name": task.get("name", "Unnamed"),
                                    "status": _extract_status_name(task),
                                    "list_name": task.get("list", {}).get(
                                        "name", "Unknown"
                                    ),
                                    "task_id": tid,
                                    "tracked": _format_duration(tracked_ms),
                                    "estimated": _format_duration(est_ms),
                                    "ratio": round(ratio, 2),
                                    "flag": "over" if ratio > RATIO_HIGH else "under",
                                }
                                if not trackers:
                                    ratio_unassigned.append(ratio_entry)
                                else:
                                    for uname in trackers:
                                        if uname not in ratio_members:
                                            ratio_members[uname] = {
                                                "count": 0,
                                                "tasks": [],
                                            }
                                        ratio_members[uname]["count"] += 1
                                        ratio_members[uname]["tasks"].append(
                                            ratio_entry
                                        )
                        continue  # has estimate → not in missing-estimate list

                    if tid in seen_task_ids:
                        continue
                    seen_task_ids.add(tid)

                    total_missing += 1
                    task_entry = {
                        "task_name": task.get("name", "Unnamed"),
                        "status": _extract_status_name(task),
                        "list_name": task.get("list", {}).get("name", "Unknown"),
                        "task_id": tid,
                    }

                    if not trackers:
                        unassigned.append(task_entry)
                    else:
                        for uname in trackers:
                            if uname not in members:
                                members[uname] = {"missing_count": 0, "tasks": []}
                            members[uname]["missing_count"] += 1
                            members[uname]["tasks"].append(task_entry)

                checked_count = len(task_trackers)

            # ----------------------------------------------------------------
            # MODE B — no period filter: scan every task, group by assignee
            # ----------------------------------------------------------------
            else:
                for task in all_tasks:
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

                    task_id_b = task["id"]
                    is_subtask = bool(task.get("parent"))
                    m = metrics.get(task_id_b, {})
                    metric_key = "direct" if is_subtask else "total"
                    est_ms = m.get(f"est_{metric_key}", 0)
                    tracked_ms = m.get(f"tracked_{metric_key}", 0)

                    assignees = task.get("assignees", [])
                    assignee_names = [u.get("username", "Unknown") for u in assignees]

                    if est_ms > 0:
                        if tracked_ms > 0:
                            ratio = tracked_ms / est_ms
                            if ratio < RATIO_LOW or ratio > RATIO_HIGH:
                                ratio_total += 1
                                ratio_entry = {
                                    "task_name": task.get("name", "Unnamed"),
                                    "status": _extract_status_name(task),
                                    "list_name": task.get("list", {}).get(
                                        "name", "Unknown"
                                    ),
                                    "task_id": task_id_b,
                                    "tracked": _format_duration(tracked_ms),
                                    "estimated": _format_duration(est_ms),
                                    "ratio": round(ratio, 2),
                                    "flag": "over" if ratio > RATIO_HIGH else "under",
                                }
                                if not assignee_names:
                                    ratio_unassigned.append(ratio_entry)
                                else:
                                    for uname in assignee_names:
                                        if uname not in ratio_members:
                                            ratio_members[uname] = {
                                                "count": 0,
                                                "tasks": [],
                                            }
                                        ratio_members[uname]["count"] += 1
                                        ratio_members[uname]["tasks"].append(
                                            ratio_entry
                                        )
                        continue  # has estimate → not in missing-estimate list

                    total_missing += 1
                    task_entry = {
                        "task_name": task.get("name", "Unnamed"),
                        "status": _extract_status_name(task),
                        "list_name": task.get("list", {}).get("name", "Unknown"),
                        "task_id": task_id_b,
                    }

                    if not assignee_names:
                        unassigned.append(task_entry)
                    else:
                        for uname in assignee_names:
                            if uname not in members:
                                members[uname] = {"missing_count": 0, "tasks": []}
                            members[uname]["missing_count"] += 1
                            members[uname]["tasks"].append(task_entry)

                checked_count = len(all_tasks)

            # Sort members by missing_count desc
            sorted_members = dict(
                sorted(
                    members.items(), key=lambda x: x[1]["missing_count"], reverse=True
                )
            )
            sorted_ratio_members = dict(
                sorted(ratio_members.items(), key=lambda x: x[1]["count"], reverse=True)
            )

            # --- Markdown formatted_output ---
            lines = [
                "## Missing Estimation Report",
                f"**Scope:** {scope}  |  **Period:** {period_label}  |  "
                f"**Tasks without estimate:** {total_missing}  |  "
                f"**Ratio-flagged tasks:** {ratio_total}  |  "
                f"**Checked:** {checked_count}",
                "",
                "### Missing Time Estimate  _(TE = 0, all assignees)_",
                "",
                "| Member | Tasks Without Estimate |",
                "|--------|----------------------:|",
            ]
            for mb, d in sorted_members.items():
                lines.append(f"| {mb} | {d['missing_count']} |")
            if unassigned:
                lines.append(f"| *(Unassigned)* | {len(unassigned)} |")
            lines.append(f"| **Total** | **{total_missing}** |")

            # --- Ratio-outlier table ---
            if ratio_total > 0:
                lines += [
                    "",
                    f"### Suspicious TT/TE Ratio  "
                    f"_(outside {RATIO_LOW}\u00d7\u2013{RATIO_HIGH}\u00d7 band)_",
                    "",
                    "| Member | Tasks | Over-Budget (>2\u00d7) | Under-Budget (<0.25\u00d7) |",
                    "|--------|------:|-------------------:|---------------------:|",
                ]
                grand_over = grand_under = 0
                for mb, d in sorted_ratio_members.items():
                    over_count = sum(1 for t in d["tasks"] if t.get("flag") == "over")
                    under_count = sum(1 for t in d["tasks"] if t.get("flag") == "under")
                    grand_over += over_count
                    grand_under += under_count
                    lines.append(
                        f"| {mb} | {d['count']} | {over_count} | {under_count} |"
                    )
                if ratio_unassigned:
                    ru_over = sum(
                        1 for t in ratio_unassigned if t.get("flag") == "over"
                    )
                    ru_under = sum(
                        1 for t in ratio_unassigned if t.get("flag") == "under"
                    )
                    grand_over += ru_over
                    grand_under += ru_under
                    lines.append(
                        f"| *(Unassigned)* | {len(ratio_unassigned)} | {ru_over} | {ru_under} |"
                    )
                lines.append(
                    f"| **Total** | **{ratio_total}** | **{grand_over}** | **{grand_under}** |"
                )

            return {
                "scope": scope,
                "period": period_label,
                "total_tasks_checked": checked_count,
                "total_missing_estimate": total_missing,
                "total_ratio_flagged": ratio_total,
                "ratio_band": {"low": RATIO_LOW, "high": RATIO_HIGH},
                "members": {
                    mb: {"missing_count": d["missing_count"]}
                    for mb, d in sorted_members.items()
                },
                "unassigned_count": len(unassigned),
                "ratio_members": {
                    mb: {
                        "count": d["count"],
                        "over": sum(1 for t in d["tasks"] if t.get("flag") == "over"),
                        "under": sum(1 for t in d["tasks"] if t.get("flag") == "under"),
                    }
                    for mb, d in sorted_ratio_members.items()
                },
                "ratio_unassigned_count": len(ratio_unassigned),
                "formatted_output": "\n".join(lines),
            }

        except Exception as e:
            import traceback

            print(traceback.format_exc())
            return {"error": str(e)}

    # =========================================================================
    # 6. OVERTIME REPORT — Tracked > Estimated
    #
    # FIX (2025-02-26): Use hierarchy-level metric pair:
    #   • Main task (no parent): est_total vs tracked_total
    #   • Subtask (any depth):   est_direct vs tracked_direct
    #
    # Per-user overage is proportional share of total task overage,
    # NOT (user_t_ms - split_estimate). The old formula inflated numbers
    # because _fetch_time_entries_smart returns ALL entries including
    # child-subtask time, while est_direct only covered a subtask's own estimate.
    # =========================================================================

    @mcp.tool()
    def get_overtracked_report(
        project_name: Optional[str] = None,
        space_name: Optional[str] = None,
        period_type: str = "this_week",
        custom_start: Optional[str] = None,
        custom_end: Optional[str] = None,
        rolling_days: Optional[int] = None,
        include_archived: bool = True,
        async_job: bool = True,
        job_id: Optional[str] = None,
    ) -> dict:
        """
        Overtracked Report

        Employees where tracked time exceeds estimated time.

        Identifies tasks and team members where actual time logged exceeded the
        estimate, showing how much over they went. Only tasks with both an
        estimate AND logged time are included.

        ESTIMATION CONSISTENCY RULE:
          Main task (no parent): uses est_total / tracked_total
          Subtask (any depth):   uses est_direct / tracked_direct
        This keeps top-level oversight rollup-aware while evaluating execution
        subtasks by their own direct effort.

        Args:
            project_name:        Narrow to a specific project (optional)
            space_name:          Narrow to a specific space (optional)
            period_type:         today | yesterday | this_week | last_week |
                                 this_month | last_month | this_year | last_30_days |
                                 rolling | custom
            custom_start:        YYYY-MM-DD (required when period_type="custom")
            custom_end:          YYYY-MM-DD (required when period_type="custom")
            rolling_days:        Number of days for rolling window (1-365)
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
              flagged_task_hierarchy: [tree-like lines],
              formatted_output: <markdown>
            }
        """
        try:
            print(f"⌛ Overtime report / {period_type}")
            sys.stdout.flush()

            if not job_id:

                def _work():
                    return get_overtracked_report(
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

            # Resolve scope
            if project_name:
                list_ids = _list_ids_for_project(project_name)
                scope = project_name
            elif space_name:
                list_ids, resolved_space_name, monitored_applied, scope_error = (
                    _resolve_space_list_ids_for_reports(space_name, auto_monitored=True)
                )
                if scope_error:
                    return {"error": scope_error}
                scope = (
                    f"Monitored {resolved_space_name}"
                    if monitored_applied and not _split_monitored_scope(space_name)[0]
                    else space_name
                )
            else:
                return {"error": "Provide either project_name or space_name"}

            if not list_ids:
                return {"error": f"No lists found for scope '{scope}'"}

            all_tasks = _fetch_all_tasks(
                list_ids, {}, include_archived=include_archived
            )

            if len(all_tasks) >= 300 and not job_id and not async_job:
                return get_overtracked_report(
                    project_name=project_name,
                    space_name=space_name,
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
            task_by_id = {str(t["id"]): t for t in all_tasks}
            children_map: Dict[str, List[str]] = {}
            for t in all_tasks:
                pid = t.get("parent")
                if pid and str(pid) in task_by_id:
                    children_map.setdefault(str(pid), []).append(str(t["id"]))

            period_user_time_by_task: Dict[str, Dict[str, int]] = {}
            for t in all_tasks:
                tid = str(t["id"])
                period_user_time_by_task[tid] = (
                    filter_time_entries_by_user_and_date_range(
                        entries_map.get(tid, []), start_ms, end_ms
                    )
                )

            # Fallback for main tasks with no direct entries in range:
            # aggregate descendant entries so pure-rollup parents can still be evaluated.
            subtree_user_cache: Dict[str, Dict[str, int]] = {}

            def _subtree_user_time(tid: str) -> Dict[str, int]:
                if tid in subtree_user_cache:
                    return subtree_user_cache[tid]
                agg = dict(period_user_time_by_task.get(tid, {}) or {})
                for cid in children_map.get(tid, []):
                    for uname, ms in _subtree_user_time(cid).items():
                        agg[uname] = agg.get(uname, 0) + ms
                subtree_user_cache[tid] = agg
                return agg

            members: Dict[str, Dict] = {}
            total_overtime_tasks = 0
            flagged_tasks: Dict[str, Dict[str, Any]] = {}

            for task in all_tasks:
                task_id = str(task["id"])

                # -------------------------------------------------------
                # Main tasks are checked with rollup totals.
                # Subtasks (including nested) are checked with direct values.
                # -------------------------------------------------------
                is_main_task = not task.get("parent")
                metric_key = "total" if is_main_task else "direct"
                m = metrics.get(task_id, {})
                est_ms = m.get(f"est_{metric_key}", 0)
                metric_tracked_ms = m.get(f"tracked_{metric_key}", 0)

                # Only tasks with an estimate
                if est_ms == 0:
                    continue

                # Only tasks with tracked time in the period
                # Main task fallback: include descendant period entries if direct map is empty.
                user_time = dict(period_user_time_by_task.get(task_id, {}) or {})
                if is_main_task and not user_time:
                    user_time = _subtree_user_time(task_id)
                period_tracked_ms = sum(user_time.values())

                if period_tracked_ms == 0:
                    continue

                # Gate check: raw overage only (tracked must exceed estimate).
                # Main tasks use rollup totals; subtasks use direct values.
                overage_ms = metric_tracked_ms - est_ms
                if overage_ms <= 0:
                    continue

                total_overtime_tasks += 1
                status = _extract_status_name(task)
                flagged_tasks[task_id] = {
                    "task_id": task_id,
                    "parent_id": task.get("parent"),
                    "task_name": task.get("name", "Unnamed"),
                    "status": status,
                    "metric_basis": metric_key,
                    "estimated_ms": est_ms,
                    "tracked_ms": metric_tracked_ms,
                    "overage_ms": overage_ms,
                    "estimated": _format_duration(est_ms),
                    "tracked": _format_duration(metric_tracked_ms),
                    "overage": _format_duration(overage_ms),
                }

                # Distribute overage proportionally across users who tracked
                # in the period. This avoids attributing full task overage to
                # a single user when multiple users worked on the same task.
                total_period_ms = sum(user_time.values()) or 1
                for username, t_ms in user_time.items():
                    proportion = t_ms / total_period_ms
                    per_user_overage = int(overage_ms * proportion)
                    if per_user_overage <= 0:
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
                            # Show the rollup-consistent estimate.
                            "estimated": _format_duration(est_ms),
                            # Metric-basis tracked used for gate check (total for main, direct for subtask)
                            "tracked_basis": _format_duration(metric_tracked_ms),
                            # User's own tracked time in the selected period (used for share split)
                            "tracked_period": _format_duration(t_ms),
                            # Backward-compatible alias
                            "tracked": _format_duration(t_ms),
                            "overage": _format_duration(per_user_overage),
                            "overage_ms": per_user_overage,
                            "metric_basis": metric_key,
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

            # Build hierarchy-only view of flagged tasks.
            flagged_ids = set(flagged_tasks.keys())
            has_flagged_cache: Dict[str, bool] = {}
            root_cache: Dict[str, str] = {}

            def _has_flagged_subtree(tid: str) -> bool:
                if tid in has_flagged_cache:
                    return has_flagged_cache[tid]
                has_any = tid in flagged_ids
                if not has_any:
                    for cid in children_map.get(tid, []):
                        if _has_flagged_subtree(cid):
                            has_any = True
                            break
                has_flagged_cache[tid] = has_any
                return has_any

            def _root_id(tid: str) -> str:
                if tid in root_cache:
                    return root_cache[tid]
                seen = set()
                cur = tid
                while True:
                    node = task_by_id.get(cur, {})
                    pid = node.get("parent")
                    if not pid or str(pid) not in task_by_id or cur in seen:
                        break
                    seen.add(cur)
                    cur = str(pid)
                root_cache[tid] = cur
                return cur

            hierarchy_lines: List[str] = []

            def _render_children(parent_id: str, prefix: str = "") -> None:
                for cid in children_map.get(parent_id, []):
                    if not _has_flagged_subtree(cid):
                        continue
                    info = flagged_tasks.get(cid)
                    child_name = task_by_id.get(cid, {}).get("name", "Unnamed")
                    if info:
                        hierarchy_lines.append(
                            f"{prefix}|- {child_name} ({info['tracked']} > {info['estimated']}, "
                            f"over {info['overage']}, basis={info['metric_basis']})"
                        )
                    else:
                        hierarchy_lines.append(f"{prefix}|- {child_name} (context)")
                    _render_children(cid, prefix + "|  ")

            root_ids = sorted(
                {_root_id(tid) for tid in flagged_ids},
                key=lambda rid: task_by_id.get(rid, {}).get("name", "").lower(),
            )
            for rid in root_ids:
                root_name = task_by_id.get(rid, {}).get("name", "Unnamed")
                root_info = flagged_tasks.get(rid)
                if root_info:
                    hierarchy_lines.append(
                        f"Main Task: {root_name} ({root_info['tracked']} > {root_info['estimated']}, "
                        f"over {root_info['overage']}, basis={root_info['metric_basis']})"
                    )
                else:
                    hierarchy_lines.append(
                        f"Main Task: {root_name} (context: flagged descendants)"
                    )
                hierarchy_lines.append("|")
                _render_children(rid)
                hierarchy_lines.append("")

            # --- Markdown formatted_output ---
            lines = [
                "## Overtime Report (Tracked > Estimated)",
                f"**Scope:** {scope}  |  **Period:** {start_date} → {end_date}",
                f"**Flagged tasks:** {total_overtime_tasks}  |  "
                f"**Overage mode:** main=total, subtasks=direct",
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
                lines.append(
                    "| Task | Status | Basis | Estimated | Tracked (Basis) | Tracked in Period | Overage |"
                )
                lines.append(
                    "|------|--------|------:|----------:|----------------:|------------------:|--------:|"
                )
                for t in d["tasks"]:
                    lines.append(
                        f"| {t['task_name']} | {t['status']} | "
                        f"{t['metric_basis']} | {t['estimated']} | {t['tracked_basis']} | "
                        f"{t['tracked_period']} | {t['overage']} |"
                    )
                lines.append("")

            if hierarchy_lines:
                lines.append("## Flagged Task Hierarchy")
                lines.append(
                    "Main tasks are checked with total metrics; subtasks and nested subtasks are checked with direct metrics."
                )
                lines.append("")
                lines.append("```text")
                lines.extend(hierarchy_lines)
                lines.append("```")

            return {
                "scope": scope,
                "period": f"{start_date} to {end_date}",
                "period_type": period_type,
                "total_overtime_tasks": total_overtime_tasks,
                "members": formatted_members,
                "summary_table": summary_table,
                "flagged_task_hierarchy": hierarchy_lines,
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
        Check status of a background task-report job (max 5 polls).

        Returns status, poll count, and result when finished.
        Stop polling when STOP_POLLING is True or status is 'finished'/'failed'.
        """
        j = JOBS.get(job_id)
        if not j:
            return {
                "error": "job_id not found — it may have been created by a different tool"
            }

        poll_count = j.get("_poll_count", 0) + 1
        j["_poll_count"] = poll_count
        max_polls = 5
        status = j.get("status")

        if status in ("finished", "failed"):
            resp = {
                "job_id": job_id,
                "status": status,
                "error": j.get("error"),
                "poll_count": poll_count,
                "polls_remaining": 0,
                "STOP_POLLING": True,
                "result_available": status == "finished",
            }
            if status == "finished":
                resp["result"] = j.get("result")
                resp["message"] = "Job complete! Result included."
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
            "STOP_POLLING": False,
            "result_available": False,
            "message": f"Job still {status}. Wait 50-60s, then poll again.",
        }

    @mcp.tool()
    def get_task_report_job_result(job_id: str) -> dict:
        """Retrieve result of a finished background task-report job."""
        j = JOBS.get(job_id)
        if not j:
            return {"error": "job_id not found"}
        if j.get("status") != "finished":
            return {
                "status": j.get("status"),
                "result_available": False,
                "STOP_POLLING": False,
                "message": "Not ready yet.",
            }
        result = j.get("result")
        payload = {
            "status": "finished",
            "result_available": True,
            "STOP_POLLING": True,
            "result": result,
        }
        if isinstance(result, dict) and result.get("formatted_output"):
            payload["formatted_output"] = result.get("formatted_output")
        return payload


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
