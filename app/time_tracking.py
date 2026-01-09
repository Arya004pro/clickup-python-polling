from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional

IST = ZoneInfo("Asia/Kolkata")


def _ms_to_ist(ms: int) -> datetime:
    """
    Convert epoch milliseconds → IST datetime
    """
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(IST)


def aggregate_time_entries(time_entries: List[Dict]) -> Dict[str, Optional[datetime]]:
    """
    Aggregate ClickUp interval-based time entries into:
    - start_time (IST)  → earliest interval start
    - end_time (IST)    → latest interval end (ignores running timers)
    - tracked_minutes  → sum of all interval durations

    Notes:
    - ClickUp intervals are authoritative (not task fields)
    - Safe for completed, in-progress, and paused tasks
    """

    if not time_entries:
        return {
            "start_time": None,
            "end_time": None,
            "tracked_minutes": 0,
        }

    start_times: List[int] = []
    end_times: List[int] = []
    total_ms: int = 0

    for entry in time_entries:
        for interval in entry.get("intervals", []):
            start_ms = interval.get("start")
            end_ms = interval.get("end")
            duration_ms = interval.get("time")

            if start_ms:
                start_times.append(int(start_ms))

            # end_ms may be None if timer is running
            if end_ms:
                end_times.append(int(end_ms))

            if duration_ms:
                total_ms += int(duration_ms)

    start_time = _ms_to_ist(min(start_times)) if start_times else None

    # If task is currently running, end_time remains last completed interval
    end_time = _ms_to_ist(max(end_times)) if end_times else None

    tracked_minutes = total_ms // 60000  # ms → minutes

    return {
        "start_time": start_time,
        "end_time": end_time,
        "tracked_minutes": tracked_minutes,
    }
