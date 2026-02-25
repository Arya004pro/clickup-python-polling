"""
Sync & Mapping Module for ClickUp MCP Server
Responsible for managing the "Project Mapping" layer between ClickUp entities
and the MCP server, as well as handling data caching.

Acts as the Source of Truth for tracked projects.
"""

import json
import os
import time
import re
import threading
from typing import Dict, List, Optional, Any
import requests
from fastmcp import FastMCP
from apscheduler.schedulers.background import BackgroundScheduler
from zoneinfo import ZoneInfo
from app.config import CLICKUP_API_TOKEN, BASE_URL
from clickup_mcp.api_client import client as _client

# --- Constants & Configuration ---
DATA_FILE = "project_map.json"
CACHE_TTL_SECONDS = 3600  # 1 hour
HEADERS = {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}
MONITORING_CONFIG_FILE = os.path.join(
    os.path.dirname(__file__), "..", "monitoring_config.json"
)
DEFAULT_MAINTENANCE_TIMEZONE = "Asia/Kolkata"

# --- Persistence Layer ---


class PersistenceManager:
    """Handles loading and saving mappings to a local JSON file."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self._data = {"mapped_projects": {}, "discovery_cache": {}}
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    self._data = json.load(f)
            except json.JSONDecodeError:
                print(f"Error decoding {self.filepath}, starting with empty data.")

    def save(self):
        with open(self.filepath, "w") as f:
            json.dump(self._data, f, indent=2)

    @property
    def projects(self) -> Dict[str, Any]:
        return self._data.get("mapped_projects", {})

    @property
    def cache(self) -> Dict[str, Any]:
        return self._data.get("discovery_cache", {})

    def add_project(self, alias: str, data: dict):
        self._data["mapped_projects"][alias] = data
        self.save()

    def remove_project(self, alias: str):
        if alias in self._data["mapped_projects"]:
            del self._data["mapped_projects"][alias]
            self.save()

    def set_cache(self, key: str, data: any):
        self._data["discovery_cache"][key] = {"timestamp": time.time(), "data": data}
        self.save()

    def get_cache(self, key: str) -> Optional[Any]:
        entry = self._data["discovery_cache"].get(key)
        if not entry:
            return None
        if time.time() - entry["timestamp"] > CACHE_TTL_SECONDS:
            del self._data["discovery_cache"][key]
            self.save()
            return None
        return entry["data"]

    def clear_all(self):
        self._data = {"mapped_projects": {}, "discovery_cache": {}}
        self.save()

    def prune_expired_cache(self) -> int:
        keys_to_remove = []
        now = time.time()
        for key, entry in self._data["discovery_cache"].items():
            if now - entry.get("timestamp", 0) > CACHE_TTL_SECONDS:
                keys_to_remove.append(key)

        for k in keys_to_remove:
            del self._data["discovery_cache"][k]

        if keys_to_remove:
            self.save()
        return len(keys_to_remove)


db = PersistenceManager(DATA_FILE)

_maintenance_scheduler: Optional[BackgroundScheduler] = None
_maintenance_lock = threading.Lock()
_maintenance_running = False

# --- Helpers ---


def _slugify(text: str) -> str:
    """Converts a string to a slug-like alias."""
    text = text.lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _api_get(endpoint: str, params: dict = None) -> Optional[dict]:
    """Generic API GET wrapper — delegates to shared client for connection pooling."""
    try:
        data, err = _client.get(endpoint, params=params)
        if err:
            return None
        return data
    except Exception as e:
        print(f"API Error: {e}")
        return None


def _fetch_full_structure(entity_id: str, entity_type: str) -> dict:
    """
    Recursively fetches the internal structure of a ClickUp entity.
    Used for mapping projects.
    """
    structure = {"id": entity_id, "type": entity_type, "children": []}

    if entity_type == "space":
        # Get space details
        space_data = _api_get(f"/space/{entity_id}")
        if not space_data:
            return structure
        structure["name"] = space_data.get("name", "Unknown Space")

        # Get Folders
        folders = _api_get(f"/space/{entity_id}/folder")
        if folders:
            for f in folders.get("folders", []):
                structure["children"].append(_fetch_full_structure(f["id"], "folder"))

        # Get Folderless Lists
        lists = _api_get(f"/space/{entity_id}/list")
        if lists:
            for lst in lists.get("lists", []):
                structure["children"].append(
                    {"id": lst["id"], "name": lst["name"], "type": "list"}
                )

    elif entity_type == "folder":
        folder_data = _api_get(f"/folder/{entity_id}")
        if not folder_data:
            return structure
        structure["name"] = folder_data.get("name", "Unknown Folder")

        # Get Lists
        lists = _api_get(f"/folder/{entity_id}/list")
        if lists:
            for lst in lists.get("lists", []):
                structure["children"].append(
                    {"id": lst["id"], "name": lst["name"], "type": "list"}
                )

    elif entity_type == "list":
        list_data = _api_get(f"/list/{entity_id}")
        if list_data:
            structure["name"] = list_data.get("name", "Unknown List")
            # Lists are leaf nodes (ignoring tasks for structure mapping)

    return structure


def _search_entity_in_structure(structure: dict, search_name: str) -> Optional[dict]:
    """Recursively search for an entity by name in a structure."""
    if not structure or not search_name:
        return None
    search_lower = search_name.lower().strip()
    if structure.get("name", "").lower() == search_lower:
        return {
            "id": structure["id"],
            "name": structure["name"],
            "type": structure["type"],
            "structure": structure,
            "found_at": "root",
        }
    for child in structure.get("children", []):
        if child.get("name", "").lower() == search_lower:
            return {
                "id": child["id"],
                "name": child["name"],
                "type": child["type"],
                "structure": child,
                "parent_name": structure.get("name"),
                "parent_type": structure.get("type"),
                "parent_id": structure.get("id"),
                "found_at": "direct_child",
            }
        if child.get("children"):
            nested = _search_entity_in_structure(child, search_name)
            if nested:
                nested["parent_name"] = structure.get("name")
                nested["parent_type"] = structure.get("type")
                nested["parent_id"] = structure.get("id")
                nested["found_at"] = "nested"
                return nested
    return None


def _refresh_project_mapping(alias: str, project: dict) -> dict:
    """Refresh one mapped project's structure in project_map.json."""
    cid = project.get("clickup_id")
    ctype = project.get("clickup_type")
    if not cid or ctype not in {"space", "folder", "list"}:
        return {
            "alias": alias,
            "success": False,
            "error": "Missing or invalid clickup_id/clickup_type",
        }

    new_structure = _fetch_full_structure(cid, ctype)
    if "name" not in new_structure:
        return {
            "alias": alias,
            "success": False,
            "error": "Failed to fetch structure from ClickUp API",
        }

    updated = dict(project)
    updated["structure"] = new_structure
    updated["last_sync"] = time.time()
    db.add_project(alias, updated)
    return {
        "alias": alias,
        "success": True,
        "children": len(new_structure.get("children", [])),
    }


def _sync_monitoring_config_list_ids() -> dict:
    """
    Keep monitoring_config.json list_ids fresh by pulling live folder lists.
    """
    if not os.path.exists(MONITORING_CONFIG_FILE):
        return {
            "updated_projects": 0,
            "status": "skipped",
            "reason": "config_not_found",
        }

    try:
        with open(MONITORING_CONFIG_FILE, "r") as f:
            cfg = json.load(f)
    except Exception as e:
        return {"updated_projects": 0, "status": "error", "reason": f"read_failed: {e}"}

    projects = cfg.get("monitored_projects", [])
    changed = 0

    for p in projects:
        if p.get("type") != "folder" or not p.get("clickup_id"):
            continue

        resp = _api_get(f"/folder/{p['clickup_id']}/list")
        if not resp:
            continue

        live_ids = [lst.get("id") for lst in resp.get("lists", []) if lst.get("id")]
        live_ids = list(dict.fromkeys(live_ids))
        old_ids = [lid for lid in (p.get("list_ids") or []) if lid]

        if live_ids and live_ids != old_ids:
            p["list_ids"] = live_ids
            changed += 1

    if changed:
        try:
            with open(MONITORING_CONFIG_FILE, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            return {
                "updated_projects": 0,
                "status": "error",
                "reason": f"write_failed: {e}",
            }

    return {"updated_projects": changed, "status": "ok"}


def run_mapping_maintenance_once() -> dict:
    """
    Internal maintenance:
    1) Refresh every mapped project structure.
    2) Refresh monitored folder list_ids.
    3) Prune expired cache.
    """
    global _maintenance_running
    with _maintenance_lock:
        if _maintenance_running:
            return {"status": "skipped", "reason": "maintenance_already_running"}
        _maintenance_running = True

    try:
        refreshed = []
        failed = []
        for alias, project in list(db.projects.items()):
            result = _refresh_project_mapping(alias, project)
            if result.get("success"):
                refreshed.append(result["alias"])
            else:
                failed.append(
                    {"alias": alias, "error": result.get("error", "unknown_error")}
                )

        monitor_res = _sync_monitoring_config_list_ids()
        pruned = db.prune_expired_cache()

        return {
            "status": "success",
            "mapped_projects_refreshed": len(refreshed),
            "mapped_projects_failed": failed,
            "monitoring_config": monitor_res,
            "cache_entries_pruned": pruned,
            "ran_at": time.time(),
        }
    finally:
        _maintenance_running = False


def start_mapping_maintenance_scheduler(
    timezone: str = DEFAULT_MAINTENANCE_TIMEZONE,
    hour: int = 18,
    minute: int = 0,
    run_on_startup: bool = True,
) -> bool:
    """
    Start daily maintenance scheduler. Returns False if already running.
    """
    global _maintenance_scheduler
    if _maintenance_scheduler and _maintenance_scheduler.running:
        return False

    tz = ZoneInfo(timezone)
    _maintenance_scheduler = BackgroundScheduler(timezone=tz)
    _maintenance_scheduler.add_job(
        run_mapping_maintenance_once,
        trigger="cron",
        hour=hour,
        minute=minute,
        id="mapping_maintenance_daily",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _maintenance_scheduler.start()

    if run_on_startup:
        threading.Thread(target=run_mapping_maintenance_once, daemon=True).start()

    return True


def find_entity_anywhere(entity_name: str) -> Optional[dict]:
    """
    Universal entity finder. Searches for a space, folder, or list by name.
    Search priority:
    1. Mapped projects in project_map.json
    2. Live API search across all spaces
    """
    if not entity_name:
        return None
    search_lower = entity_name.lower().strip()

    # 1. Search in mapped projects
    for alias, data in db.projects.items():
        structure = data.get("structure", {})
        if search_lower in [
            alias.lower(),
            data.get("alias", "").lower(),
            structure.get("name", "").lower(),
        ]:
            return {
                "id": data["clickup_id"],
                "name": structure.get("name", alias),
                "type": data["clickup_type"],
                "structure": structure,
                "source": "project_map",
                "alias": alias,
                "found_at": "root",
            }
        result = _search_entity_in_structure(structure, entity_name)
        if result:
            result["source"] = "project_map"
            result["root_alias"] = alias
            return result

    # 2. Live API search
    teams_data = _api_get("/team")
    if not teams_data:
        return None
    team_id = teams_data["teams"][0]["id"]
    spaces_data = _api_get(f"/team/{team_id}/space")
    if not spaces_data:
        return None

    for space in spaces_data.get("spaces", []):
        if space["name"].lower() == search_lower:
            return {
                "id": space["id"],
                "name": space["name"],
                "type": "space",
                "structure": _fetch_full_structure(space["id"], "space"),
                "source": "api",
                "found_at": "root",
            }
        folders_data = _api_get(f"/space/{space['id']}/folder")
        if folders_data:
            for folder in folders_data.get("folders", []):
                if folder["name"].lower() == search_lower:
                    return {
                        "id": folder["id"],
                        "name": folder["name"],
                        "type": "folder",
                        "structure": _fetch_full_structure(folder["id"], "folder"),
                        "parent_name": space["name"],
                        "parent_type": "space",
                        "parent_id": space["id"],
                        "source": "api",
                        "found_at": "folder",
                    }
                lists_data = _api_get(f"/folder/{folder['id']}/list")
                if lists_data:
                    for lst in lists_data.get("lists", []):
                        if lst["name"].lower() == search_lower:
                            return {
                                "id": lst["id"],
                                "name": lst["name"],
                                "type": "list",
                                "structure": {
                                    "id": lst["id"],
                                    "name": lst["name"],
                                    "type": "list",
                                },
                                "parent_name": folder["name"],
                                "parent_type": "folder",
                                "parent_id": folder["id"],
                                "grandparent_name": space["name"],
                                "grandparent_type": "space",
                                "grandparent_id": space["id"],
                                "source": "api",
                                "found_at": "list_in_folder",
                            }
        lists_data = _api_get(f"/space/{space['id']}/list")
        if lists_data:
            for lst in lists_data.get("lists", []):
                if lst["name"].lower() == search_lower:
                    return {
                        "id": lst["id"],
                        "name": lst["name"],
                        "type": "list",
                        "structure": {
                            "id": lst["id"],
                            "name": lst["name"],
                            "type": "list",
                        },
                        "parent_name": space["name"],
                        "parent_type": "space",
                        "parent_id": space["id"],
                        "source": "api",
                        "found_at": "folderless_list",
                    }
    return None


# --- Tools Definition ---


def register_sync_mapping_tools(mcp: FastMCP):
    @mcp.tool()
    def discover_hierarchy(
        workspace_id: str = None, show_archived: bool = False
    ) -> dict:
        """
        List all spaces, folders, and lists in a workspace for project mapping.
        Fetches the full tree and caches the result.
        """
        # 1. Resolve Workspace
        target_ws_id = workspace_id
        if not target_ws_id:
            teams_data = _api_get("/team")
            if not teams_data or not teams_data.get("teams"):
                return {"error": "No teams/workspaces found."}
            target_ws_id = teams_data["teams"][0]["id"]

        cache_key = f"hierarchy_{target_ws_id}_{show_archived}"
        cached = db.get_cache(cache_key)
        if cached:
            return {"source": "cache", "data": cached}

        # 2. Fetch Spaces
        spaces_data = _api_get(
            f"/team/{target_ws_id}/space", {"archived": str(show_archived).lower()}
        )
        if not spaces_data:
            return {"error": "Failed to fetch spaces."}

        hierarchy = []
        for space in spaces_data.get("spaces", []):
            space_node = {
                "id": space["id"],
                "name": space["name"],
                "type": "space",
                "folders": [],
                "folderless_lists": [],
            }

            # 3. Fetch Folders
            folders_data = _api_get(
                f"/space/{space['id']}/folder", {"archived": str(show_archived).lower()}
            )
            if folders_data:
                for folder in folders_data.get("folders", []):
                    folder_node = {
                        "id": folder["id"],
                        "name": folder["name"],
                        "type": "folder",
                        "lists": [],
                    }
                    # 4. Fetch Lists inside Folders
                    lists_data = _api_get(
                        f"/folder/{folder['id']}/list",
                        {"archived": str(show_archived).lower()},
                    )
                    if lists_data:
                        folder_node["lists"] = [
                            {"id": lst["id"], "name": lst["name"], "type": "list"}
                            for lst in lists_data.get("lists", [])
                        ]
                    space_node["folders"].append(folder_node)

            # 5. Fetch Folderless Lists
            fl_lists_data = _api_get(
                f"/space/{space['id']}/list", {"archived": str(show_archived).lower()}
            )
            if fl_lists_data:
                space_node["folderless_lists"] = [
                    {"id": lst["id"], "name": lst["name"], "type": "list"}
                    for lst in fl_lists_data.get("lists", [])
                ]

            hierarchy.append(space_node)

        result = {"workspace_id": target_ws_id, "hierarchy": hierarchy}
        db.set_cache(cache_key, result)
        return {"source": "api", "data": result}

    @mcp.tool()
    def map_project(id: str, type: str, alias: str = None) -> dict:
        """
        Map a ClickUp entity (Space, Folder, or List) as a 'Project'.
        Verifies ID/Name, fetches internal structure, and persists mapping.

        Args:
            id: Entity ID or Name (will auto-resolve names to IDs for spaces).
            type: Entity type - must be 'space', 'folder', or 'list'.
            alias: Optional custom alias for the project (auto-generated if not provided).

        Returns:
            Mapping confirmation with project details.
        """
        if type not in ["space", "folder", "list"]:
            return {"error": "Type must be 'space', 'folder', or 'list'."}

        # Step 1: Resolve name to ID if needed (for spaces)
        resolved_id = id
        if type == "space" and not id.isdigit():
            # Try to resolve space name to ID
            teams_data = _api_get("/team")
            if teams_data and teams_data.get("teams"):
                team_id = teams_data["teams"][0]["id"]
                spaces_data = _api_get(f"/team/{team_id}/space")

                if spaces_data:
                    all_spaces = spaces_data.get("spaces", [])
                    found = False

                    for s in all_spaces:
                        if s["name"].lower() == id.lower():
                            resolved_id = s["id"]
                            found = True
                            break

                    if not found:
                        return {
                            "error": f"Space '{id}' not found",
                            "hint": f"Available spaces: {[s['name'] for s in all_spaces]}",
                            "available_spaces": [
                                {"id": s["id"], "name": s["name"]} for s in all_spaces
                            ],
                        }

        # Step 2: Verify ID and get initial structure
        structure = _fetch_full_structure(resolved_id, type)
        if "name" not in structure:
            return {
                "error": f"Could not verify {type} with ID/Name '{id}'. Check ID or permissions."
            }

        final_alias = alias or _slugify(structure["name"])

        # Step 3: Check if alias exists
        if final_alias in db.projects:
            return {
                "error": f"Alias '{final_alias}' already exists. Please choose another."
            }

        mapping_data = {
            "alias": final_alias,
            "clickup_id": resolved_id,
            "clickup_type": type,
            "last_sync": time.time(),
            "structure": structure,
        }

        db.add_project(final_alias, mapping_data)

        return {
            "success": True,
            "message": f"Mapped {type} '{structure['name']}' as '{final_alias}'",
            "project_details": mapping_data,
        }

    @mcp.tool()
    def list_mapped_projects() -> List[Dict[str, Any]]:
        """Show all currently mapped projects."""
        projects = db.projects
        output = []
        for alias, data in projects.items():
            output.append(
                {
                    "alias": alias,
                    "clickup_id": data.get("clickup_id"),
                    "type": data.get("clickup_type"),
                    "last_sync": time.ctime(data.get("last_sync", 0)),
                }
            )
        return output

    @mcp.tool()
    def get_mapped_project(alias: str) -> dict:
        """Get detailed info about a specifically mapped project."""
        project = db.projects.get(alias)
        if not project:
            return {"error": f"Project alias '{alias}' not found."}
        return project["structure"]

    @mcp.tool()
    def refresh_project(alias: str) -> dict:
        """Force a refresh of a project's structure from ClickUp."""
        project = db.projects.get(alias)
        if not project:
            return {"error": f"Project alias '{alias}' not found."}
        result = _refresh_project_mapping(alias, project)
        if not result.get("success"):
            return {
                "error": result.get(
                    "error", "Failed to refresh structure from ClickUp API."
                )
            }

        return {
            "success": True,
            "message": f"Refreshed structure for '{alias}'",
            "structure_summary": f"Contains {result.get('children', 0)} top-level items.",
        }

    @mcp.tool()
    def unmap_project(alias: str) -> dict:
        """Remove a project mapping."""
        if alias not in db.projects:
            return {"error": f"Project alias '{alias}' not found."}

        db.remove_project(alias)
        return {"success": True, "message": f"Unmapped project '{alias}'"}

    @mcp.tool()
    def get_sync_status() -> dict:
        """Get the overall health of the sync and cache."""
        return {
            "mapped_projects_count": len(db.projects),
            "cached_items_count": len(db.cache),
            "storage_file": DATA_FILE,
            "status": "active",
        }

    @mcp.tool()
    def list_spaces(workspace_id: str = None) -> dict:
        """List all spaces and indicate which ones are already mapped."""
        # Get actual spaces
        teams_data = _api_get("/team")
        if not teams_data:
            return {"error": "API Error"}

        ws_id = workspace_id or teams_data["teams"][0]["id"]
        spaces_data = _api_get(f"/team/{ws_id}/space")

        if not spaces_data:
            return {"error": "Failed to fetch spaces."}

        # Get local map IDs for comparison
        mapped_ids = {
            p["clickup_id"]
            for p in db.projects.values()
            if p["clickup_type"] == "space"
        }

        output = []
        for s in spaces_data.get("spaces", []):
            output.append(
                {"id": s["id"], "name": s["name"], "is_mapped": s["id"] in mapped_ids}
            )

        return {"workspace_id": ws_id, "spaces": output}

    @mcp.tool()
    def clear_sync(confirm: bool) -> dict:
        """Clear all project mappings and discovery cache. Destructive."""
        if not confirm:
            return {"error": "You must set confirm=True to wipe the database."}

        db.clear_all()
        return {"success": True, "message": "All mappings and cache have been wiped."}

    @mcp.tool()
    def prune_cache() -> dict:
        """Remove expired cache entries."""
        count = db.prune_expired_cache()
        return {"success": True, "removed_entries": count, "message": "Cache pruned."}

    @mcp.tool()
    def find_project_anywhere(project_name: str) -> dict:
        """
        Universal project/entity finder. Search for any space, folder, or list by name.
        Works regardless of where the entity is located in the hierarchy.
        Use this BEFORE any report tool to resolve the entity type and ID.
        """
        result = find_entity_anywhere(project_name)
        if not result:
            return {
                "error": f"Project '{project_name}' not found",
                "suggestion": "Try discover_hierarchy() to see all available spaces, folders, and lists",
            }
        response = {
            "found": True,
            "name": result["name"],
            "id": result["id"],
            "type": result["type"],
            "source": result["source"],
            "location": result.get("found_at", "unknown"),
        }
        if result.get("parent_name"):
            response["parent"] = {
                "name": result["parent_name"],
                "type": result["parent_type"],
                "id": result["parent_id"],
            }
        if result.get("grandparent_name"):
            response["grandparent"] = {
                "name": result["grandparent_name"],
                "type": result["grandparent_type"],
                "id": result["grandparent_id"],
            }
        if result.get("structure"):
            children = result["structure"].get("children", [])
            if children:
                response["contains"] = {
                    "total_children": len(children),
                    "children": [
                        {"name": c.get("name"), "type": c.get("type")}
                        for c in children[:10]
                    ],
                }
                if len(children) > 10:
                    response["contains"]["note"] = (
                        f"Showing 10 of {len(children)} items"
                    )
        response["usage_hint"] = (
            f"Use this {result['type']} ID ({result['id']}) with report generation tools"
        )
        return response

    @mcp.tool()
    def get_environment_context() -> dict:
        """
        Bootstrap tool: returns MCP environment state including
        mapped projects, cache status, and usage guidance.
        Call this at session start.
        """
        projects = [
            {
                "alias": alias,
                "clickup_id": data.get("clickup_id"),
                "type": data.get("clickup_type"),
                "last_sync": data.get("last_sync"),
            }
            for alias, data in db.projects.items()
        ]

        return {
            "mapped_projects_count": len(projects),
            "mapped_projects": projects,
            "cached_items": len(db.cache),
            "storage": DATA_FILE,
            "server_status": "ready",
            "usage_guidance": {
                "mapped_projects": "Use mapped project tools for analytics/reporting.",
                "raw_clickup": "Use raw fetch tools only for discovery or unmapped entities.",
            },
        }
