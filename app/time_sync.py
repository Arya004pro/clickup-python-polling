from datetime import datetime, timezone
from collections import defaultdict

from app.supabase_db import update_task_time
from app.clickup import fetch_time_entries
from app.time_tracking import aggregate_time_entries


def sync_time_entries(updated_after_ms: int | None = None) -> int:
    """
    Incremental time sync (SAFE):
    - Only updates tasks that have NEW time entries
    - Never overwrites existing tracked time with zero
    """

    time_entries = fetch_time_entries()
    if not time_entries:
        return 0

    entries_by_task = defaultdict(list)

    for entry in time_entries:
        updated = entry.get("updated")
        if updated_after_ms and updated:
            if int(updated) <= updated_after_ms:
                continue

        task_id = entry.get("task", {}).get("id")
        if task_id:
            entries_by_task[task_id].append(entry)

    if not entries_by_task:
        return 0

    updated_count = 0
    now_utc = datetime.now(timezone.utc).isoformat()

    for task_id, entries in entries_by_task.items():
        # SAFETY CHECK
        if not entries:
            continue

        aggregated = aggregate_time_entries(entries)

        update_task_time(
            task_id=task_id,
            tracked_minutes=aggregated["tracked_minutes"],
            start_time=aggregated["start_time"].isoformat()
            if aggregated["start_time"]
            else None,
            end_time=aggregated["end_time"].isoformat()
            if aggregated["end_time"]
            else None,
            updated_at=now_utc,
        )

        updated_count += 1

    return updated_count
