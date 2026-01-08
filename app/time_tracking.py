from datetime import datetime, timezone


def aggregate_time_entries(time_entries: list):
    """
    Aggregate ClickUp time entries into:
    - start_time
    - end_time
    - tracked_minutes
    """

    if not time_entries:
        return {"start_time": None, "end_time": None, "tracked_minutes": 0}

    start_times = []
    end_times = []
    total_ms = 0

    for entry in time_entries:
        start_ms = entry.get("start")
        end_ms = entry.get("end")
        duration_ms = entry.get("duration")

        if start_ms:
            start_times.append(start_ms)

        if end_ms:
            end_times.append(end_ms)

        if duration_ms:
            total_ms += duration_ms

    start_time = (
        datetime.fromtimestamp(min(start_times) / 1000, tz=timezone.utc)
        if start_times
        else None
    )

    end_time = (
        datetime.fromtimestamp(max(end_times) / 1000, tz=timezone.utc)
        if end_times
        else None
    )

    tracked_minutes = total_ms // (1000 * 60)

    return {
        "start_time": start_time,
        "end_time": end_time,
        "tracked_minutes": tracked_minutes,
    }
