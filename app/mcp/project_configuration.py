# app/mcp/project_configuration.py
"""
Project Configuration Module - Optimized
Features: Real-time status, Smart Deduplication, Hierarchy Tracking.
Includes robust status categorization for correct health calculation.
"""

import requests
from datetime import datetime
from typing import Dict, List, Optional
from fastmcp import FastMCP
from app.config import CLICKUP_API_TOKEN, BASE_URL

TRACKED_PROJECTS = []  # In-memory storage

# --- Standardized Status Logic (Consistent with PM Analytics) ---
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
    # 1. Check Overrides (Project Specific naming conventions)
    if cat := STATUS_OVERRIDE_MAP.get(status_name.upper()):
        return cat
    # 2. Check ClickUp Internal Type
    if status_type:
        type_map = {
            "open": "not_started",
            "done": "done",
            "closed": "closed",
            "custom": "active",
        }
        return type_map.get(status_type.lower(), "other")
    return "other"


# --- Helpers ---


def _api_call(
    method: str,
    endpoint: str,
    params: Optional[Dict] = None,
    payload: Optional[Dict] = None,
):
    try:
        headers = {
            "Authorization": CLICKUP_API_TOKEN,
            "Content-Type": "application/json",
        }
        resp = requests.request(
            method,
            f"{BASE_URL}{endpoint}",
            headers=headers,
            params=params,
            json=payload,
        )
        return (
            (resp.json(), None)
            if resp.status_code == 200
            else (None, f"API {resp.status_code}")
        )
    except Exception as e:
        return None, str(e)


def _get_list_ids(p: Dict) -> List[str]:
    """Recursively fetch List IDs for a tracked project (List, Folder, or Space)."""
    if p["type"] == "list":
        return [p["id"]]

    endpoint = f"/{p['type']}/{p['id']}"

    # Fetch direct lists
    resp, _ = _api_call("GET", f"{endpoint}/list")
    d_lists = resp.get("lists", []) if resp else []
    ids = [lst["id"] for lst in d_lists]

    # If space, fetch folder lists too
    if p["type"] == "space":
        resp, _ = _api_call("GET", f"{endpoint}/folder")
        folders = resp.get("folders", []) if resp else []
        for f in folders:
            ids.extend([lst["id"] for lst in f.get("lists", [])])

    return ids


def _calc_health(p: Dict) -> Dict:
    """
    Calculates project health based on Task Status distribution.
    Uses robust status logic to correctly identify 'Done' tasks (e.g. 'Shipped').
    """
    list_ids = _get_list_ids(p)
    if not list_ids:
        return {
            "status": "empty",
            "progress": "0%",
            "health": "Empty",
            "metrics": {"total": 0, "active": 0, "done": 0},
        }

    total, active, done = 0, 0, 0

    for lid in list_ids:
        # Fetch minimal fields to check health.
        # Using subtasks=true to get accurate count of all work items.
        data, _ = _api_call(
            "GET",
            f"/list/{lid}/task",
            params={"subtasks": "true", "include_closed": "true", "page": 0},
        )
        if not data:
            continue

        for t in data.get("tasks", []):
            total += 1

            # Use Robust Status Logic
            status_obj = t.get("status", {})
            status_name = status_obj.get("status", "")
            status_type = status_obj.get("type", "")

            cat = get_status_category(status_name, status_type)

            if cat in ["done", "closed"]:
                done += 1
            else:
                active += 1

    if total == 0:
        return {
            "status": "inactive",
            "progress": "0%",
            "health": "No Tasks",
            "metrics": {"total": 0, "active": 0, "done": 0},
        }

    progress = int((done / total) * 100)

    # Simple Health Heuristics
    if progress == 100:
        health = "Completed"
    elif active > 50:
        health = "Overloaded"
    elif active > 20 and progress < 20:
        health = "At Risk (Stagnant)"
    else:
        health = "Good"

    return {
        "status": "active" if active > 0 else "completed",
        "progress": f"{progress}%",
        "health": health,
        "metrics": {"total": total, "active": active, "done": done},
    }


def register_project_configuration_tools(mcp: FastMCP):
    @mcp.tool()
    def discover_projects(workspace_id: str, project_level: str = "folder") -> dict:
        """Scan workspace to find potential projects."""
        if project_level not in {"space", "folder", "list"}:
            return {"error": "Invalid level. Must be 'space', 'folder', or 'list'."}

        data, err = _api_call("GET", f"/team/{workspace_id}/space")
        if err:
            return {"error": err}

        results = []
        spaces = data.get("spaces", [])

        for s in spaces:
            if project_level == "space":
                results.append({"id": s["id"], "name": s["name"], "type": "space"})
            else:
                # Fetch folders
                f_data, _ = _api_call("GET", f"/space/{s['id']}/folder")
                folders = f_data.get("folders", []) if f_data else []

                if project_level == "folder":
                    results.extend(
                        [
                            {
                                "id": f["id"],
                                "name": f["name"],
                                "type": "folder",
                                "parent": s["name"],
                            }
                            for f in folders
                        ]
                    )
                elif project_level == "list":
                    # Direct lists in Space
                    l_data, _ = _api_call("GET", f"/space/{s['id']}/list")
                    if l_data and l_data.get("lists"):
                        results.extend(
                            [
                                {
                                    "id": lst["id"],
                                    "name": lst["name"],
                                    "type": "list",
                                    "parent": s["name"],
                                }
                                for lst in l_data.get("lists", [])
                            ]
                        )
                    # Lists in Folders
                    for f in folders:
                        results.extend(
                            [
                                {
                                    "id": lst["id"],
                                    "name": lst["name"],
                                    "type": "list",
                                    "parent": f["name"],
                                }
                                for lst in f.get("lists", [])
                            ]
                        )
        return {"count": len(results), "results": results}

    @mcp.tool()
    def add_project(name: str, type: str, id: str, workspace_id: str) -> dict:
        """Add unique project."""
        if type not in {"space", "folder", "list"}:
            return {"error": "Invalid type"}
        if any(p["id"] == id for p in TRACKED_PROJECTS):
            return {"message": "Already tracked."}

        # Validation & Hierarchy Check
        data, err = _api_call("GET", f"/{type}/{id}")
        if err:
            return {"error": f"ID invalid: {err}"}

        # Check overlaps (Prevent tracking a List if its Folder is already tracked)
        entity = data.get(type) or data
        pid = entity.get("folder", {}).get("id")
        sid = entity.get("space", {}).get("id")

        for p in TRACKED_PROJECTS:
            if (p["type"] == "folder" and pid == p["id"]) or (
                p["type"] == "space" and sid == p["id"]
            ):
                return {
                    "status": "skipped",
                    "message": f"Included in tracked {p['type']} '{p['name']}'",
                }

        TRACKED_PROJECTS.append(
            {
                "name": name,
                "type": type,
                "id": id,
                "workspace_id": workspace_id,
                "added_at": datetime.now().isoformat(),
            }
        )
        return {
            "status": "success",
            "message": f"Added '{name}'.",
            "total": len(TRACKED_PROJECTS),
        }

    @mcp.tool()
    def list_projects() -> dict:
        return {"projects": TRACKED_PROJECTS, "count": len(TRACKED_PROJECTS)}

    @mcp.tool()
    def remove_project(project_name: str) -> dict:
        global TRACKED_PROJECTS
        orig_len = len(TRACKED_PROJECTS)
        TRACKED_PROJECTS = [p for p in TRACKED_PROJECTS if p["name"] != project_name]
        return (
            {"message": "Removed."}
            if len(TRACKED_PROJECTS) < orig_len
            else {"error": "Not found."}
        )

    @mcp.tool()
    def refresh_projects() -> dict:
        """Verifies all tracked projects still exist in ClickUp."""
        global TRACKED_PROJECTS
        valid = []
        for p in TRACKED_PROJECTS:
            resp, _ = _api_call("GET", f"/{p['type']}/{p['id']}")
            if resp:
                valid.append(p)

        removed = len(TRACKED_PROJECTS) - len(valid)
        TRACKED_PROJECTS[:] = valid
        return {"status": "success", "removed_count": removed}

    @mcp.tool()
    def get_project_status(project_name: str) -> dict:
        """Get high-level status metrics for a project."""
        p = next((x for x in TRACKED_PROJECTS if x["name"] == project_name), None)
        return (
            {"project": p["name"], **_calc_health(p)} if p else {"error": "Not found"}
        )

    @mcp.tool()
    def get_all_projects_status() -> dict:
        """Get summary status for all tracked projects."""
        return {
            "projects": [
                {"name": p["name"], **_calc_health(p)} for p in TRACKED_PROJECTS
            ]
        }
