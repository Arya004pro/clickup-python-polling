"""
Project Intelligence Module for ClickUp MCP Server
Features: Health Scores, Standups, Digests, Risk Analysis.
Implements robust bottom-up time calculations and unified status mapping.
"""

import requests
import time
from fastmcp import FastMCP
from app.config import CLICKUP_API_TOKEN, BASE_URL
from .project_configuration import TRACKED_PROJECTS
from .status_helpers import (
    extract_status_name,
    get_effective_statuses,
    get_status_category,
    format_status_for_display,
)

# --- Status Configuration ---
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
    "done": [
        "SHIPPED",
        "RELEASE",
        "COMPLETE",
        "DONE",
        "RESOLVED",
        "PROD",
        "QC CHECK",
    ],
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


def _extract_status_name(task: dict) -> str:
    """Safely extracts status name handling both dict and string formats."""
    status = task.get("status")
    if isinstance(status, dict):
        return status.get("status", "Unknown")
    return str(status) if status else "Unknown"


# --- Helpers ---


def _api(method, endpoint, params=None):
    try:
        h = {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}
        r = requests.request(method, f"{BASE_URL}{endpoint}", headers=h, params=params)
        return (
            (r.json(), r.status_code)
            if r.status_code in [200, 201]
            else (None, r.status_code)
        )
    except Exception:
        return None, 500


def _get_ids(p_name):
    # 1. Try to find in tracked projects (in-memory)
    p = next((x for x in TRACKED_PROJECTS if x["name"] == p_name), None)

    # 2. If tracked, use that structure
    if p:
        if p["type"] == "list":
            return [p["id"]]
        ep = f"/{p['type']}/{p['id']}"
        data, _ = _api("GET", f"{ep}/list")
        ids = [lst["id"] for lst in (data or {}).get("lists", [])]
        if p["type"] == "space":
            f_data, _ = _api("GET", f"{ep}/folder")
            for f in (f_data or {}).get("folders", []):
                ids.extend([lst["id"] for lst in f.get("lists", [])])
        return ids

    # 3. Check project_map.json mapped projects
    try:
        from .sync_mapping import db

        for alias, data in db.projects.items():
            alias_name = data.get("alias", alias)
            if alias.lower() == p_name.lower() or alias_name.lower() == p_name.lower():
                mapped_id = data["clickup_id"]
                mapped_type = data["clickup_type"]

                if mapped_type == "list":
                    return [mapped_id]
                elif mapped_type == "folder":
                    resp, _ = _api("GET", f"/folder/{mapped_id}/list")
                    ids = [lst["id"] for lst in (resp or {}).get("lists", [])]
                    if not ids:
                        # Fallback: use cached structure
                        structure = data.get("structure", {})
                        ids = [
                            c["id"]
                            for c in structure.get("children", [])
                            if c.get("type") == "list"
                        ]
                    return ids
                elif mapped_type == "space":
                    ids = []
                    resp, _ = _api("GET", f"/space/{mapped_id}/list")
                    ids = [lst["id"] for lst in (resp or {}).get("lists", [])]
                    f_data, _ = _api("GET", f"/space/{mapped_id}/folder")
                    for f in (f_data or {}).get("folders", []):
                        ids.extend([lst["id"] for lst in f.get("lists", [])])
                    return ids
    except ImportError:
        pass

    # 4. If not tracked or mapped, try to resolve name to a List ID dynamically
    found_id, _ = _resolve_name_to_list_id(p_name)
    if found_id:
        return [found_id]

    return []


def _fetch_deep(list_ids):
    tasks, seen = [], set()
    for lid in list_ids:
        for arch in ["false", "true"]:
            p = 0
            while True:
                d, _ = _api(
                    "GET",
                    f"/list/{lid}/task",
                    {"page": p, "subtasks": "true", "archived": arch},
                )
                ts = d.get("tasks", []) if d else []
                if not ts:
                    break
                for t in ts:
                    if t["id"] not in seen:
                        seen.add(t["id"])
                        tasks.append(t)
                if len(ts) < 100:
                    break
                p += 1
    exist = {t["id"] for t in tasks}
    for pid in {
        t["parent"] for t in tasks if t.get("parent") and t["parent"] not in exist
    }:
        t, _ = _api("GET", f"/task/{pid}", {"include_subtasks": "true"})
        if t and t["id"] not in exist:
            tasks.append(t)
            exist.add(t["id"])
    return tasks


def _calc_time(tasks):
    t_map = {t["id"]: t for t in tasks}
    c_map = {}
    [
        c_map.setdefault(t["parent"], []).append(t["id"])
        for t in tasks
        if t.get("parent")
    ]
    cache = {}

    def get(tid):
        if tid in cache:
            return cache[tid]
        tr, est = 0, 0
        for cid in c_map.get(tid, []):
            c_tr, _, c_est, _ = get(cid)
            tr += c_tr
            est += c_est
        raw_t, raw_e = (
            int(t_map[tid].get("time_spent") or 0),
            int(t_map[tid].get("time_estimate") or 0),
        )
        d_tr, d_est = (
            max(0, raw_t - tr if raw_t >= tr else raw_t),
            max(0, raw_e - est if raw_e >= est else raw_e),
        )
        res = (d_tr + tr, d_tr, d_est + est, d_est)
        cache[tid] = res
        return res

    for t in tasks:
        get(t["id"])
    return cache


def _fmt(ms):
    return f"{int(ms) // 3600000}h {(int(ms) // 60000) % 60}m" if ms else "0m"


def _get_finish_date(task):
    status_cat = get_status_category(
        task.get("status", {}).get("status"), task.get("status", {}).get("type")
    )
    if status_cat not in ["done", "closed"]:
        return 0
    if task.get("date_closed"):
        return int(task["date_closed"])
    if task.get("date_done"):
        return int(task["date_done"])
    return int(task.get("date_updated") or 0)


# --- Deep Hierarchy Helpers ---


def _resolve_name_to_list_id(name: str):
    """
    Scans the workspace to find a List with the given name.
    """
    teams_data, _ = _api("GET", "/team")
    if not teams_data:
        return None, None
    team_id = teams_data["teams"][0]["id"]

    spaces_data, _ = _api("GET", f"/team/{team_id}/space")
    if not spaces_data:
        return None, None

    spaces = spaces_data.get("spaces", [])
    name_lower = name.lower().strip()

    for s in spaces:
        # Check folderless lists
        lists_data, _ = _api("GET", f"/space/{s['id']}/list")
        if lists_data:
            for lst in lists_data.get("lists", []):
                if lst["name"].lower().strip() == name_lower:
                    return lst["id"], lst["name"]

        # Check folders
        folders_data, _ = _api("GET", f"/space/{s['id']}/folder")
        if folders_data:
            for f in folders_data.get("folders", []):
                for lst in f.get("lists", []):
                    if lst["name"].lower().strip() == name_lower:
                        return lst["id"], lst["name"]
    return None, None


# Use extract_statuses_from_response from status_helpers module
# Removed local implementation in favor of shared helper


# --- Tools ---


def register_project_intelligence_tools(mcp: FastMCP):
    @mcp.tool()
    def get_workspace_folderless_lists(workspace_id: str) -> dict:
        """
        Scans the workspace to find all 'Folderless Lists'.
        """
        spaces_data, code = _api("GET", f"/team/{workspace_id}/space")
        if not spaces_data:
            return {"error": f"Failed to fetch spaces (HTTP {code})"}

        spaces = spaces_data.get("spaces", [])
        found_lists = []

        for space in spaces:
            space_id = space["id"]
            space_name = space["name"]
            lists_data, _ = _api("GET", f"/space/{space_id}/list")
            if lists_data:
                for lst in lists_data.get("lists", []):
                    found_lists.append(
                        {
                            "list_name": lst["name"],
                            "list_id": lst["id"],
                            "location": f"Space: {space_name}",
                            "space_id": space_id,
                            "task_count": lst.get("task_count", 0),
                        }
                    )

        return {
            "workspace_id": workspace_id,
            "total_folderless_lists": len(found_lists),
            "lists": found_lists,
        }

    @mcp.tool()
    def get_list_defined_statuses(list_id: str) -> dict:
        """
        Fetches the Effective Statuses for a list.
        If the list inherits statuses (returns empty), it automatically fetches from the Parent Space.
        Uses the centralized get_effective_statuses helper.
        """
        # Use the helper function which handles all the inheritance logic
        result = get_effective_statuses(list_id)

        # Check for errors
        if "error" in result:
            return result

        # Format statuses with categories
        formatted = [format_status_for_display(s) for s in result.get("statuses", [])]

        return {
            "list_id": list_id,
            "list_name": result.get("list_name"),
            "definition_source": result.get("source"),
            "status_count": len(formatted),
            "statuses": formatted,
        }

    @mcp.tool()
    def get_project_statuses(project_name: str) -> dict:
        """
        Gets defined statuses for a project (List, Folder, or Space).
        Uses Smart Resolution to find the exact List ID first.
        """
        debug_log = []

        # 1. Smart Resolve: Find List ID by name
        real_list_id, real_name = _resolve_name_to_list_id(project_name)

        if real_list_id:
            debug_log.append(
                f"Smart Resolved '{project_name}' to List ID: {real_list_id}"
            )
            # Delegate to our robust list fetcher
            return get_list_defined_statuses(real_list_id)

        # 2. Fallback to tracked
        project = next((x for x in TRACKED_PROJECTS if x["name"] == project_name), None)
        if not project:
            return {
                "error": f"Project '{project_name}' not found. Try providing the List ID directly to 'get_list_defined_statuses'."
            }

        # If it's a tracked list, use the list fetcher
        if project["type"] == "list":
            return get_list_defined_statuses(project["id"])

        # If it's a Folder/Space, use legacy logic (simplified here)
        entity_id = project["id"]
        entity_type = project["type"]

        defined_statuses = []
        source = "unknown"

        # Fetch directly from entity
        data, _ = _api("GET", f"/{entity_type}/{entity_id}")
        if data:
            # Extract based on type wrapper
            obj = data.get(entity_type, data)
            defined_statuses = obj.get("statuses", [])
            source = f"{entity_type}_settings"

        formatted_statuses = []
        for s in defined_statuses:
            cat = get_status_category(s.get("status"), s.get("type"))
            formatted_statuses.append(
                {
                    "status": s.get("status"),
                    "clickup_type": s.get("type"),
                    "our_category": cat,
                    "color": s.get("color"),
                    "origin": "defined_in_settings",
                }
            )

        return {
            "project": project_name,
            "definition_source": source,
            "total_statuses": len(formatted_statuses),
            "statuses": formatted_statuses,
        }

    # ... (Keep existing health/standup/blocker/risk tools as is) ...
    @mcp.tool()
    def get_project_health_score(project_name: str) -> dict:
        ids = _get_ids(project_name)
        if not ids:
            return {"error": f"Project '{project_name}' not found"}
        tasks = _fetch_deep(ids)
        metrics = _calc_time(tasks)
        if not tasks:
            return {"score": 0, "status": "No Tasks", "recommendations": []}

        now = time.time() * 1000
        active = [
            t
            for t in tasks
            if get_status_category(
                t.get("status", {}).get("status"), t.get("status", {}).get("type")
            )
            == "active"
        ]

        overdue = sum(
            1 for t in active if t.get("due_date") and int(t["due_date"]) < now
        )
        s_over = max(0, 100 - (overdue / len(active) * 100 * 2)) if active else 100

        stale = sum(1 for t in active if (now - int(t["date_updated"])) > 432000000)
        s_fresh = max(0, 100 - (stale / len(active) * 100)) if active else 100

        done_count = sum(
            1
            for t in tasks
            if get_status_category(
                t.get("status", {}).get("status"), t.get("status", {}).get("type")
            )
            in ["done", "closed"]
        )
        s_prog = (done_count / len(tasks)) * 100 if tasks else 0

        assigned = sum(1 for t in active if t.get("assignees"))
        s_cov = (assigned / len(active) * 100) if active else 100

        roots = [t for t in tasks if not t["parent"] or t["parent"] not in metrics]
        est_cov = sum(1 for t in roots if metrics.get(t["id"], (0, 0, 0, 0))[2] > 0)
        s_time = (est_cov / len(roots) * 100) if roots else 0

        score = (
            (s_over * 0.3)
            + (s_fresh * 0.2)
            + (s_prog * 0.25)
            + (s_cov * 0.15)
            + (s_time * 0.1)
        )

        recs = []
        if s_over < 70:
            recs.append(f"High risk: {overdue} overdue tasks.")
        if s_fresh < 70:
            recs.append(f"Stagnant: {stale} tasks untouched >5 days.")
        if s_cov < 80:
            recs.append("Assign active tasks to owners.")
        if s_time < 50:
            recs.append("Add estimates to root tasks.")

        return {
            "project": project_name,
            "score": int(round(score)),
            "grade": "A"
            if score >= 90
            else "B"
            if score >= 75
            else "C"
            if score >= 50
            else "F",
            "recommendations": recs or ["Healthy project."],
            "breakdown": {
                "overdue": int(s_over),
                "freshness": int(s_fresh),
                "progress": int(s_prog),
                "assignment": int(s_cov),
                "time_coverage": int(s_time),
            },
            "metrics": {"total": len(tasks), "active": len(active), "overdue": overdue},
        }

    @mcp.tool()
    def get_project_daily_standup(project_name: str) -> dict:
        ids = _get_ids(project_name)
        if not ids:
            return {"error": "Project not found"}
        tasks = _fetch_deep(ids)

        now = time.time() * 1000
        day_ms = 86400000
        yest_start = now - (day_ms * 2)
        yest_end = now - day_ms

        done_yest = [t for t in tasks if yest_start <= _get_finish_date(t) <= yest_end]
        active = [
            t
            for t in tasks
            if get_status_category(
                t.get("status", {}).get("status"), t.get("status", {}).get("type")
            )
            == "active"
        ]
        blocked = [
            t
            for t in active
            if "block" in _extract_status_name(t).lower()
            or (t.get("priority") or {}).get("orderindex") == "1"
        ]
        due_today = [
            t
            for t in active
            if t.get("due_date") and abs(int(t.get("due_date")) - now) < day_ms
        ]

        def _min(tl):
            return [
                {
                    "name": t["name"],
                    "status": _extract_status_name(t),
                    "assignees": [u["username"] for u in t.get("assignees", [])],
                }
                for t in tl
            ]

        return {
            "project": project_name,
            "completed_yesterday": _min(done_yest),
            "in_progress": _min(active[:15]),
            "blockers": _min(blocked),
            "due_today": _min(due_today),
        }

    @mcp.tool()
    def get_project_time_tracking(
        project_name: str, group_by: str = "assignee"
    ) -> dict:
        ids = _get_ids(project_name)
        if not ids:
            return {"error": "Project not found"}
        tasks = _fetch_deep(ids)
        metrics = _calc_time(tasks)
        rep = {}

        for t in tasks:
            m = metrics.get(t["id"], (0, 0, 0, 0))
            val_t = m[1] if group_by == "assignee" else m[0]
            val_e = m[3] if group_by == "assignee" else m[2]
            if not val_t and not val_e:
                continue

            keys = (
                [u["username"] for u in t.get("assignees", [])] or ["Unassigned"]
                if group_by == "assignee"
                else [t.get("status", {}).get("status")]
            )
            for k in keys:
                r = rep.setdefault(k, {"tracked": 0, "est": 0})
                r["tracked"] += val_t // len(keys)
                r["est"] += val_e // len(keys)

        return {
            "report": {
                k: {
                    **v,
                    "human_time": _fmt(v["tracked"]),
                    "human_est": _fmt(v["est"]),
                    "eff": f"{round(v['tracked'] / v['est'] * 100)}%"
                    if v["est"]
                    else "-",
                }
                for k, v in rep.items()
            }
        }

    @mcp.tool()
    def get_project_blockers(project_name: str, stale_days: int = 5) -> dict:
        ids = _get_ids(project_name)
        if not ids:
            return {"error": "Project not found"}
        tasks = _fetch_deep(ids)
        now = time.time() * 1000

        active = [
            t
            for t in tasks
            if get_status_category(
                t.get("status", {}).get("status"), t.get("status", {}).get("type")
            )
            == "active"
        ]
        blocked = [t for t in active if "block" in _extract_status_name(t).lower()]
        waiting = [t for t in active if "wait" in _extract_status_name(t).lower()]
        stale = [
            t
            for t in active
            if (now - int(t.get("date_updated") or 0)) > (stale_days * 86400000)
        ]

        def _f(tl):
            return [
                {
                    "id": t["id"],
                    "name": t["name"],
                    "status": _extract_status_name(t),
                    "assignee": [u["username"] for u in t.get("assignees", [])],
                }
                for t in tl
            ]

        return {
            "project": project_name,
            "blocked": _f(blocked),
            "waiting": _f(waiting),
            "stale": _f(stale),
            "recommendations": [
                "Unblock high priority tasks first.",
                "Ping assignees of stale tasks.",
            ]
            if blocked or stale
            else [],
        }

    @mcp.tool()
    def get_project_at_risk(project_name: str, risk_days: int = 3) -> dict:
        ids = _get_ids(project_name)
        if not ids:
            return {"error": "Project not found"}
        tasks = _fetch_deep(ids)
        now = time.time() * 1000
        limit = now + (risk_days * 86400000)

        active = [
            t
            for t in tasks
            if get_status_category(
                t.get("status", {}).get("status"), t.get("status", {}).get("type")
            )
            == "active"
        ]
        overdue = [t for t in active if t.get("due_date") and int(t["due_date"]) < now]
        at_risk = [
            t
            for t in active
            if t.get("due_date") and now <= int(t["due_date"]) <= limit
        ]

        return {
            "project": project_name,
            "overdue_count": len(overdue),
            "at_risk_count": len(at_risk),
            "urgent_tasks": [
                {"name": t["name"], "due": _fmt(int(t["due_date"]) - now) + " left"}
                for t in overdue + at_risk
            ],
            "recommendations": ["Reschedule overdue tasks immediately."]
            if overdue
            else [],
        }

    @mcp.tool()
    def get_project_weekly_digest(project_name: str) -> dict:
        """Weekly summary for stakeholders."""
        ids = _get_ids(project_name)
        if not ids:
            return {"error": "Project not found"}
        tasks = _fetch_deep(ids)
        now = time.time() * 1000
        week = 604800000

        done_wk = [
            t
            for t in tasks
            if _get_finish_date(t) > 0 and (now - _get_finish_date(t)) < week
        ]
        active = [
            t
            for t in tasks
            if get_status_category(
                t.get("status", {}).get("status"), t.get("status", {}).get("type")
            )
            == "active"
        ]
        risks = [t for t in active if t.get("due_date") and int(t["due_date"]) < now]

        contrib = {}
        for t in done_wk:
            for u in t.get("assignees", []):
                contrib[u["username"]] = contrib.get(u["username"], 0) + 1

        return {
            "project": project_name,
            "summary": f"{len(done_wk)} tasks done this week. {len(active)} active.",
            "key_metrics": {
                "completed_7d": len(done_wk),
                "active": len(active),
                "overdue": len(risks),
            },
            "completed_highlights": [t["name"] for t in done_wk[:5]],
            "team_contributions": contrib,
        }

    @mcp.tool()
    def get_project_team_workload(project_name: str) -> dict:
        ids = _get_ids(project_name)
        if not ids:
            return {"error": "Project not found"}
        tasks = _fetch_deep(ids)
        active = [
            t
            for t in tasks
            if get_status_category(
                t.get("status", {}).get("status"), t.get("status", {}).get("type")
            )
            == "active"
        ]

        load = {}
        for t in active:
            assignees = t.get("assignees", []) or [{"username": "Unassigned"}]
            for u in assignees:
                k = u.get("username", "Unknown")
                load[k] = load.get(k, 0) + 1

        avg = len(active) / max(1, len(load))
        recs = []
        for u, c in load.items():
            if c > (avg * 1.5) and c > 5:
                recs.append(f"{u} is overloaded ({c} tasks).")
            if c < (avg * 0.5) and u != "Unassigned":
                recs.append(f"{u} has capacity.")
        if "Unassigned" in load:
            recs.append(f"{load['Unassigned']} unassigned tasks.")

        return {
            "project": project_name,
            "workload": load,
            "total_active": len(active),
            "recommendations": recs,
        }
