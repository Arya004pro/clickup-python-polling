"""
Status Helper Functions for ClickUp MCP Server
Provides unified status handling across all MCP tools.
Handles API response polymorphism and status inheritance.
"""

import requests
from typing import List, Dict, Optional, Any
from app.config import CLICKUP_API_TOKEN, BASE_URL


# --- Status Category Mapping ---
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


def _headers() -> Dict[str, str]:
    """Get API headers."""
    return {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}


def _api_get(
    endpoint: str, params: Optional[Dict] = None
) -> tuple[Optional[Dict], Optional[str]]:
    """Generic API GET wrapper with error handling."""
    url = f"{BASE_URL}{endpoint}"
    try:
        response = requests.get(url, headers=_headers(), params=params)
        return (
            (response.json(), None)
            if response.status_code == 200
            else (None, f"API Error {response.status_code}")
        )
    except Exception as e:
        return None, str(e)


# --- Core Status Extraction Functions ---


def extract_status_name(task: Dict) -> str:
    """
    Safely extracts status name from a task object.
    Handles both dict and string formats.

    Args:
        task: Task dictionary from ClickUp API

    Returns:
        Status name as string, or "Unknown" if not found
    """
    status = task.get("status")
    if isinstance(status, dict):
        return status.get("status", "Unknown")
    return str(status) if status else "Unknown"


def extract_status_type(task: Dict) -> Optional[str]:
    """
    Extracts status type from a task object.

    Args:
        task: Task dictionary from ClickUp API

    Returns:
        Status type string or None
    """
    status = task.get("status")
    if isinstance(status, dict):
        return status.get("type")
    return None


def extract_statuses_from_response(data_obj: Any) -> List[Dict]:
    """
    Safe extractor for status lists from various API response types.
    Handles wrapped and unwrapped responses.

    Args:
        data_obj: API response object (list, space, or folder response)

    Returns:
        List of status dictionaries, empty list if not found
    """
    if not data_obj:
        return []

    # Try direct statuses field
    if "statuses" in data_obj:
        return data_obj.get("statuses", [])

    # Try wrapped list response
    if "list" in data_obj and isinstance(data_obj["list"], dict):
        return data_obj["list"].get("statuses", [])

    # Try wrapped space response
    if "space" in data_obj and isinstance(data_obj["space"], dict):
        return data_obj["space"].get("statuses", [])

    # Try wrapped folder response
    if "folder" in data_obj and isinstance(data_obj["folder"], dict):
        return data_obj["folder"].get("statuses", [])

    return []


def get_effective_statuses(list_id: str) -> Dict[str, Any]:
    """
    Get the effective statuses for a list, handling inheritance from parent space.

    ClickUp lists inherit statuses from their parent space if they don't have
    custom statuses defined. This function implements the inheritance logic.

    Args:
        list_id: The ClickUp list ID

    Returns:
        Dictionary with:
        - statuses: List of status dictionaries
        - status_count: Number of statuses
        - source: Where the statuses came from ("list_custom" or "space_inherited")
        - space_id: The parent space ID (if applicable)
    """
    # 1. Fetch the list
    list_data, err = _api_get(f"/list/{list_id}")
    if err or not list_data:
        return {
            "statuses": [],
            "status_count": 0,
            "source": "error",
            "error": err or "Failed to fetch list",
        }

    # Handle wrapped vs unwrapped response
    list_obj = list_data.get("list", list_data)
    statuses = extract_statuses_from_response(list_obj)

    # 2. If list has custom statuses, return them
    if statuses:
        return {
            "statuses": statuses,
            "status_count": len(statuses),
            "source": "list_custom",
            "list_name": list_obj.get("name"),
        }

    # 3. List has no custom statuses - inherit from parent space
    space_obj = list_obj.get("space", {})
    space_id = space_obj.get("id")

    # If space info is missing, try to find it via folder
    if not space_id:
        folder_obj = list_obj.get("folder", {})
        folder_id = folder_obj.get("id")

        if folder_id:
            folder_data, _ = _api_get(f"/folder/{folder_id}")
            if folder_data:
                f_obj = folder_data.get("folder", folder_data)
                space_id = f_obj.get("space", {}).get("id")

    # 4. Fetch space statuses
    if space_id:
        space_data, _ = _api_get(f"/space/{space_id}")
        if space_data:
            space_obj_full = space_data.get("space", space_data)
            statuses = extract_statuses_from_response(space_obj_full)

            return {
                "statuses": statuses,
                "status_count": len(statuses),
                "source": f"space_inherited_{space_id}",
                "space_id": space_id,
                "list_name": list_obj.get("name"),
            }

    # 5. Fallback - no statuses found
    return {
        "statuses": [],
        "status_count": 0,
        "source": "none_found",
        "list_name": list_obj.get("name"),
    }


def get_space_statuses(space_id: str) -> Dict[str, Any]:
    """
    Get statuses defined at the space level.

    Args:
        space_id: The ClickUp space ID

    Returns:
        Dictionary with statuses list and metadata
    """
    space_data, err = _api_get(f"/space/{space_id}")
    if err or not space_data:
        return {
            "statuses": [],
            "status_count": 0,
            "error": err or "Failed to fetch space",
        }

    space_obj = space_data.get("space", space_data)
    statuses = extract_statuses_from_response(space_obj)

    return {
        "statuses": statuses,
        "status_count": len(statuses),
        "space_name": space_obj.get("name"),
        "space_id": space_id,
    }


def format_status_for_display(status_dict: Dict) -> Dict[str, Any]:
    """
    Format a status dictionary for consistent display.
    Adds category based on status name and type.

    Args:
        status_dict: Raw status dictionary from API

    Returns:
        Formatted status dictionary with additional fields
    """
    status_name = status_dict.get("status", "")
    status_type = status_dict.get("type", "")

    return {
        "status": status_name,
        "type": status_type,
        "color": status_dict.get("color"),
        "orderindex": status_dict.get("orderindex"),
        "category": get_status_category(status_name, status_type),
    }


def get_status_category(status_name: str, status_type: str = None) -> str:
    """
    Determine the category of a status based on its name and type.

    Args:
        status_name: The status name (e.g., "BACKLOG", "In Review")
        status_type: The ClickUp status type (e.g., "open", "custom", "done")

    Returns:
        Category string: "not_started", "active", "done", "closed", or "other"
    """
    if not status_name:
        return "other"

    # 1. Check project-specific name overrides
    if cat := STATUS_OVERRIDE_MAP.get(status_name.upper()):
        return cat

    # 2. Fall back to ClickUp internal type
    if status_type:
        type_map = {
            "open": "not_started",
            "done": "done",
            "closed": "closed",
            "custom": "active",
        }
        return type_map.get(status_type.lower(), "other")

    return "other"


def get_all_list_statuses_in_folder(folder_id: str) -> Dict[str, Any]:
    """
    Get all statuses for all lists in a folder, handling inheritance.

    Args:
        folder_id: The ClickUp folder ID

    Returns:
        Dictionary with list statuses and metadata
    """
    folder_data, err = _api_get(f"/folder/{folder_id}")
    if err or not folder_data:
        return {"error": err or "Failed to fetch folder"}

    folder_obj = folder_data.get("folder", folder_data)

    # Get lists in the folder
    lists_data, err = _api_get(f"/folder/{folder_id}/list")
    if err or not lists_data:
        return {"error": err or "Failed to fetch lists"}

    lists = lists_data.get("lists", [])
    list_statuses = []

    for lst in lists:
        list_id = lst["id"]
        effective = get_effective_statuses(list_id)

        list_statuses.append(
            {
                "list_id": list_id,
                "list_name": lst.get("name"),
                "statuses": effective["statuses"],
                "status_count": effective["status_count"],
                "source": effective["source"],
            }
        )

    return {
        "folder_id": folder_id,
        "folder_name": folder_obj.get("name"),
        "lists": list_statuses,
    }


def normalize_status_name(status_name: str) -> str:
    """
    Normalize a status name for comparison.
    Converts to uppercase and strips whitespace.

    Args:
        status_name: Raw status name

    Returns:
        Normalized status name
    """
    return status_name.strip().upper() if status_name else ""


# --- Assignee Helper Functions ---


def get_workspace_members(workspace_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Get all members from a workspace/team.

    Args:
        workspace_id: The ClickUp workspace/team ID (optional, uses first team if not provided)

    Returns:
        Dictionary with members list and metadata
    """
    # Get workspace ID if not provided
    if not workspace_id:
        teams_data, err = _api_get("/team")
        if err or not teams_data:
            return {"error": err or "Failed to fetch teams", "members": []}

        teams = teams_data.get("teams", [])
        if not teams:
            return {"error": "No teams found", "members": []}

        workspace_id = teams[0]["id"]

    # Fetch team details with members
    team_data, err = _api_get(f"/team/{workspace_id}")
    if err or not team_data:
        return {"error": err or "Failed to fetch team", "members": []}

    team = team_data.get("team", team_data)
    members = team.get("members", [])

    formatted_members = []
    for member in members:
        user = member.get("user", {})
        formatted_members.append(
            {
                "id": user.get("id"),
                "username": user.get("username"),
                "email": user.get("email"),
                "color": user.get("color"),
                "initials": user.get("initials"),
                "role": member.get("role"),
            }
        )

    return {
        "workspace_id": workspace_id,
        "workspace_name": team.get("name"),
        "members": formatted_members,
        "member_count": len(formatted_members),
    }


def resolve_assignee_name_to_id(
    assignee_name: str, workspace_id: Optional[str] = None
) -> Optional[int]:
    """
    Resolve an assignee name/username to their user ID.

    Args:
        assignee_name: The username, email, or display name to search for
        workspace_id: Optional workspace ID to search in

    Returns:
        User ID as integer, or None if not found
    """
    members_data = get_workspace_members(workspace_id)

    if "error" in members_data:
        return None

    assignee_lower = assignee_name.lower().strip()

    for member in members_data.get("members", []):
        # Check username
        if member.get("username", "").lower() == assignee_lower:
            return int(member["id"])

        # Check email
        if member.get("email", "").lower() == assignee_lower:
            return int(member["id"])

    return None


def resolve_multiple_assignees(
    assignee_names: List[str], workspace_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Resolve multiple assignee names to IDs.

    Args:
        assignee_names: List of usernames/emails to resolve
        workspace_id: Optional workspace ID

    Returns:
        Dictionary with resolved IDs and any errors
    """
    members_data = get_workspace_members(workspace_id)

    if "error" in members_data:
        return {
            "error": members_data["error"],
            "resolved": [],
            "not_found": assignee_names,
        }

    resolved = []
    not_found = []

    for name in assignee_names:
        assignee_id = resolve_assignee_name_to_id(name, workspace_id)
        if assignee_id:
            resolved.append({"name": name, "id": assignee_id})
        else:
            not_found.append(name)

    return {
        "resolved": resolved,
        "resolved_ids": [r["id"] for r in resolved],
        "not_found": not_found,
        "success": len(not_found) == 0,
    }
