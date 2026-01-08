from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def aggregate_time_entries(time_entries: list):
    """
    Aggregate ClickUp time entries (interval-based) into:
    - start_time (IST)
    - end_time (IST)
    - tracked_minutes
    """

    if not time_entries:
        return {
            "start_time": None,
            "end_time": None,
            "tracked_minutes": 0,
        }

    start_times_ms = []
    end_times_ms = []
    total_ms = 0

    for entry in time_entries:
        intervals = entry.get("intervals", [])

        for interval in intervals:
            start_ms = interval.get("start")
            end_ms = interval.get("end")
            duration_ms = interval.get("time")

            if start_ms is not None:
                start_times_ms.append(int(start_ms))

            if end_ms is not None:
                end_times_ms.append(int(end_ms))

            if duration_ms is not None:
                total_ms += int(duration_ms)

    # Convert earliest start and latest end to IST
    start_time = (
        datetime.fromtimestamp(min(start_times_ms) / 1000, tz=timezone.utc).astimezone(
            IST
        )
        if start_times_ms
        else None
    )

    end_time = (
        datetime.fromtimestamp(max(end_times_ms) / 1000, tz=timezone.utc).astimezone(
            IST
        )
        if end_times_ms
        else None
    )

    tracked_minutes = total_ms // (1000 * 60)

    return {
        "start_time": start_time,
        "end_time": end_time,
        "tracked_minutes": tracked_minutes,
    }
