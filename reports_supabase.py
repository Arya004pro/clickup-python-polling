import os
import re
import json
from pathlib import Path

try:
    import psycopg2
    from psycopg2.extras import Json
except ImportError:
    psycopg2 = None

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    # Fallback to root .env when running standalone from any cwd.
    from dotenv import load_dotenv

    env_candidates = [
        Path(__file__).resolve().parent / ".env",
        Path(__file__).resolve().parent / "clickup-motia-reports" / ".env",
    ]
    for env_path in env_candidates:
        if env_path.exists():
            load_dotenv(env_path)
            break
    DATABASE_URL = os.getenv("DATABASE_URL")


def _db():
    if not psycopg2:
        raise RuntimeError("psycopg2 is not installed.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(DATABASE_URL)


# ---------------------------------------------------------------------------
# Helpers: extract structured data from markdown
# ---------------------------------------------------------------------------


def _extract_member_hours(content: str) -> dict:
    members = {}
    in_employee_summary = False

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        if "### Employee Summary" in line or "### Member Summary" in line:
            in_employee_summary = True
            continue

        if in_employee_summary and line.startswith("### "):
            in_employee_summary = False

        if (
            in_employee_summary
            and line.startswith("|")
            and not line.startswith("| Member")
            and not line.startswith("|-")
        ):
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 4:
                name = parts[1]
                tracked_str = parts[3]

                hours = 0.0
                hm = re.search(r"(\d+(?:\.\d+)?)\s*h", tracked_str, re.IGNORECASE)
                if hm:
                    hours += float(hm.group(1))
                mm = re.search(r"(\d+(?:\.\d+)?)\s*m", tracked_str, re.IGNORECASE)
                if mm:
                    hours += float(mm.group(1)) / 60.0

                if hours > 0:
                    members[name] = round(members.get(name, 0.0) + hours, 2)

    return members


def _extract_total_hours(content: str) -> float:
    m = re.search(
        r"\bTotal Tracked:\s*(?:\*\*)?\s*(?:(\d+(?:\.\d+)?)\s*h)?\s*(?:(\d+(?:\.\d+)?)\s*m)?",
        content,
        re.IGNORECASE,
    )
    if m:
        h = float(m.group(1)) if m.group(1) else 0.0
        mins = float(m.group(2)) if m.group(2) else 0.0
        if h > 0 or mins > 0:
            return round(h + mins / 60.0, 2)

    member_data = _extract_member_hours(content)
    if member_data:
        return round(sum(member_data.values()), 2)
    return 0.0


def _extract_task_statuses(content: str) -> dict:
    STATUS_MAP = {
        "in progress": "In Progress",
        "completed": "Completed",
        "done": "Completed",
        "closed": "Completed",
        "to do": "To Do",
        "open": "To Do",
        "review": "In Review",
        "in review": "In Review",
        "blocked": "Blocked",
        "overdue": "Overdue",
        "backlog": "To Do",
        "shipped": "Completed",
    }
    counts = {}
    for line in content.lower().splitlines():
        if (
            line.startswith("|")
            and not line.startswith("| task")
            and not line.startswith("|-")
        ):
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                status_raw = parts[2]
                for keyword, canonical in STATUS_MAP.items():
                    if keyword == status_raw or (
                        len(keyword) > 3 and keyword in status_raw
                    ):
                        counts[canonical] = counts.get(canonical, 0) + 1
                        break
    return counts


def _parse_filename_meta(filename: str, content: str = "") -> dict:
    meta = {
        "space": "Unknown",
        "date": None,
        "kind": "generic",
        "period_type": "unknown",
    }

    dm = re.search(r"(\d{4}-\d{2}-\d{2})", filename)
    if dm:
        meta["date"] = dm.group(1)

    if "yesterday" in filename.lower():
        meta["period_type"] = "yesterday"
    elif "week" in filename.lower():
        meta["period_type"] = "this_week"
    elif "month" in filename.lower():
        meta["period_type"] = "this_month"
    elif "custom" in filename.lower():
        meta["period_type"] = "custom"
    else:
        meta["period_type"] = "today"

    if content:
        for line in content.splitlines()[:10]:
            line = line.strip()
            if line.startswith("## Space Report:"):
                meta["kind"] = "space"
                meta["space"] = line.replace("## Space Report:", "").strip()
                break
            elif line.startswith("## Member Report:"):
                meta["kind"] = "member"
                meta["space"] = (
                    "Member: " + line.replace("## Member Report:", "").strip()
                )
                break
            elif line.startswith("## Low Hours"):
                meta["kind"] = "low-hours"
                meta["space"] = "Low Hours Report"
                break
            elif line.startswith("## Overtime"):
                meta["kind"] = "overtime"
                meta["space"] = "Overtime Report"
                break
            elif line.startswith("## Missing"):
                meta["kind"] = "missing-estimates"
                meta["space"] = "Missing Estimates Report"
                break
            elif line.startswith("## All Members"):
                meta["kind"] = "all-members"
                meta["space"] = "All Members Report"
                break

    return meta


# ---------------------------------------------------------------------------
# Database Insert Logic
# ---------------------------------------------------------------------------


def upsert_report(filename: str, content: str = "") -> bool:
    """Read a markdown file or direct content and push its parsed summary to the correct Supabase table."""
    if not psycopg2 or not DATABASE_URL:
        return False

    try:
        meta = _parse_filename_meta(filename, content)
        member_hours = _extract_member_hours(content)
        task_statuses = _extract_task_statuses(content)
        total_hours = _extract_total_hours(content)
        size_bytes = len(content.encode("utf-8"))

        # Determine Route based on filename tagging
        table_name = "clickup_reports_manual"  # Default to manual testing
        fname_lower = filename.lower()

        # In openrouter_client.py, we prefix the filename with schedule_label slug
        # e.g., report_cron-weekly-report_space_blogmanager_period-na_timestamp.md
        if "cron" in fname_lower:
            if "week" in fname_lower:
                table_name = "clickup_reports_weekly"
            elif "month" in fname_lower:
                table_name = "clickup_reports_monthly"

        with _db() as conn:
            with conn.cursor() as cur:
                # Use psycopg2.extras.Json to safely serialize dictionaries into JSONB columns
                cur.execute(
                    f"""
                    INSERT INTO {table_name} 
                        (filename, space_name, period_type, report_kind, report_date, 
                         markdown_content, member_hours, task_statuses, total_hours, size_bytes, synced_at)
                    VALUES 
                        (%(filename)s, %(space_name)s, %(period_type)s, %(report_kind)s, %(report_date)s, 
                         %(markdown_content)s, %(member_hours)s::jsonb, %(task_statuses)s::jsonb,
                         %(total_hours)s, %(size_bytes)s, NOW())
                    ON CONFLICT (filename) DO UPDATE SET
                        space_name       = EXCLUDED.space_name,
                        period_type      = EXCLUDED.period_type,
                        report_kind      = EXCLUDED.report_kind,
                        report_date      = EXCLUDED.report_date,
                        markdown_content = EXCLUDED.markdown_content,
                        member_hours     = EXCLUDED.member_hours,
                        task_statuses    = EXCLUDED.task_statuses,
                        total_hours      = EXCLUDED.total_hours,
                        size_bytes       = EXCLUDED.size_bytes,
                        synced_at        = NOW()
                    """,
                    {
                        "filename": filename,
                        "space_name": meta["space"],
                        "period_type": meta["period_type"],
                        "report_kind": meta["kind"],
                        "report_date": meta["date"],
                        "markdown_content": content,
                        "member_hours": Json(member_hours),
                        "task_statuses": Json(task_statuses),
                        "total_hours": total_hours,
                        "size_bytes": size_bytes,
                    },
                )
            conn.commit()
        return True
    except Exception as e:
        print(f"[{table_name}] Failed to save report {filename} to Supabase: {e}")
        return False
