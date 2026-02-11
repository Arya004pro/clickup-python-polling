"""
Status Helper Functions for ClickUp MCP Server
Provides unified status handling across all MCP tools.
Handles API response polymorphism and status inheritance.
"""

import requests
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime, timezone, timedelta
from app.config import CLICKUP_API_TOKEN, BASE_URL


# --- Date/Timestamp Helpers ---


def date_to_timestamp_ms(date_str: str) -> int:
    """
    Convert YYYY-MM-DD date string to Unix timestamp in milliseconds.
    Time is set to 00:00:00 UTC.

    Args:
        date_str: Date in YYYY-MM-DD format

    Returns:
        Unix timestamp in milliseconds

    Example:
        date_to_timestamp_ms("2024-01-15") -> 1705276800000
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def date_range_to_timestamps(start_date: str, end_date: str) -> tuple[int, int]:
    """
    Convert date range to timestamp range in milliseconds.
    Start time is 00:00:00, end time is 23:59:59.999 of the end date.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format

    Returns:
        Tuple of (start_timestamp_ms, end_timestamp_ms)

    Example:
        date_range_to_timestamps("2024-01-15", "2024-01-21")
        -> (1705276800000, 1705881599999)
    """
    start_ms = date_to_timestamp_ms(start_date)
    # Add 1 day minus 1 millisecond to include the entire end date
    end_ms = date_to_timestamp_ms(end_date) + (86400000 - 1)
    return start_ms, end_ms


def get_current_week_dates() -> tuple[str, str]:
    """
    Get current week's Monday and Sunday dates.

    Returns:
        Tuple of (monday_date, sunday_date) in YYYY-MM-DD format

    Example:
        get_current_week_dates() -> ("2024-01-15", "2024-01-21")
    """
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")


def get_previous_week_dates() -> Tuple[str, str]:
    """
    Get previous week's Monday and Sunday dates.

    Returns:
        Tuple of (monday_date, sunday_date) in YYYY-MM-DD format

    Example:
        get_previous_week_dates() -> ("2024-01-08", "2024-01-14")
    """
    today = datetime.now()
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday.strftime("%Y-%m-%d"), last_sunday.strftime("%Y-%m-%d")


def get_week_dates_from_offset(weeks_ago: int = 0) -> Tuple[str, str]:
    """
    Get week dates by offset from current week.

    Args:
        weeks_ago: Number of weeks before current week (0 = current, 1 = last week, etc.)

    Returns:
        Tuple of (monday_date, sunday_date) in YYYY-MM-DD format

    Examples:
        get_week_dates_from_offset(0) -> Current week ("2024-01-15", "2024-01-21")
        get_week_dates_from_offset(1) -> Last week ("2024-01-08", "2024-01-14")
        get_week_dates_from_offset(2) -> 2 weeks ago ("2024-01-01", "2024-01-07")
    """
    today = datetime.now()
    target_week_monday = today - timedelta(days=today.weekday() + (weeks_ago * 7))
    target_week_sunday = target_week_monday + timedelta(days=6)
    return (
        target_week_monday.strftime("%Y-%m-%d"),
        target_week_sunday.strftime("%Y-%m-%d"),
    )


def get_week_dates_from_date(date_str: str) -> Tuple[str, str]:
    """
    Get Monday-Sunday for the week containing the given date.

    Args:
        date_str: Date in YYYY-MM-DD format

    Returns:
        Tuple of (monday_date, sunday_date) in YYYY-MM-DD format

    Example:
        get_week_dates_from_date("2026-01-15") -> ("2026-01-12", "2026-01-18")
    """
    target_date = datetime.strptime(date_str, "%Y-%m-%d")
    monday = target_date - timedelta(days=target_date.weekday())
    sunday = monday + timedelta(days=6)
    return monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")


def get_week_dates_from_week_number(year: int, week_number: int) -> Tuple[str, str]:
    """
    Get Monday-Sunday for a specific ISO week number.

    Args:
        year: Year (e.g., 2026)
        week_number: ISO week number (1-53)

    Returns:
        Tuple of (monday_date, sunday_date) in YYYY-MM-DD format

    Example:
        get_week_dates_from_week_number(2026, 3) -> ("2026-01-12", "2026-01-18")

    Note:
        ISO week 1 is the week with the first Thursday of the year.
    """
    # ISO week 1 is the week with the first Thursday of the year
    jan_4 = datetime(year, 1, 4)
    week_1_monday = jan_4 - timedelta(days=jan_4.weekday())
    target_monday = week_1_monday + timedelta(weeks=week_number - 1)
    target_sunday = target_monday + timedelta(days=6)
    return target_monday.strftime("%Y-%m-%d"), target_sunday.strftime("%Y-%m-%d")


def parse_week_input(week_input: str) -> Tuple[str, str]:
    """
    Smart parser for various week input formats.

    Supported formats:
    - "current", "this" → Current week
    - "previous", "last" → Previous week
    - "N-weeks-ago" → N weeks before current (e.g., "2-weeks-ago")
    - "YYYY-MM-DD" → Week containing that date
    - "YYYY-WNN" → ISO week number (e.g., "2026-W03")

    Args:
        week_input: Week specification string

    Returns:
        Tuple of (monday_date, sunday_date) in YYYY-MM-DD format

    Raises:
        ValueError: If format is not recognized

    Examples:
        parse_week_input("current") -> ("2026-02-10", "2026-02-16")
        parse_week_input("previous") -> ("2026-02-03", "2026-02-09")
        parse_week_input("2-weeks-ago") -> ("2026-01-27", "2026-02-02")
        parse_week_input("2026-01-15") -> ("2026-01-12", "2026-01-18")
        parse_week_input("2026-W03") -> ("2026-01-12", "2026-01-18")
    """
    week_input = week_input.lower().strip()

    # Current week
    if week_input in ["current", "this", "this week"]:
        return get_current_week_dates()

    # Previous week
    if week_input in ["previous", "last", "last week", "previous week"]:
        return get_previous_week_dates()

    # N weeks ago format: "2-weeks-ago", "3-weeks-ago"
    if week_input.endswith("-weeks-ago") or week_input.endswith("-week-ago"):
        try:
            weeks_ago = int(week_input.split("-")[0])
            return get_week_dates_from_offset(weeks_ago)
        except (ValueError, IndexError):
            raise ValueError(f"Invalid weeks-ago format: {week_input}")

    # ISO week format: "2026-W03", "2026-w03"
    if "-w" in week_input or "-W" in week_input:
        try:
            year_str, week_str = week_input.upper().split("-W")
            year = int(year_str)
            week_num = int(week_str)
            return get_week_dates_from_week_number(year, week_num)
        except (ValueError, IndexError):
            raise ValueError(
                f"Invalid ISO week format: {week_input}. Expected: YYYY-WNN"
            )

    # Specific date format: "2026-01-15"
    if len(week_input) == 10 and week_input.count("-") == 2:
        try:
            datetime.strptime(week_input, "%Y-%m-%d")
            return get_week_dates_from_date(week_input)
        except ValueError:
            raise ValueError(f"Invalid date format: {week_input}. Expected: YYYY-MM-DD")

    raise ValueError(
        f"Unrecognized week format: {week_input}. "
        f"Supported: 'current', 'previous', 'N-weeks-ago', 'YYYY-MM-DD', 'YYYY-WNN'"
    )


def validate_week_dates(week_start: str, week_end: str) -> bool:
    """
    Validate that week_start is Monday and week_end is Sunday, exactly 6 days apart.

    Args:
        week_start: Start date in YYYY-MM-DD format
        week_end: End date in YYYY-MM-DD format

    Returns:
        True if valid

    Raises:
        ValueError: If validation fails

    Example:
        validate_week_dates("2026-01-12", "2026-01-18") -> True
        validate_week_dates("2026-01-13", "2026-01-19") -> ValueError (not Monday-Sunday)
    """
    start_date = datetime.strptime(week_start, "%Y-%m-%d")
    end_date = datetime.strptime(week_end, "%Y-%m-%d")

    if start_date.weekday() != 0:  # Monday = 0
        raise ValueError(
            f"week_start must be a Monday, got {start_date.strftime('%A')}"
        )

    if end_date.weekday() != 6:  # Sunday = 6
        raise ValueError(f"week_end must be a Sunday, got {end_date.strftime('%A')}")

    if (end_date - start_date).days != 6:
        raise ValueError(
            f"week_end must be 6 days after week_start, got {(end_date - start_date).days} days"
        )

    return True


def filter_time_entries_by_date_range(
    time_entries: List[Dict], start_ms: int, end_ms: int
) -> tuple[int, List[Dict]]:
    """
    Filter time entry intervals by date range and calculate total time.

    Args:
        time_entries: List of time entry objects with 'intervals' field
        start_ms: Start timestamp in milliseconds (inclusive)
        end_ms: End timestamp in milliseconds (inclusive)

    Returns:
        Tuple of (total_time_ms, filtered_intervals)

    Example:
        entries = [{
            "intervals": [
                {"start": 1705276800000, "end": 1705280400000, "time": 3600000}
            ]
        }]
        filter_time_entries_by_date_range(entries, start_ms, end_ms)
        -> (3600000, [{...}])
    """
    total_ms = 0
    filtered_intervals = []

    for entry in time_entries:
        for interval in entry.get("intervals", []):
            interval_start = interval.get("start")
            duration = interval.get("time")

            if not interval_start:
                continue

            # Convert to int (API may return as string)
            try:
                interval_start = int(interval_start)
            except (ValueError, TypeError):
                continue

            # Check if interval overlaps with date range
            # Interval is included if it started within the range
            if start_ms <= interval_start <= end_ms:
                if duration:
                    total_ms += int(duration)
                filtered_intervals.append(interval)

    return total_ms, filtered_intervals


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
