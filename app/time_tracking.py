from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import List, Dict

IST = ZoneInfo("Asia/Kolkata")


def _ms_to_ist(ms: int) -> datetime:
    """Convert epoch milliseconds → IST datetime"""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(IST)


def aggregate_time_entries(time_entries: List[Dict]) -> Dict:
    """
    Aggregate ClickUp interval-based time entries into:
    - start_times     → array of session start times (latest first)
    - end_times       → array of session end times (latest first)
    - tracked_minutes → sum of all interval durations
    """
    if not time_entries:
        return {"start_times": [], "end_times": [], "tracked_minutes": 0}

    intervals = []
    total_ms = 0

    for entry in time_entries:
        for interval in entry.get("intervals", []):
            start_ms = interval.get("start")
            end_ms = interval.get("end")
            duration_ms = interval.get("time")

            if start_ms:
                intervals.append(
                    {"start": int(start_ms), "end": int(end_ms) if end_ms else None}
                )
            if duration_ms:
                total_ms += int(duration_ms)

    if not intervals:
        return {"start_times": [], "end_times": [], "tracked_minutes": 0}

    # Sort by start time descending (latest first)
    intervals.sort(key=lambda x: x["start"], reverse=True)

    start_times = [_ms_to_ist(i["start"]).isoformat() for i in intervals]
    end_times = [
        _ms_to_ist(i["end"]).isoformat() if i["end"] else None for i in intervals
    ]

    return {
        "start_times": start_times,
        "end_times": end_times,
        "tracked_minutes": total_ms // 60000,
    }
