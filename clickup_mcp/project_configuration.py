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
from .api_client import client as _client
from .clickup_shared import (
    STATUS_NAME_OVERRIDES,
    STATUS_OVERRIDE_MAP,
    get_status_category,
)

TRACKED_PROJECTS = []  # In-memory storage


# --- Helpers (delegated to shared client) ---


def _api_call(
    method: str,
    endpoint: str,
    params: Optional[Dict] = None,
    payload: Optional[Dict] = None,
):
    """API call handler — delegates to shared client for connection pooling."""
    method_upper = method.upper()
    fn = {
        "GET": _client.get,
        "POST": _client.post,
        "PUT": _client.put,
        "DELETE": _client.delete,
    }.get(method_upper, _client.get)
    kwargs = {}
    if params:
        kwargs["params"] = params
    if payload:
        kwargs["json"] = payload
    return fn(endpoint, **kwargs)


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
