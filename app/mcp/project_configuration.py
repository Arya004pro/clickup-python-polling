# app/mcp/project_configuration.py
"""
Project Configuration Module - Optimized
Features: Real-time status, Smart Deduplication, Hierarchy Tracking.
"""

import requests
from datetime import datetime
from typing import Dict, List
from fastmcp import FastMCP
from app.config import CLICKUP_API_TOKEN, BASE_URL

TRACKED_PROJECTS = []  # In-memory storage


def _api_call(method: str, endpoint: str, params: None = None, payload: None = None):
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
    if p["type"] == "list":
        return [p["id"]]
    endpoint = f"/{p['type']}/{p['id']}"
    # Fetch direct lists
    d_lists = (
        _api_call("GET", f"{endpoint}/list")[0].get("lists", [])
        if _api_call("GET", f"{endpoint}/list")[0]
        else []
    )
    ids = [lst["id"] for lst in d_lists]
    # If space, fetch folder lists too
    if p["type"] == "space":
        folders = (
            _api_call("GET", f"{endpoint}/folder")[0].get("folders", [])
            if _api_call("GET", f"{endpoint}/folder")[0]
            else []
        )
        for f in folders:
            ids.extend([lst["id"] for lst in f.get("lists", [])])
    return ids


def _calc_health(p: Dict) -> Dict:
    list_ids = _get_list_ids(p)
    if not list_ids:
        return {
            "status": "empty",
            "progress": "0%",
            "health": "Empty",
            "metrics": {"total": 0},
        }

    total, active, done = 0, 0, 0
    for lid in list_ids:
        # Optimization: Fetch minimal fields if possible, page=0 checks health
        data, _ = _api_call(
            "GET",
            f"/list/{lid}/task",
            params={"subtasks": "true", "include_closed": "true", "page": 0},
        )
        if not data:
            continue
        for t in data.get("tasks", []):
            total += 1
            if t.get("status", {}).get("type") == "closed" or t.get("status", {}).get(
                "status"
            ) in ["complete", "done", "shipped"]:
                done += 1
            else:
                active += 1

    if total == 0:
        return {
            "status": "inactive",
            "progress": "0%",
            "health": "No Tasks",
            "metrics": {"total": 0},
        }
    progress = int((done / total) * 100)
    health = (
        "Completed"
        if progress == 100
        else "Overloaded"
        if active > 50
        else "At Risk (Stagnant)"
        if (active > 20 and progress < 20)
        else "Good"
    )
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
            return {"error": "Invalid level"}
        data, err = _api_call("GET", f"/team/{workspace_id}/space")
        if err:
            return {"error": err}

        results = []
        for s in data.get("spaces", []):
            if project_level == "space":
                results.append({"id": s["id"], "name": s["name"], "type": "space"})
            else:
                # Fetch folders
                f_data = _api_call("GET", f"/space/{s['id']}/folder")[0]
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
                    # Direct lists
                    l_data = _api_call("GET", f"/space/{s['id']}/list")[0]
                    if l_data:
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
                    # Folder lists
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

        # Simple Hierarchy check logic
        entity = data.get(type) or data
        pid, sid = entity.get("folder", {}).get("id"), entity.get("space", {}).get("id")
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
        global TRACKED_PROJECTS
        valid = [
            p
            for p in TRACKED_PROJECTS
            if _api_call("GET", f"/{p['type']}/{p['id']}")[0]
        ]
        removed = len(TRACKED_PROJECTS) - len(valid)
        TRACKED_PROJECTS[:] = valid
        return {"status": "success", "removed_count": removed}

    @mcp.tool()
    def get_project_status(project_name: str) -> dict:
        p = next((x for x in TRACKED_PROJECTS if x["name"] == project_name), None)
        return (
            {"project": p["name"], **_calc_health(p)} if p else {"error": "Not found"}
        )

    @mcp.tool()
    def get_all_projects_status() -> dict:
        return {
            "projects": [
                {"name": p["name"], **_calc_health(p)} for p in TRACKED_PROJECTS
            ]
        }
