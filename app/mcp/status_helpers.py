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


def get_day_of_week_name(date_str: str) -> str:
    """Get the day of week name for a given date."""
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    return date_obj.strftime("%A")


def is_valid_monday_sunday_range(week_start: str, week_end: str) -> Tuple[bool, str]:
    """Check if a date range is a valid Monday-Sunday week."""
    try:
        start_date = datetime.strptime(week_start, "%Y-%m-%d")
        end_date = datetime.strptime(week_end, "%Y-%m-%d")

        if start_date.weekday() != 0:
            return False, f"week_start must be Monday, got {start_date.strftime('%A')}"

        if end_date.weekday() != 6:
            return False, f"week_end must be Sunday, got {end_date.strftime('%A')}"

        if (end_date - start_date).days != 6:
            return False, f"Invalid range: {(end_date - start_date).days} days apart"

        return True, ""
    except ValueError as e:
        return False, f"Invalid date format: {str(e)}"


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
    # Use UTC to match date_to_timestamp_ms behavior
    today = datetime.now(timezone.utc)  # ✓ Now uses UTC
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
    # Use UTC to match date_to_timestamp_ms behavior
    today = datetime.now(timezone.utc)  # ✓ Now uses UTC
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
    # Use UTC to match date_to_timestamp_ms behavior
    today = datetime.now(timezone.utc)  # ✓ Now uses UTC
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


def parse_week_input(
    week_input: str, allow_multi_week: bool = False
) -> Tuple[str, str]:
    """
    Smart parser for various week input formats.

    Supported formats:
    - "current", "this" → Current week
    - "previous", "last" → Previous week
    - "N-weeks-ago" → N weeks before current (e.g., "2-weeks-ago")
    - "YYYY-MM-DD" → Week containing that date
    - "YYYY-WNN" → ISO week number (e.g., "2026-W03")
    - "N-weeks" → N weeks starting current Monday (if allow_multi_week=True)
    - "last-N-weeks" → Last N weeks ending last Sunday (if allow_multi_week=True)
    - "month" → 4 weeks (if allow_multi_week=True)

    Args:
        week_input: Week specification string
        allow_multi_week: If True, allows multi-week range inputs

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
        parse_week_input("3-weeks", allow_multi_week=True) -> 3 weeks from Monday
        parse_week_input("month", allow_multi_week=True) -> 4 weeks from Monday
    """
    week_input = week_input.lower().strip()

    # Try multi-week formats first if enabled
    if allow_multi_week:
        # Check for multi-week patterns
        multi_week_patterns = [
            "month",
            "last-month",
            "this-month",
            "previous-month",
            "-weeks",
            "last-",
            "-weeks-current",
            "-weeks-previous",
        ]
        if any(pattern in week_input for pattern in multi_week_patterns):
            try:
                return parse_multi_week_input(week_input)
            except ValueError:
                pass  # Fall through to single week parsing

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
            monday, sunday = get_week_dates_from_date(week_input)
            print(f"[DEBUG] Parsed '{week_input}' to week: {monday} - {sunday}")
            import sys

            sys.stdout.flush()
            return monday, sunday
        except ValueError:
            raise ValueError(f"Invalid date format: {week_input}. Expected: YYYY-MM-DD")

    error_msg = (
        f"Unrecognized week format: {week_input}. "
        f"Supported: 'current', 'previous', 'N-weeks-ago', 'YYYY-MM-DD', 'YYYY-WNN'"
    )
    if allow_multi_week:
        error_msg += ", 'N-weeks', 'last-N-weeks', 'month', 'last-month'"

    raise ValueError(error_msg)


def validate_week_dates(
    week_start: str, week_end: str, allow_multi_week: bool = False
) -> bool:
    """
    Validate week date range.

    Args:
        week_start: Start date in YYYY-MM-DD format
        week_end: End date in YYYY-MM-DD format
        allow_multi_week: If True, allows ranges spanning multiple weeks (must still be Monday-Sunday)

    Returns:
        True if valid

    Raises:
        ValueError: If dates are invalid
    """
    start_date = datetime.strptime(week_start, "%Y-%m-%d")
    end_date = datetime.strptime(week_end, "%Y-%m-%d")

    # Get day names for better error messages
    start_day = start_date.strftime("%A")
    end_day = end_date.strftime("%A")

    if start_date.weekday() != 0:
        raise ValueError(
            f"week_start must be a Monday, got {start_day}. Date provided: {week_start}"
        )

    if end_date.weekday() != 6:
        raise ValueError(
            f"week_end must be a Sunday, got {end_day}. Date provided: {week_end}"
        )

    days_diff = (end_date - start_date).days

    if allow_multi_week:
        # For multi-week, check that it's a multiple of 7 days (full weeks)
        if (days_diff + 1) % 7 != 0:
            raise ValueError(
                f"Multi-week range must span complete weeks (multiples of 7 days), got {days_diff + 1} days"
            )
        if days_diff < 6:
            raise ValueError(f"Date range too short, got {days_diff + 1} days")
    else:
        # Single week validation
        if days_diff != 6:
            raise ValueError(
                f"week_end must be 6 days after week_start, got {days_diff} days"
            )

    return True


def get_multi_week_range(
    num_weeks: int, week_selector: str = "current"
) -> Tuple[str, str]:
    """
    Get a date range spanning multiple weeks.

    Args:
        num_weeks: Number of weeks to include (1-8)
        week_selector: Starting point - "current", "previous", or "N-weeks-ago"

    Returns:
        Tuple of (start_monday, end_sunday) spanning num_weeks

    Examples:
        get_multi_week_range(2, "current") -> Current week + next week
        get_multi_week_range(3, "previous") -> Last 3 weeks
        get_multi_week_range(4, "current") -> Current + next 3 weeks (month)
    """
    if num_weeks < 1 or num_weeks > 8:
        raise ValueError("num_weeks must be between 1 and 8")

    # Get the base week
    if week_selector in ["current", "this"]:
        start_monday, _ = get_current_week_dates()
    elif week_selector in ["previous", "last"]:
        start_monday, _ = get_previous_week_dates()
    elif week_selector.endswith("-weeks-ago") or week_selector.endswith("-week-ago"):
        weeks_ago = int(week_selector.split("-")[0])
        start_monday, _ = get_week_dates_from_offset(weeks_ago)
    else:
        # Try to parse as date
        start_monday, _ = parse_week_input(week_selector)

    # Calculate end date by adding (num_weeks - 1) * 7 days, then adding 6 more for Sunday
    start_date = datetime.strptime(start_monday, "%Y-%m-%d")
    end_date = start_date + timedelta(days=(num_weeks * 7) - 1)

    return start_monday, end_date.strftime("%Y-%m-%d")


def parse_multi_week_input(week_input: str) -> Tuple[str, str]:
    """
    Parse multi-week input formats.

    Supported formats:
    - "2-weeks" or "2-weeks-current" → Current week + next week (2 weeks total)
    - "3-weeks-previous" → Last 3 weeks ending last Sunday
    - "4-weeks" or "month" → 4 weeks starting from current week
    - "last-2-weeks" → Last 2 weeks ending last Sunday
    - "last-month" → Last 4 weeks ending last Sunday

    Args:
        week_input: Multi-week specification string

    Returns:
        Tuple of (monday_start, sunday_end) spanning the period

    Examples:
        parse_multi_week_input("2-weeks") -> 2 weeks starting this Monday
        parse_multi_week_input("last-2-weeks") -> Previous 2 weeks ending last Sunday
        parse_multi_week_input("month") -> 4 weeks starting this Monday
    """
    week_input = week_input.lower().strip()

    # "month" or "4-weeks" starting current week
    if week_input in ["month", "this-month"]:
        return get_multi_week_range(4, "current")

    # "last-month" - 4 weeks ending last Sunday
    if week_input in ["last-month", "previous-month"]:
        # Get 4 weeks starting from 3 weeks ago to include last week
        start_monday, _ = get_week_dates_from_offset(3)
        end_monday, end_sunday = get_previous_week_dates()
        return start_monday, end_sunday

    # "N-weeks" format - N weeks starting current Monday
    if week_input.endswith("-weeks") or week_input.endswith("-week"):
        parts = week_input.split("-")
        try:
            num_weeks = int(parts[0])
            return get_multi_week_range(num_weeks, "current")
        except (ValueError, IndexError):
            raise ValueError(f"Invalid multi-week format: {week_input}")

    # "last-N-weeks" format
    if week_input.startswith("last-"):
        parts = week_input[5:].split("-")  # Remove "last-" prefix
        try:
            num_weeks = int(parts[0])
            # Start from (num_weeks - 1) weeks ago and end last Sunday
            start_monday, _ = get_week_dates_from_offset(num_weeks - 1)
            _, end_sunday = get_previous_week_dates()
            return start_monday, end_sunday
        except (ValueError, IndexError):
            raise ValueError(f"Invalid last-N-weeks format: {week_input}")

    # "N-weeks-current" or "N-weeks-previous"
    if "-weeks-" in week_input:
        parts = week_input.split("-")
        try:
            num_weeks = int(parts[0])
            base = parts[2] if len(parts) > 2 else "current"
            return get_multi_week_range(num_weeks, base)
        except (ValueError, IndexError):
            raise ValueError(f"Invalid multi-week format: {week_input}")

    raise ValueError(
        f"Unrecognized multi-week format: {week_input}. "
        f"Supported: 'N-weeks', 'last-N-weeks', 'month', 'last-month'"
    )


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
            interval_end = interval.get("end")
            duration = interval.get("time")

            if not interval_start:
                continue

            # Convert to int (API may return as string)
            try:
                interval_start = int(interval_start)
            except (ValueError, TypeError):
                continue

            # Attempt to obtain interval_end. If missing, try to infer from 'time'.
            try:
                interval_end = int(interval_end) if interval_end is not None else None
            except (ValueError, TypeError):
                interval_end = None

            if interval_end is None and duration:
                try:
                    interval_end = interval_start + int(duration)
                except (ValueError, TypeError):
                    interval_end = None

            # If still missing an end, treat as instantaneous (skip)
            if interval_end is None:
                continue

            # Compute overlap between [interval_start, interval_end] and [start_ms, end_ms]
            overlap_start = max(start_ms, interval_start)
            overlap_end = min(end_ms, interval_end)
            overlap = max(0, overlap_end - overlap_start)

            if overlap > 0:
                total_ms += int(overlap)
                # record the original interval but annotate the effective overlap
                annotated = dict(interval)
                annotated["overlap_time"] = int(overlap)
                filtered_intervals.append(annotated)

    return total_ms, filtered_intervals


def filter_time_entries_by_user_and_date_range(
    time_entries: List[Dict], start_ms: int, end_ms: int
) -> Dict[str, int]:
    """
    Filter time entries by date range and calculate time tracked per user.

    Args:
        time_entries: List of time entry objects with 'user' and 'intervals' fields
        start_ms: Start timestamp in milliseconds (inclusive)
        end_ms: End timestamp in milliseconds (inclusive)

    Returns:
        Dict mapping username to total time tracked (in milliseconds)

    Example:
        entries = [{
            "user": {"username": "John"},
            "intervals": [{"start": 1705276800000, "end": 1705280400000, "time": 3600000}]
        }]
        filter_time_entries_by_user_and_date_range(entries, start_ms, end_ms)
        -> {"John": 3600000}
    """
    user_time_map = {}

    for entry in time_entries:
        # Get the user who logged this time entry
        user = entry.get("user", {})
        username = user.get("username", "Unassigned")

        for interval in entry.get("intervals", []):
            interval_start = interval.get("start")
            interval_end = interval.get("end")
            duration = interval.get("time")

            if not interval_start:
                continue

            # Convert to int (API may return as string)
            try:
                interval_start = int(interval_start)
            except (ValueError, TypeError):
                continue

            # Attempt to obtain interval_end. If missing, try to infer from 'time'.
            try:
                interval_end = int(interval_end) if interval_end is not None else None
            except (ValueError, TypeError):
                interval_end = None

            if interval_end is None and duration:
                try:
                    interval_end = interval_start + int(duration)
                except (ValueError, TypeError):
                    interval_end = None

            # If still missing an end, treat as instantaneous (skip)
            if interval_end is None:
                continue

            # Compute overlap between [interval_start, interval_end] and [start_ms, end_ms]
            overlap_start = max(start_ms, interval_start)
            overlap_end = min(end_ms, interval_end)
            overlap = max(0, overlap_end - overlap_start)

            if overlap > 0:
                if username not in user_time_map:
                    user_time_map[username] = 0
                user_time_map[username] += int(overlap)

    return user_time_map


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


# --- Extended Date Filter Functions ---


def get_today_dates() -> Tuple[str, str]:
    """
    Get today's date as both start and end.

    Returns:
        Tuple of (today, today) in YYYY-MM-DD format

    Example:
        get_today_dates() -> ("2026-02-13", "2026-02-13")
    """
    today = datetime.now(timezone.utc)
    today_str = today.strftime("%Y-%m-%d")
    return today_str, today_str


def get_yesterday_dates() -> Tuple[str, str]:
    """
    Get yesterday's date as both start and end.

    Returns:
        Tuple of (yesterday, yesterday) in YYYY-MM-DD format

    Example:
        get_yesterday_dates() -> ("2026-02-12", "2026-02-12")
    """
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    yesterday_str = yesterday.strftime("%Y-%m-%d")
    return yesterday_str, yesterday_str


def get_this_month_dates() -> Tuple[str, str]:
    """
    Get the full current month's date range (1st to last day of month).

    Returns:
        Tuple of (first_day, last_day) in YYYY-MM-DD format

    Example:
        get_this_month_dates() -> ("2026-02-01", "2026-02-28")
    """
    today = datetime.now(timezone.utc)
    first_day = today.replace(day=1)

    # Get last day of month
    if today.month == 12:
        last_day = today.replace(day=31)
    else:
        next_month = today.replace(month=today.month + 1, day=1)
        last_day = next_month - timedelta(days=1)

    return first_day.strftime("%Y-%m-%d"), last_day.strftime("%Y-%m-%d")


def get_last_month_dates() -> Tuple[str, str]:
    """
    Get the full previous month's date range (1st to last day of previous month).

    Returns:
        Tuple of (first_day, last_day) in YYYY-MM-DD format

    Example:
        get_last_month_dates() -> ("2026-01-01", "2026-01-31")
    """
    today = datetime.now(timezone.utc)
    # Get first day of current month, then go back one day
    first_of_current_month = today.replace(day=1)
    last_of_previous_month = first_of_current_month - timedelta(days=1)
    first_of_previous_month = last_of_previous_month.replace(day=1)

    return (
        first_of_previous_month.strftime("%Y-%m-%d"),
        last_of_previous_month.strftime("%Y-%m-%d"),
    )


def get_this_year_dates() -> Tuple[str, str]:
    """
    Get the full current year's date range (Jan 1 to Dec 31).

    Returns:
        Tuple of (jan_1, dec_31) in YYYY-MM-DD format

    Example:
        get_this_year_dates() -> ("2026-01-01", "2026-12-31")
    """
    today = datetime.now(timezone.utc)
    first_day = today.replace(month=1, day=1)
    last_day = today.replace(month=12, day=31)

    return first_day.strftime("%Y-%m-%d"), last_day.strftime("%Y-%m-%d")


def get_last_n_days_dates(n_days: int) -> Tuple[str, str]:
    """
    Get date range for the last N days (including today).

    Args:
        n_days: Number of days to go back (e.g., 30 for last 30 days)

    Returns:
        Tuple of (start_date, end_date) in YYYY-MM-DD format

    Example:
        get_last_n_days_dates(30) -> ("2026-01-14", "2026-02-13")
    """
    today = datetime.now(timezone.utc)
    start_date = today - timedelta(days=n_days - 1)

    return start_date.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def parse_time_period_filter(
    period_type: str,
    custom_start: Optional[str] = None,
    custom_end: Optional[str] = None,
    rolling_days: Optional[int] = None,
) -> Tuple[str, str]:
    """
    Universal date range parser for all time reporting filters.

    Supported period types:
    - "today": Today's date
    - "yesterday": Yesterday's date
    - "this_week": Current week (Monday-Sunday)
    - "last_week": Previous week (Monday-Sunday)
    - "this_month": Current month (1st to last day)
    - "last_month": Previous month (1st to last day)
    - "this_year": Current year (Jan 1 to Dec 31)
    - "last_30_days": Last 30 days including today
    - "rolling": Rolling period (requires rolling_days parameter)
    - "custom": Custom date range (requires custom_start and custom_end)

    Args:
        period_type: Type of period filter (see supported types above)
        custom_start: Start date for custom range (YYYY-MM-DD format)
        custom_end: End date for custom range (YYYY-MM-DD format)
        rolling_days: Number of days for rolling period

    Returns:
        Tuple of (start_date, end_date) in YYYY-MM-DD format

    Raises:
        ValueError: If invalid period type or missing required parameters

    Examples:
        parse_time_period_filter("today") -> ("2026-02-13", "2026-02-13")
        parse_time_period_filter("this_week") -> ("2026-02-10", "2026-02-16")
        parse_time_period_filter("last_30_days") -> ("2026-01-14", "2026-02-13")
        parse_time_period_filter("rolling", rolling_days=7) -> Last 7 days
        parse_time_period_filter("custom", custom_start="2026-01-01", custom_end="2026-01-31")
    """
    period_type = period_type.lower().strip()

    if period_type == "today":
        return get_today_dates()

    elif period_type == "yesterday":
        return get_yesterday_dates()

    elif period_type in ["this_week", "current_week"]:
        return get_current_week_dates()

    elif period_type in ["last_week", "previous_week"]:
        return get_previous_week_dates()

    elif period_type in ["this_month", "current_month"]:
        return get_this_month_dates()

    elif period_type in ["last_month", "previous_month"]:
        return get_last_month_dates()

    elif period_type in ["this_year", "current_year"]:
        return get_this_year_dates()

    elif period_type == "last_30_days":
        return get_last_n_days_dates(30)

    elif period_type == "rolling":
        if rolling_days is None:
            raise ValueError(
                "rolling_days parameter is required for rolling period type"
            )
        if rolling_days < 1 or rolling_days > 365:
            raise ValueError("rolling_days must be between 1 and 365")
        return get_last_n_days_dates(rolling_days)

    elif period_type == "custom":
        if not custom_start or not custom_end:
            raise ValueError(
                "custom_start and custom_end are required for custom period type"
            )

        # Validate date formats
        try:
            datetime.strptime(custom_start, "%Y-%m-%d")
            datetime.strptime(custom_end, "%Y-%m-%d")
        except ValueError as e:
            raise ValueError(f"Invalid date format. Expected YYYY-MM-DD: {str(e)}")

        # Validate that end is not before start
        start_dt = datetime.strptime(custom_start, "%Y-%m-%d")
        end_dt = datetime.strptime(custom_end, "%Y-%m-%d")
        if end_dt < start_dt:
            raise ValueError("custom_end cannot be before custom_start")

        return custom_start, custom_end

    else:
        raise ValueError(
            f"Unsupported period type: {period_type}. "
            f"Supported: today, yesterday, this_week, last_week, this_month, "
            f"last_month, this_year, last_30_days, rolling, custom"
        )
