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
from typing import Dict, List, Optional, Any
import requests
from fastmcp import FastMCP
from app.config import CLICKUP_API_TOKEN, BASE_URL

# --- Constants & Configuration ---
DATA_FILE = "project_map.json"
CACHE_TTL_SECONDS = 3600  # 1 hour
HEADERS = {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}

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

# --- Helpers ---


def _slugify(text: str) -> str:
    """Converts a string to a slug-like alias."""
    text = text.lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _api_get(endpoint: str, params: dict = None) -> Optional[dict]:
    """Generic API GET wrapper."""
    try:
        response = requests.get(f"{BASE_URL}{endpoint}", headers=HEADERS, params=params)
        if response.status_code == 200:
            return response.json()
        return None
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
        Verifies ID, fetches internal structure, and persists mapping.
        """
        if type not in ["space", "folder", "list"]:
            return {"error": "Type must be 'space', 'folder', or 'list'."}

        # Verify ID and get initial Name
        structure = _fetch_full_structure(id, type)
        if "name" not in structure:
            return {
                "error": f"Could not verify {type} with ID {id}. Check ID or permissions."
            }

        final_alias = alias or _slugify(structure["name"])

        # Check if alias exists
        if final_alias in db.projects:
            return {
                "error": f"Alias '{final_alias}' already exists. Please choose another."
            }

        mapping_data = {
            "alias": final_alias,
            "clickup_id": id,
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
    def list_projects() -> List[Dict[str, Any]]:
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
    def get_project(alias: str) -> dict:
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

        cid = project["clickup_id"]
        ctype = project["clickup_type"]

        # Re-fetch structure
        new_structure = _fetch_full_structure(cid, ctype)
        if "name" not in new_structure:
            return {"error": "Failed to refresh structure from ClickUp API."}

        # Update DB
        project["structure"] = new_structure
        project["last_sync"] = time.time()
        db.add_project(alias, project)  # Overwrite

        return {
            "success": True,
            "message": f"Refreshed structure for '{alias}'",
            "structure_summary": f"Contains {len(new_structure.get('children', []))} top-level items.",
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
