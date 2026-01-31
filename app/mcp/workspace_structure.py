# app/mcp/workspace_structure.py

from fastmcp import FastMCP
from app.clickup import _get, BASE_URL
from app.config import CLICKUP_TEAM_ID
import json  # ← added for pretty printing


def register_workspace_tools(mcp: FastMCP):
    def pretty_json(data):
        """Helper: return indented JSON string for readable terminal output"""
        return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)

    @mcp.tool
    def get_workspaces():
        """
        List all accessible workspaces (teams) in ClickUp.
        Returns pretty-printed JSON list.
        """
        try:
            teams_data = _get(f"{BASE_URL}/team")
            teams = teams_data.get("teams", [])

            if not teams:
                return {"error": "No teams/workspaces found for this API token"}

            result = [
                {
                    "workspace_id": t["id"],
                    "name": t["name"],
                    "color": t.get("color"),
                    "avatar": t.get("avatar"),
                    "plan": t.get("plan", {}).get("name", "Unknown"),
                    "members_count": len(t.get("members", [])),
                }
                for t in teams
            ]

            return result

        except Exception as e:
            return {"error": f"Failed to fetch workspaces: {str(e)}"}

    @mcp.tool
    def get_spaces(workspace_id: str = None):
        """
        List all spaces inside a specific workspace (team).
        Returns pretty-printed JSON list.
        """
        team_id = workspace_id or CLICKUP_TEAM_ID

        if not team_id:
            teams = _get(f"{BASE_URL}/team").get("teams", [])
            if not teams:
                return {"error": "No workspaces found"}
            team_id = teams[0]["id"]

        try:
            spaces_data = _get(f"{BASE_URL}/team/{team_id}/space")
            spaces = spaces_data.get("spaces", [])

            result = [
                {
                    "space_id": s["id"],
                    "name": s["name"],
                    "status_count": len(s.get("statuses", [])),
                    "multiple_assignees": s.get("multiple_assignees_enabled", False),
                    "archived": s.get("archived", False),
                    "color": s.get("color"),
                }
                for s in spaces
            ]

            return result

        except Exception as e:
            return {"error": f"Failed to fetch spaces: {str(e)}"}

    @mcp.tool
    def get_space(space_id: str):
        """
        Get detailed information about a specific ClickUp space.
        Returns pretty-printed JSON object.
        """
        try:
            # Try direct fetch first
            space_data = _get(f"{BASE_URL}/space/{space_id}")
            space = space_data.get("space", {})

            if space and space.get("id") == space_id:
                return format_space_details(space)

            # Fallback: search in team spaces
            print(f"[DEBUG] Direct fetch failed. Trying team fallback for {space_id}")
            team_id = CLICKUP_TEAM_ID
            if not team_id:
                teams = _get(f"{BASE_URL}/team").get("teams", [])
                if teams:
                    team_id = teams[0]["id"]

            if team_id:
                spaces_data = _get(f"{BASE_URL}/team/{team_id}/space")
                all_spaces = spaces_data.get("spaces", [])
                for s in all_spaces:
                    if s["id"] == space_id:
                        return format_space_details(s)

            return {
                "space_id": space_id,
                "error": "Space not found or not accessible",
                "hint": "Check if ID exists in get_spaces output and token has access",
            }

        except Exception as e:
            return {"error": str(e)}

    def format_space_details(space: dict) -> dict:
        """Consistent formatting helper"""
        return {
            "space_id": space["id"],
            "name": space["name"],
            "color": space.get("color"),
            "avatar": space.get("avatar"),
            "team_id": space.get("team_id"),
            "archived": space.get("archived", False),
            "multiple_assignees": space.get("multiple_assignees_enabled", False),
            "statuses": space.get("statuses", []),
            "status_count": len(space.get("statuses", [])),
            "features": space.get("features", {}),
            "permissions": space.get("permissions", {}),
        }

    @mcp.tool
    def get_folders(space_id: str):
        """
        List all folders inside a specific ClickUp space.

        Parameters:
        - space_id (string, required): The ID of the space to fetch folders from.

        Returns:
        Pretty-printed JSON list of folders with IDs, names, list count, etc.
        """
        try:
            folders_data = _get(f"{BASE_URL}/space/{space_id}/folder")
            folders = folders_data.get("folders", [])

            if not folders:
                return {
                    "space_id": space_id,
                    "folders": [],
                    "message": "No folders found in this space (or space has only folderless lists)",
                }

            result = []
            for f in folders:
                folder_info = {
                    "folder_id": f["id"],
                    "name": f["name"],
                    "list_count": len(f.get("lists", [])),
                    "hidden": f.get("hidden", False),
                    "orderindex": f.get("orderindex"),
                }

                # Optional: add basic list summary if lists exist
                if f.get("lists"):
                    folder_info["lists_summary"] = [
                        {"list_id": lst["id"], "name": lst["name"]}
                        for lst in f.get("lists", [])
                    ]

                result.append(folder_info)

            return result

        except Exception as e:
            return {"error": f"Failed to fetch folders for space {space_id}: {str(e)}"}

    @mcp.tool
    def get_folder(folder_id: str) -> dict:  # ← return dict, not str
        """
        Get details of a specific ClickUp folder, including all lists inside it.
        Uses fallback search across spaces if direct fetch fails.
        """
        try:
            # Step 1: Direct single-folder fetch
            folder_data = _get(f"{BASE_URL}/folder/{folder_id}")
            folder = folder_data.get("folder", {})

            if folder and folder.get("id") == folder_id:
                print(f"[DEBUG] Direct fetch OK for folder {folder_id}")
                return build_folder_result(folder)

            # Step 2: Fallback — scan all folders in default team
            print(f"[DEBUG] Direct failed. Starting fallback search for {folder_id}")

            team_id = CLICKUP_TEAM_ID
            if not team_id:
                teams = _get(f"{BASE_URL}/team").get("teams", [])
                if teams:
                    team_id = teams[0]["id"]

            if not team_id:
                return {"error": "No team found for fallback"}

            spaces_data = _get(f"{BASE_URL}/team/{team_id}/space")
            spaces = spaces_data.get("spaces", [])

            found_folder = None
            for space in spaces:
                space_id = space["id"]
                folders_data = _get(f"{BASE_URL}/space/{space_id}/folder")
                folders = folders_data.get("folders", [])

                for f in folders:
                    if f["id"] == folder_id:
                        found_folder = f
                        print(f"[DEBUG] Found folder {folder_id} in space {space_id}")
                        break  # stop inner loop
                if found_folder:
                    break  # stop outer loop

            if found_folder:
                return build_folder_result(found_folder)

            return {
                "folder_id": folder_id,
                "error": "Folder not found after full search",
                "hint": "Run get_folders on suspected spaces to confirm",
            }

        except Exception as e:
            print(f"[ERROR] get_folder failed: {str(e)}")
            return {"error": str(e)}

    def build_folder_result(folder: dict) -> dict:
        result = {
            "folder_id": folder["id"],
            "name": folder["name"],
            "hidden": folder.get("hidden", False),
            "orderindex": folder.get("orderindex"),
            "team_id": folder.get("team_id"),
            "space_id": folder.get("space", {}).get("id"),
            "space_name": folder.get("space", {}).get("name"),
            "list_count": len(folder.get("lists", [])),
            "lists": [],
        }

        for lst in folder.get("lists", []):
            result["lists"].append(
                {
                    "list_id": lst["id"],
                    "name": lst["name"],
                    "orderindex": lst.get("orderindex"),
                    "task_count": lst.get("task_count", 0),
                    "status_count": len(lst.get("statuses", [])),
                    "statuses": lst.get("statuses", []),
                }
            )

        return result

    @mcp.tool
    def get_lists(folder_id: str):
        """
        List all lists inside a specific ClickUp folder.

        Parameters:
        - folder_id (string, required): The ID of the folder to fetch lists from.

        Returns:
        Pretty-printed JSON list of lists with IDs, names, task counts, statuses, etc.
        """
        try:
            # Step 1: Try direct fetch of lists in folder
            lists_data = _get(f"{BASE_URL}/folder/{folder_id}/list")
            lists = lists_data.get("lists", [])

            # If we got lists → success
            if lists:
                print(
                    f"[DEBUG] Direct fetch successful for lists in folder {folder_id}"
                )
                return build_lists_result(lists, folder_id=folder_id)

            # Step 2: Fallback - find the folder first, then get its lists
            print(
                f"[DEBUG] Direct lists fetch empty. Falling back to folder search for {folder_id}"
            )

            team_id = CLICKUP_TEAM_ID
            if not team_id:
                teams = _get(f"{BASE_URL}/team").get("teams", [])
                if teams:
                    team_id = teams[0]["id"]

            if not team_id:
                return {"error": "No team/workspace found for fallback"}

            spaces_data = _get(f"{BASE_URL}/team/{team_id}/space")
            spaces = spaces_data.get("spaces", [])

            found_lists = None
            found_space_id = None
            for space in spaces:
                space_id = space["id"]
                folders_data = _get(f"{BASE_URL}/space/{space_id}/folder")
                folders = folders_data.get("folders", [])

                for f in folders:
                    if f["id"] == folder_id:
                        found_lists = _get(f"{BASE_URL}/folder/{folder_id}/list").get(
                            "lists", []
                        )
                        found_space_id = space_id
                        print(
                            f"[DEBUG] Found folder {folder_id} in space {space_id} via fallback"
                        )
                        break
                if found_lists is not None:
                    break

            if found_lists is not None:
                return build_lists_result(
                    found_lists, folder_id=folder_id, space_id=found_space_id
                )

            return {
                "folder_id": folder_id,
                "error": "No lists found in folder (or folder not accessible)",
                "hint": "Run get_folders on the parent space to confirm folder exists and has lists",
            }

        except Exception as e:
            print(f"[ERROR] get_lists failed: {str(e)}")
            return {"error": str(e)}

    def build_lists_result(
        lists: list, folder_id: str = None, space_id: str = None
    ) -> list[dict]:
        """Format list data consistently"""
        result = []
        for lst in lists:
            result.append(
                {
                    "list_id": lst["id"],
                    "name": lst["name"],
                    "orderindex": lst.get("orderindex"),
                    "task_count": lst.get("task_count", 0),
                    "status_count": len(lst.get("statuses", [])),
                    "statuses": lst.get("statuses", []),
                    "folder_id": folder_id,
                    "space_id": space_id or lst.get("space", {}).get("id"),
                }
            )
        return result

    @mcp.tool
    def get_folderless_lists(space_id: str):
        """
        List all lists that are directly in the space (not inside any folder).

        Parameters:
        - space_id (string, required): The ID of the space to fetch folderless lists from.

        Returns:
        Pretty-printed JSON list of folderless lists with IDs, names, task counts, etc.
        """
        try:
            lists_data = _get(f"{BASE_URL}/space/{space_id}/list")
            lists = lists_data.get("lists", [])

            if not lists:
                return {
                    "space_id": space_id,
                    "lists": [],
                    "message": "No folderless lists found in this space (all lists may be in folders, or space is empty)",
                }

            result = []
            for lst in lists:
                result.append(
                    {
                        "list_id": lst["id"],
                        "name": lst["name"],
                        "orderindex": lst.get("orderindex"),
                        "task_count": lst.get("task_count", 0),
                        "status_count": len(lst.get("statuses", [])),
                        "statuses": lst.get("statuses", []),  # full statuses if needed
                    }
                )

            return result

        except Exception as e:
            print(f"[ERROR] get_folderless_lists failed: {str(e)}")
            return {
                "error": f"Failed to fetch folderless lists for space {space_id}: {str(e)}",
                "hint": "Verify space_id exists in get_spaces output and token has access",
            }

    @mcp.tool
    def get_list(list_id: str):
        """
        Get detailed information about a specific ClickUp list.

        Parameters:
        - list_id (string, required): The ID of the list to retrieve.

        Returns:
        Pretty-printed JSON with list details or error message.
        """
        try:
            # Step 1: Direct fetch
            list_data = _get(f"{BASE_URL}/list/{list_id}")
            lst = list_data.get("list", {})

            if lst and lst.get("id") == list_id:
                print(f"[DEBUG] Direct fetch OK for list {list_id}")
                return build_list_result(lst)

            # Step 2: Fallback - search in all spaces/folders
            print(
                f"[DEBUG] Direct fetch failed. Starting fallback search for list {list_id}"
            )

            team_id = CLICKUP_TEAM_ID
            if not team_id:
                teams = _get(f"{BASE_URL}/team").get("teams", [])
                if teams:
                    team_id = teams[0]["id"]

            if not team_id:
                return {"error": "No team found for fallback"}

            spaces_data = _get(f"{BASE_URL}/team/{team_id}/space")
            spaces = spaces_data.get("spaces", [])

            found_list = None
            found_parent = None
            for space in spaces:
                space_id = space["id"]

                # Check folderless lists in space
                lists_data = _get(f"{BASE_URL}/space/{space_id}/list")
                lists = lists_data.get("lists", [])
                for lst in lists:
                    if lst["id"] == list_id:
                        found_list = lst
                        found_parent = {
                            "type": "space",
                            "id": space_id,
                            "name": space["name"],
                        }
                        print(
                            f"[DEBUG] Found list {list_id} as folderless in space {space_id}"
                        )
                        break

                if found_list:
                    break

                # Check lists inside folders
                folders_data = _get(f"{BASE_URL}/space/{space_id}/folder")
                folders = folders_data.get("folders", [])
                for folder in folders:
                    folder_id = folder["id"]
                    folder_lists = _get(f"{BASE_URL}/folder/{folder_id}/list").get(
                        "lists", []
                    )
                    for lst in folder_lists:
                        if lst["id"] == list_id:
                            found_list = lst
                            found_parent = {
                                "type": "folder",
                                "id": folder_id,
                                "name": folder["name"],
                            }
                            print(
                                f"[DEBUG] Found list {list_id} in folder {folder_id} in space {space_id}"
                            )
                            break
                    if found_list:
                        break
                if found_list:
                    break

            if found_list:
                result = build_list_result(found_list)
                result["parent"] = found_parent
                return result

            return {
                "list_id": list_id,
                "error": "List not found or not accessible after full search",
                "hint": "Run get_folderless_lists or get_folder to confirm list exists",
            }

        except Exception as e:
            print(f"[ERROR] get_list failed: {str(e)}")
            return {"error": str(e)}

    def build_list_result(lst: dict) -> dict:
        """Format list data consistently"""
        res = {
            "list_id": lst["id"],
            "name": lst["name"],
            "orderindex": lst.get("orderindex"),
            "content": lst.get("content"),
            "task_count": lst.get("task_count", 0),
            "folder_id": lst.get("folder", {}).get("id"),
            "folder_name": lst.get("folder", {}).get("name"),
            "space_id": lst.get("space", {}).get("id"),
            "space_name": lst.get("space", {}).get("name"),
            "statuses": lst.get("statuses", []),
            "status_count": len(lst.get("statuses", [])),
            "priority_enabled": lst.get("priority_enabled", False),
            "custom_fields_enabled": lst.get("custom_fields_enabled", False),
            "multiple_assignees_flag": lst.get("multiple_assignees", None),
            "multiple_assignees_enabled_flag": lst.get(
                "multiple_assignees_enabled", None
            ),
            "permissions": lst.get("permissions", {}),
        }

        # Stronger detection for multiple assignees:
        # 1) Check list-level explicit flags (new and old keys)
        # 2) Fallback to folder-level and space-level settings
        # 3) As a final check, scan recent tasks to see if multiple assignees are actually used

        def _truthy(v):
            return bool(v) or (isinstance(v, str) and v.lower() == "true")

        list_ma = None
        if res.get("multiple_assignees_enabled_flag") is not None:
            list_ma = _truthy(res.get("multiple_assignees_enabled_flag"))
        elif res.get("multiple_assignees_flag") is not None:
            list_ma = _truthy(res.get("multiple_assignees_flag"))

        source = "list" if list_ma else None

        # Check folder
        folder_id = res.get("folder_id")
        if list_ma is not True and folder_id:
            try:
                f_data = _get(f"{BASE_URL}/folder/{folder_id}") or {}
                folder_obj = f_data.get("folder", {})
                f_ma = folder_obj.get("multiple_assignees_enabled")
                if f_ma is None:
                    f_ma = folder_obj.get("multiple_assignees")
                if f_ma is not None:
                    f_ma = _truthy(f_ma)
                    if f_ma:
                        list_ma = True
                        source = "folder"
            except Exception:
                pass

        # Check space
        space_id = res.get("space_id")
        if list_ma is not True and space_id:
            try:
                s_data = _get(f"{BASE_URL}/space/{space_id}") or {}
                space_obj = s_data.get("space", {})
                s_ma = space_obj.get("multiple_assignees_enabled")
                if s_ma is None:
                    s_ma = space_obj.get("multiple_assignees")
                if s_ma is not None:
                    s_ma = _truthy(s_ma)
                    if s_ma:
                        list_ma = True
                        source = "space"
            except Exception:
                pass

        # Scan a small sample of tasks to infer actual usage
        tasks_have_multiple = False
        try:
            tasks_data = (
                _get(
                    f"{BASE_URL}/list/{res['list_id']}/task",
                    params={"page": 0, "subtasks": "false", "archived": "false"},
                )
                or {}
            )
            task_list = tasks_data.get("tasks", [])
            multi_count = 0
            for t in task_list[:50]:
                ass = t.get("assignees", []) or []
                if len(ass) > 1:
                    multi_count += 1
            tasks_have_multiple = multi_count > 0
            if tasks_have_multiple and not list_ma:
                # If tasks show multiple assignees but flags say false, mark as inferred
                source = "inferred_from_tasks"
                list_ma = True
        except Exception:
            pass

        res["effective_multiple_assignees"] = bool(list_ma)
        res["multiple_assignees_source"] = source or "none"
        res["tasks_have_multiple_assignees"] = tasks_have_multiple

        return res

    @mcp.tool
    def invalidate_cache(type: str = "all"):
        """
        Clear cached ClickUp data for fresh results.

        Parameters:
        - type (string, optional): What to clear. Options: 'all', 'workspaces', 'spaces', 'folders', 'lists', 'tasks'.
        Defaults to 'all'.

        Returns:
        Pretty-printed confirmation message.
        """
        from app.clickup import (
            fetch_all_spaces,
            fetch_all_lists_in_space,
        )  # import cached functions

        cleared = []

        try:
            if type in ("all", "workspaces"):
                fetch_all_spaces.cache_clear()
                cleared.append("workspaces")

            if type in ("all", "spaces", "folders", "lists"):
                fetch_all_lists_in_space.cache_clear()
                cleared.append("lists_in_space")

            # Add more caches if you create them later (e.g., task lists, time entries)
            # Example:
            # if type in ("all", "tasks"):
            #     fetch_tasks_from_list.cache_clear()
            #     cleared.append("tasks")

            if not cleared:
                return {
                    "status": "nothing_cleared",
                    "message": f"No caches cleared for type '{type}'. Valid types: 'all', 'workspaces', 'spaces', 'folders', 'lists', 'tasks'",
                }

            return {
                "status": "success",
                "cleared": cleared,
                "message": f"Cache cleared for: {', '.join(cleared)}",
            }

        except Exception as e:
            return {"status": "error", "message": f"Failed to clear cache: {str(e)}"}
