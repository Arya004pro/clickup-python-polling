"""
Timestamp and Time Entry Formatting Helpers
Add these functions to app/mcp/status_helpers.py
"""

from datetime import datetime, timezone, timedelta
from typing import Dict


def timestamp_ms_to_human(timestamp_ms: int, timezone_offset: str = "+05:30") -> Dict:
    """
    Convert Unix timestamp in milliseconds to human-readable format.

    Args:
        timestamp_ms: Unix timestamp in milliseconds
        timezone_offset: Timezone offset (default IST +05:30)

    Returns:
        dict with formatted date/time strings

    Example:
        timestamp_ms_to_human(1738410840000)
        -> {
            "date": "2026-02-02",
            "day_of_week": "Monday",
            "time": "3:44 PM",
            "datetime": "Mon, Feb 2, 3:44 PM IST",
            "iso": "2026-02-02T15:44:00+05:30",
            "timestamp_ms": 1738410840000
        }
    """
    # Parse timezone offset
    sign = 1 if timezone_offset.startswith("+") else -1
    offset_str = timezone_offset[1:]  # Remove the +/- sign

    if ":" in offset_str:
        hours, minutes = map(int, offset_str.split(":"))
    else:
        # Handle formats like "+0530" without colon
        hours = int(offset_str[:2])
        minutes = int(offset_str[2:]) if len(offset_str) > 2 else 0

    tz_delta = timedelta(hours=sign * hours, minutes=sign * minutes)
    tz = timezone(tz_delta)

    # Convert timestamp to datetime
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=tz)

    return {
        "date": dt.strftime("%Y-%m-%d"),
        "day_of_week": dt.strftime("%A"),
        "time": dt.strftime("%I:%M %p").lstrip("0"),  # Remove leading zero from hour
        "datetime": dt.strftime("%a, %b %d, %I:%M %p IST").replace(" 0", " "),
        "iso": dt.isoformat(),
        "timestamp_ms": timestamp_ms,
    }


def format_duration_simple(milliseconds: int) -> str:
    """
    Format duration in milliseconds to 'Xh Ym' format.

    Args:
        milliseconds: Duration in milliseconds

    Returns:
        Formatted string like "3h 4m" or "45m" or "0m"

    Example:
        format_duration_simple(11040000) -> "3h 4m"
        format_duration_simple(2700000) -> "45m"
        format_duration_simple(0) -> "0m"
    """
    if milliseconds == 0:
        return "0m"

    total_minutes = milliseconds // 60000
    hours = total_minutes // 60
    minutes = total_minutes % 60

    if hours > 0 and minutes > 0:
        return f"{hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h 0m"
    else:
        return f"{minutes}m"


def format_time_entry_interval(interval: Dict, timezone_offset: str = "+05:30") -> Dict:
    """
    Format a time entry interval with human-readable timestamps.

    Args:
        interval: Raw interval dict with 'start', 'end', 'time' fields
        timezone_offset: Timezone offset (default IST +05:30)

    Returns:
        Formatted interval with readable dates/times

    Example:
        interval = {
            "id": "interval_123",
            "start": 1738410840000,
            "end": 1738421880000,
            "time": 11040000
        }
        format_time_entry_interval(interval)
        -> {
            "interval_id": "interval_123",
            "date": "2026-02-02",
            "day_of_week": "Monday",
            "start_time": "Mon, Feb 2, 3:44 PM IST",
            "end_time": "Mon, Feb 2, 6:48 PM IST",
            "duration_ms": 11040000,
            "duration": "3h 4m"
        }
    """
    start_ms = interval.get("start")
    if not start_ms:
        return {
            "error": "No start timestamp",
            "duration_ms": interval.get("time", 0),
            "duration": format_duration_simple(interval.get("time", 0)),
        }

    try:
        start_ms = int(start_ms)
    except (ValueError, TypeError):
        return {
            "error": "Invalid start timestamp",
            "duration_ms": interval.get("time", 0),
            "duration": format_duration_simple(interval.get("time", 0)),
        }

    start_info = timestamp_ms_to_human(start_ms, timezone_offset)
    duration_ms = interval.get("time", 0)

    # Calculate end time if available
    end_ms = interval.get("end")
    if end_ms:
        try:
            end_ms = int(end_ms)
            end_info = timestamp_ms_to_human(end_ms, timezone_offset)
            end_time = end_info["datetime"]
        except (ValueError, TypeError):
            end_time = "Unknown"
    else:
        end_time = "Ongoing"

    # Format duration
    duration_human = format_duration_simple(duration_ms)

    return {
        "interval_id": interval.get("id"),
        "date": start_info["date"],
        "day_of_week": start_info["day_of_week"],
        "start_time": start_info["datetime"],
        "end_time": end_time,
        "duration_ms": duration_ms,
        "duration": duration_human,
    }


def group_intervals_by_date(intervals: list) -> Dict:
    """
    Group time entry intervals by date for easier daily summaries.

    Args:
        intervals: List of formatted intervals (from format_time_entry_interval)

    Returns:
        Dict with dates as keys and intervals as values

    Example:
        intervals = [
            {"date": "2026-02-02", "duration": "3h 4m", ...},
            {"date": "2026-02-02", "duration": "1h 30m", ...},
            {"date": "2026-02-03", "duration": "2h 15m", ...}
        ]
        group_intervals_by_date(intervals)
        -> {
            "2026-02-02": {
                "day_of_week": "Monday",
                "intervals": [...2 intervals...],
                "total_duration_ms": 16440000,
                "total_duration": "4h 34m"
            },
            "2026-02-03": {
                "day_of_week": "Tuesday",
                "intervals": [...1 interval...],
                "total_duration_ms": 8100000,
                "total_duration": "2h 15m"
            }
        }
    """
    grouped = {}

    for interval in intervals:
        date = interval.get("date")
        if not date:
            continue

        if date not in grouped:
            grouped[date] = {
                "day_of_week": interval.get("day_of_week", "Unknown"),
                "intervals": [],
                "total_duration_ms": 0,
            }

        grouped[date]["intervals"].append(interval)
        grouped[date]["total_duration_ms"] += interval.get("duration_ms", 0)

    # Add human-readable totals
    for date, data in grouped.items():
        data["total_duration"] = format_duration_simple(data["total_duration_ms"])
        data["interval_count"] = len(data["intervals"])

    return grouped
