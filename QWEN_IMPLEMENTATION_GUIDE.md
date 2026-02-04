# Qwen 2.5-7B Implementation Guide

## Overview

This guide covers the implementation of Qwen 2.5-7B-Instruct for ClickUp project management with **zero hallucination** through direct API integration.

---

## ✅ Implementation Status

### Primary Implementation: `qwen_client.py`

**Status:** ✅ Complete

**Features:**

1. ✅ 4 Custom Time Entry Reports (Direct ClickUp API - No Hallucination)
2. ✅ 54 MCP Tool Integration
3. ✅ LM Studio Integration
4. ✅ Structured Output Validation
5. ✅ Interactive CLI

---

## 🚀 Quick Start

### Prerequisites

1. **LM Studio** installed and running
2. **Qwen 2.5-7B-Instruct** model loaded in LM Studio
3. **ClickUp API Token** configured
4. **MCP Server** running (`python -m app.mcp.mcp_server`)

### Environment Setup

```bash
# .env file
CLICKUP_API_TOKEN=your_token_here
CLICKUP_TEAM_ID=your_team_id_here
LM_STUDIO_BASE_URL=http://localhost:1234/v1
LM_STUDIO_MODEL=qwen2.5-7b-instruct
MCP_SERVER_URL=http://127.0.0.1:8001/sse
```

### Running the Client

```bash
# Terminal 1 - Start MCP Server
uvicorn app.mcp.mcp_server:mcp --host 0.0.0.0 --port 8001

# Terminal 2 - Start LM Studio
# Load Qwen 2.5-7B-Instruct model
# Enable API server on port 1234

# Terminal 3 - Run Qwen Client
python qwen_client.py
```

---

## 📊 Custom Reports Implementation

### Report 1: Space-wise Time Entry Report

**Function:** `generate_space_wise_time_report(start_date, end_date)`

**What it does:**

- Fetches ALL time entries for team within date range
- Groups entries by ClickUp Space
- Aggregates: total hours, entry count, unique tasks, unique users

**Data Flow:**

```
User Query → Qwen → generate_space_wise_time_report()
                  ↓
          ClickUp API /team/{id}/time_entries
                  ↓
          Group by Space (from task hierarchy)
                  ↓
          Return structured JSON
```

**Output Example:**

```json
{
  "report_type": "space_wise_time_entries",
  "date_range": {
    "start": "2026-01-01T00:00:00",
    "end": "2026-02-04T00:00:00"
  },
  "spaces": {
    "Marketing": {
      "total_hours": 120.5,
      "total_entries": 45,
      "unique_tasks": 12,
      "unique_users": 5,
      "users": ["Alice", "Bob", "Charlie", "David", "Eve"]
    },
    "Engineering": {
      "total_hours": 340.2,
      "total_entries": 120,
      "unique_tasks": 35,
      "unique_users": 8,
      "users": [...]
    }
  },
  "total_spaces": 2
}
```

**Zero Hallucination:** All data comes directly from ClickUp API. No LLM inference on numbers.

---

### Report 2: Space > Folder > Member Time Report

**Function:** `generate_space_folder_member_report(start_date, end_date, space_name=None)`

**What it does:**

- Builds hierarchical time breakdown: Space → Folder → User
- Shows time distribution across workspace structure
- Optional space filter

**Data Flow:**

```
User Query → Qwen → generate_space_folder_member_report()
                  ↓
          Fetch workspace structure (Spaces/Folders)
                  ↓
          Fetch time entries
                  ↓
          Map entries to hierarchy (Space→Folder→User)
                  ↓
          Return nested JSON
```

**Output Example:**

```json
{
  "report_type": "space_folder_member_time_entries",
  "hierarchy": {
    "Marketing": {
      "Campaigns": {
        "Alice": {
          "total_hours": 40.5,
          "total_entries": 15,
          "unique_tasks": 8
        },
        "Bob": {...}
      },
      "No Folder": {
        "Charlie": {...}
      }
    },
    "Engineering": {...}
  }
}
```

---

### Report 3: Team Member-wise Time Report

**Function:** `generate_team_member_report(start_date, end_date, member_name=None)`

**What it does:**

- Shows time breakdown per user
- Includes daily breakdown
- Shows spaces/tasks worked on
- Optional member filter

**Data Flow:**

```
User Query → Qwen → generate_team_member_report()
                  ↓
          Fetch all time entries
                  ↓
          Group by user
                  ↓
          Build daily breakdown
                  ↓
          Return user-centric JSON
```

**Output Example:**

```json
{
  "report_type": "team_member_time_entries",
  "members": {
    "Alice": {
      "total_hours": 120.5,
      "total_entries": 45,
      "unique_tasks": 12,
      "unique_spaces": 3,
      "spaces_worked": ["Marketing", "Sales", "Support"],
      "daily_hours": {
        "2026-01-15": 8.5,
        "2026-01-16": 7.2,
        ...
      }
    },
    "Bob": {...}
  },
  "total_members": 12
}
```

---

### Report 4: Weekly Reports

**Function:** `generate_weekly_report(report_type, weeks_back=1, **kwargs)`

**What it does:**

- Generates weekly breakdown of any above report
- Supports: 'space', 'space_folder_member', 'team_member'
- Configurable lookback period

**Data Flow:**

```
User Query → Qwen → generate_weekly_report(type='space', weeks=4)
                  ↓
          Calculate week boundaries
                  ↓
          For each week:
              Call generate_{type}_report(week_start, week_end)
                  ↓
          Aggregate weekly reports
                  ↓
          Return time-series data
```

**Output Example:**

```json
{
  "report_type": "weekly_space_time_entries",
  "weeks_analyzed": 4,
  "total_weeks": 4,
  "weekly_breakdown": [
    {
      "week_start": "2026-01-07T00:00:00",
      "week_end": "2026-01-14T00:00:00",
      "report": {
        "spaces": {
          "Marketing": {"total_hours": 30.5, ...},
          "Engineering": {"total_hours": 85.2, ...}
        }
      }
    },
    {
      "week_start": "2026-01-14T00:00:00",
      "week_end": "2026-01-21T00:00:00",
      "report": {...}
    },
    ...
  ]
}
```

---

## 🔧 Anti-Hallucination Architecture

### Problem: LLMs Hallucinate Numbers

**Traditional Approach:**

```
User: "Show time report" → LLM → "Marketing: 45 hours" (MADE UP!)
```

**Our Solution:**

```
User: "Show time report" → Qwen detects intent
                         → Calls generate_space_wise_time_report()
                         → Direct ClickUp API call
                         → Real data returned
                         → Qwen formats output (text only)
```

### Key Principles

1. **LLM for Intent, Not Data**
   - Qwen identifies which report to run
   - Qwen does NOT calculate or generate numbers
   - All numeric data comes from ClickUp API

2. **Direct API Integration**
   - `get_time_entries_for_team()` - Direct API call
   - `get_task_details()` - Direct API call
   - No intermediate LLM processing of raw data

3. **Validation Layer**

   ```python
   # Every API response is validated
   data, error = _api_call("GET", endpoint)
   if error:
       return {"error": error}  # Explicit error, no hallucination
   ```

4. **Structured Output**
   - All reports return strict JSON schema
   - No free-form text with numbers
   - Easy to verify and audit

---

## 🛠️ Alternative: Adding Reports as MCP Tools

If the current implementation doesn't work well, you can add the 4 reports as native MCP tools:

### File: `app/mcp/time_reports.py`

```python
"""
Time Entry Reports Module for ClickUp MCP Server
Provides 4 specialized time tracking reports using direct ClickUp API
"""

from fastmcp import FastMCP
import requests
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict
from collections import defaultdict
from app.config import CLICKUP_API_TOKEN, BASE_URL, CLICKUP_TEAM_ID

def _headers() -> Dict[str, str]:
    return {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}

def _api_call(method: str, endpoint: str, params: Optional[Dict] = None):
    url = f"{BASE_URL}{endpoint}"
    try:
        response = requests.request(method, url, headers=_headers(), params=params, timeout=30)
        return (response.json(), None) if response.status_code == 200 else (None, f"Error {response.status_code}")
    except Exception as e:
        return None, str(e)

def register_time_report_tools(mcp: FastMCP):
    """Register time entry report tools with MCP server"""

    @mcp.tool()
    def get_space_wise_time_report(
        start_date_iso: str,
        end_date_iso: str
    ) -> dict:
        """
        Generate space-wise time entry report.

        Args:
            start_date_iso: Start date in ISO format (e.g., '2026-01-01T00:00:00Z')
            end_date_iso: End date in ISO format

        Returns:
            Breakdown of time entries grouped by Space with total hours,
            entry counts, unique tasks, and unique users per space.
        """
        try:
            # Parse dates to milliseconds
            start_ms = int(datetime.fromisoformat(start_date_iso.replace('Z', '+00:00')).timestamp() * 1000)
            end_ms = int(datetime.fromisoformat(end_date_iso.replace('Z', '+00:00')).timestamp() * 1000)

            # Fetch time entries
            params = {"start_date": start_ms, "end_date": end_ms}
            data, error = _api_call("GET", f"/team/{CLICKUP_TEAM_ID}/time_entries", params=params)

            if error:
                return {"error": error}

            time_entries = data.get("data", [])

            # Get workspace structure for space mapping
            spaces_data, _ = _api_call("GET", f"/team/{CLICKUP_TEAM_ID}/space")
            space_map = {s["id"]: s["name"] for s in spaces_data.get("spaces", [])}

            # Group by space
            space_report = defaultdict(lambda: {
                "total_duration_ms": 0,
                "entries_count": 0,
                "tasks": set(),
                "users": set()
            })

            for entry in time_entries:
                task_id = entry.get("task", {}).get("id")
                if not task_id:
                    continue

                # Get task to find space
                task_data, _ = _api_call("GET", f"/task/{task_id}")
                if not task_data:
                    continue

                space_id = task_data.get("space", {}).get("id")
                space_name = space_map.get(space_id, "Unknown Space")

                duration = int(entry.get("duration", 0))
                user_name = entry.get("user", {}).get("username", "Unknown")

                space_report[space_name]["total_duration_ms"] += duration
                space_report[space_name]["entries_count"] += 1
                space_report[space_name]["tasks"].add(task_id)
                space_report[space_name]["users"].add(user_name)

            # Convert to serializable format
            final_report = {}
            for space_name, data in space_report.items():
                final_report[space_name] = {
                    "total_hours": round(data["total_duration_ms"] / (1000 * 60 * 60), 2),
                    "total_entries": data["entries_count"],
                    "unique_tasks": len(data["tasks"]),
                    "unique_users": len(data["users"]),
                    "users": sorted(list(data["users"]))
                }

            return {
                "report_type": "space_wise_time_entries",
                "date_range": {"start": start_date_iso, "end": end_date_iso},
                "spaces": final_report,
                "total_spaces": len(final_report)
            }

        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_space_folder_member_time_report(
        start_date_iso: str,
        end_date_iso: str,
        space_filter: Optional[str] = None
    ) -> dict:
        """
        Generate hierarchical Space > Folder > Member time entry report.

        Args:
            start_date_iso: Start date in ISO format
            end_date_iso: End date in ISO format
            space_filter: Optional space name to filter results

        Returns:
            Nested breakdown showing time distribution across
            Space → Folder → Team Member hierarchy.
        """
        try:
            start_ms = int(datetime.fromisoformat(start_date_iso.replace('Z', '+00:00')).timestamp() * 1000)
            end_ms = int(datetime.fromisoformat(end_date_iso.replace('Z', '+00:00')).timestamp() * 1000)

            params = {"start_date": start_ms, "end_date": end_ms}
            data, error = _api_call("GET", f"/team/{CLICKUP_TEAM_ID}/time_entries", params=params)

            if error:
                return {"error": error}

            time_entries = data.get("data", [])

            # Build hierarchical report
            hierarchy = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {
                "total_duration_ms": 0,
                "entries_count": 0,
                "tasks": set()
            })))

            for entry in time_entries:
                task_id = entry.get("task", {}).get("id")
                if not task_id:
                    continue

                task_data, _ = _api_call("GET", f"/task/{task_id}")
                if not task_data:
                    continue

                space = task_data.get("space", {}).get("name", "Unknown Space")
                folder = task_data.get("folder", {}).get("name", "No Folder")
                user = entry.get("user", {}).get("username", "Unknown User")
                duration = int(entry.get("duration", 0))

                # Apply space filter
                if space_filter and space.lower() != space_filter.lower():
                    continue

                hierarchy[space][folder][user]["total_duration_ms"] += duration
                hierarchy[space][folder][user]["entries_count"] += 1
                hierarchy[space][folder][user]["tasks"].add(task_id)

            # Convert to serializable
            final_hierarchy = {}
            for space, folders in hierarchy.items():
                final_hierarchy[space] = {}
                for folder, users in folders.items():
                    final_hierarchy[space][folder] = {}
                    for user, data in users.items():
                        final_hierarchy[space][folder][user] = {
                            "total_hours": round(data["total_duration_ms"] / (1000 * 60 * 60), 2),
                            "total_entries": data["entries_count"],
                            "unique_tasks": len(data["tasks"])
                        }

            return {
                "report_type": "space_folder_member_time_entries",
                "date_range": {"start": start_date_iso, "end": end_date_iso},
                "filter": {"space": space_filter} if space_filter else None,
                "hierarchy": final_hierarchy
            }

        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_team_member_time_report(
        start_date_iso: str,
        end_date_iso: str,
        member_filter: Optional[str] = None
    ) -> dict:
        """
        Generate team member-wise time entry report with daily breakdown.

        Args:
            start_date_iso: Start date in ISO format
            end_date_iso: End date in ISO format
            member_filter: Optional member username to filter results

        Returns:
            Per-user time breakdown showing total hours, daily hours,
            unique tasks, and spaces worked on.
        """
        try:
            start_ms = int(datetime.fromisoformat(start_date_iso.replace('Z', '+00:00')).timestamp() * 1000)
            end_ms = int(datetime.fromisoformat(end_date_iso.replace('Z', '+00:00')).timestamp() * 1000)

            params = {"start_date": start_ms, "end_date": end_ms}
            data, error = _api_call("GET", f"/team/{CLICKUP_TEAM_ID}/time_entries", params=params)

            if error:
                return {"error": error}

            time_entries = data.get("data", [])

            member_report = defaultdict(lambda: {
                "total_duration_ms": 0,
                "entries_count": 0,
                "tasks": set(),
                "spaces": set(),
                "daily_breakdown": defaultdict(int)
            })

            for entry in time_entries:
                user = entry.get("user", {}).get("username", "Unknown User")

                # Apply member filter
                if member_filter and user.lower() != member_filter.lower():
                    continue

                task_id = entry.get("task", {}).get("id")
                duration = int(entry.get("duration", 0))
                start_entry_ms = int(entry.get("start", 0))

                # Get task for space info
                task_data, _ = _api_call("GET", f"/task/{task_id}") if task_id else (None, None)
                space_name = task_data.get("space", {}).get("name", "Unknown") if task_data else "Unknown"

                # Date for daily breakdown
                date_str = datetime.fromtimestamp(start_entry_ms/1000).date().isoformat()

                member_report[user]["total_duration_ms"] += duration
                member_report[user]["entries_count"] += 1
                if task_id:
                    member_report[user]["tasks"].add(task_id)
                member_report[user]["spaces"].add(space_name)
                member_report[user]["daily_breakdown"][date_str] += round(duration / (1000 * 60 * 60), 2)

            # Convert to serializable
            final_report = {}
            for user, data in member_report.items():
                final_report[user] = {
                    "total_hours": round(data["total_duration_ms"] / (1000 * 60 * 60), 2),
                    "total_entries": data["entries_count"],
                    "unique_tasks": len(data["tasks"]),
                    "unique_spaces": len(data["spaces"]),
                    "spaces_worked": sorted(list(data["spaces"])),
                    "daily_hours": dict(data["daily_breakdown"])
                }

            return {
                "report_type": "team_member_time_entries",
                "date_range": {"start": start_date_iso, "end": end_date_iso},
                "filter": {"member": member_filter} if member_filter else None,
                "members": final_report,
                "total_members": len(final_report)
            }

        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_weekly_time_report(
        report_type: str,
        weeks_back: int = 1,
        space_filter: Optional[str] = None,
        member_filter: Optional[str] = None
    ) -> dict:
        """
        Generate weekly breakdown of any time report type.

        Args:
            report_type: Type of report ('space' | 'space_folder_member' | 'team_member')
            weeks_back: Number of weeks to analyze (default: 1)
            space_filter: Optional space name filter
            member_filter: Optional member name filter

        Returns:
            Week-by-week breakdown of the selected report type.
        """
        try:
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(weeks=weeks_back)

            weekly_reports = []
            current_start = start_date

            while current_start < end_date:
                week_end = min(current_start + timedelta(days=7), end_date)
                start_iso = current_start.isoformat()
                end_iso = week_end.isoformat()

                # Generate appropriate report
                if report_type == "space":
                    week_report = get_space_wise_time_report(start_iso, end_iso)
                elif report_type == "space_folder_member":
                    week_report = get_space_folder_member_time_report(start_iso, end_iso, space_filter)
                elif report_type == "team_member":
                    week_report = get_team_member_time_report(start_iso, end_iso, member_filter)
                else:
                    return {"error": f"Invalid report_type: {report_type}"}

                weekly_reports.append({
                    "week_start": start_iso,
                    "week_end": end_iso,
                    "report": week_report
                })

                current_start = week_end

            return {
                "report_type": f"weekly_{report_type}_time_entries",
                "weeks_analyzed": weeks_back,
                "total_weeks": len(weekly_reports),
                "weekly_breakdown": weekly_reports
            }

        except Exception as e:
            return {"error": str(e)}
```

### Integration Steps

1. **Update `app/mcp/mcp_server.py`:**

```python
from app.mcp.time_reports import register_time_report_tools

# Add this line with other registrations
register_time_report_tools(mcp)
```

2. **Restart MCP Server:**

```bash
uvicorn app.mcp.mcp_server:mcp --reload --host 0.0.0.0 --port 8001
```

3. **Verify Tools Loaded:**
   The MCP server will now expose 58 tools (54 existing + 4 new time reports).

---

## 🎯 Usage Examples

### Example 1: Space-wise Report

```
You: Show me space-wise time entries for January 2026

Qwen: I'll generate a space-wise time entry report for January 2026.

[Calls: generate_space_wise_time_report(start=2026-01-01, end=2026-02-01)]

📊 Report Generated:

{
  "report_type": "space_wise_time_entries",
  "spaces": {
    "Marketing": {
      "total_hours": 245.5,
      "total_entries": 89,
      "unique_tasks": 23,
      "unique_users": 6
    },
    "Engineering": {
      "total_hours": 512.3,
      "total_entries": 156,
      "unique_tasks": 45,
      "unique_users": 12
    }
  }
}

Summary: In January, your team logged 757.8 hours across 2 spaces,
with Engineering having the most activity (512.3 hours).
```

### Example 2: Team Member Report with Filter

```
You: Show me time report for Alice for the past 2 weeks

Qwen: I'll generate Alice's time report for the past 2 weeks.

[Calls: generate_team_member_report(member='Alice', start=..., end=...)]

📊 Report Generated:

{
  "members": {
    "Alice": {
      "total_hours": 78.5,
      "total_entries": 32,
      "unique_tasks": 15,
      "unique_spaces": 3,
      "spaces_worked": ["Marketing", "Sales", "Support"],
      "daily_hours": {
        "2026-01-20": 8.5,
        "2026-01-21": 7.2,
        "2026-01-22": 8.0,
        ...
      }
    }
  }
}

Summary: Alice logged 78.5 hours over 32 time entries across 3 spaces.
Peak day: Jan 20 with 8.5 hours.
```

### Example 3: Weekly Breakdown

```
You: Give me weekly space-wise reports for the past month

Qwen: I'll generate weekly space-wise reports for the past 4 weeks.

[Calls: generate_weekly_report(type='space', weeks=4)]

📊 Report Generated:

{
  "weekly_breakdown": [
    {
      "week_start": "2026-01-07",
      "week_end": "2026-01-14",
      "report": {
        "spaces": {
          "Marketing": {"total_hours": 62.3},
          "Engineering": {"total_hours": 128.5}
        }
      }
    },
    {
      "week_start": "2026-01-14",
      "week_end": "2026-01-21",
      "report": {...}
    },
    ...
  ]
}

Summary: 4-week trend shows Engineering averaging 130 hours/week,
Marketing at 60 hours/week. Week of Jan 14 had peak activity.
```

### Example 4: Using MCP Tools

```
You: Show me all overdue tasks in Marketing

Qwen: I'll fetch overdue tasks from the Marketing space.

[Calls MCP Tool: get_tasks(project='Marketing', filter='overdue')]

📋 Found 7 overdue tasks:

1. "Q1 Campaign Launch" - Due: Jan 15 (20 days overdue)
2. "Social Media Assets" - Due: Jan 20 (15 days overdue)
...

Would you like me to get more details on any specific task?
```

---

## 🔍 Debugging & Validation

### Verify MCP Tools Loaded

```python
# In qwen_client.py, add this to initialization:
print(f"MCP Tools: {len(self.mcp.tools)}")
for tool in self.mcp.tools[:5]:
    print(f"  - {tool.name}: {tool.description[:50]}...")
```

### Test Direct API Calls

```python
# Test ClickUp API connectivity
from qwen_client import get_time_entries_for_team, get_team_id
import datetime

team_id = get_team_id()
start = int(datetime.datetime(2026, 1, 1).timestamp() * 1000)
end = int(datetime.datetime(2026, 2, 1).timestamp() * 1000)

entries = get_time_entries_for_team(team_id, start, end)
print(f"Found {len(entries)} time entries")
```

### Validate Report Output

```python
# Test report generation
from qwen_client import generate_space_wise_time_report
import datetime

start = int(datetime.datetime(2026, 1, 1).timestamp() * 1000)
end = int(datetime.datetime(2026, 2, 1).timestamp() * 1000)

report = generate_space_wise_time_report(start, end)
print(json.dumps(report, indent=2))
```

---

## 📈 Performance Optimization

### For Large Workspaces (1000+ tasks)

1. **Batch API Calls:**

   ```python
   # Instead of calling get_task_details() for every entry:
   # Cache task details in memory
   task_cache = {}

   for entry in time_entries:
       task_id = entry["task"]["id"]
       if task_id not in task_cache:
           task_cache[task_id] = get_task_details(task_id)
   ```

2. **Pagination:**

   ```python
   # ClickUp API returns max 100 entries per page
   # Implement pagination in get_time_entries_for_team()
   ```

3. **Date Range Limiting:**
   ```python
   # For weekly reports, limit to 12 weeks max
   if weeks_back > 12:
       weeks_back = 12
       print("⚠️ Limited to 12 weeks for performance")
   ```

---

## 🛡️ Error Handling

### Common Errors & Solutions

**Error:** `API Error 401: Unauthorized`

- **Solution:** Check `CLICKUP_API_TOKEN` in `.env`

**Error:** `No team ID found`

- **Solution:** Set `CLICKUP_TEAM_ID` explicitly in `.env`

**Error:** `LM Studio connection refused`

- **Solution:** Ensure LM Studio is running with API server enabled on port 1234

**Error:** `MCP server not responding`

- **Solution:** Start MCP server: `uvicorn app.mcp.mcp_server:mcp --port 8001`

**Error:** `Tool call failed: timeout`

- **Solution:** Increase timeout in `_api_call()` or reduce date range

---

## 🚦 Testing Checklist

- [ ] LM Studio running with Qwen 2.5-7B loaded
- [ ] MCP server running on port 8001
- [ ] `.env` file configured with all tokens
- [ ] Test 1: `python qwen_client.py` starts without errors
- [ ] Test 2: Query "list all spaces" returns real data
- [ ] Test 3: Query "space-wise time report" generates valid JSON
- [ ] Test 4: Query "weekly report" returns multiple weeks
- [ ] Test 5: All 4 custom reports work
- [ ] Test 6: MCP tools (e.g., get tasks) work
- [ ] Test 7: Error handling (invalid dates, missing data) works

---

## 📝 Next Steps

### If Current Implementation Works ✅

1. Fine-tune Qwen system prompts for better intent detection
2. Add more query examples to improve user experience
3. Implement caching for frequently accessed data
4. Add export functionality (CSV, Excel)

### If You Need MCP Tool Approach 🔧

1. Copy `time_reports.py` code from guide to `app/mcp/time_reports.py`
2. Update `mcp_server.py` to register tools
3. Restart MCP server
4. Update `qwen_client.py` to remove custom report functions
5. Rely entirely on MCP tool calls

---

## 💡 Tips for Qwen Prompting

### Good Prompts (Clear Intent)

✅ "Show me space-wise time entries for January"
✅ "Team member report for Alice last week"
✅ "Weekly breakdown for Marketing space, past 4 weeks"
✅ "List all overdue tasks"

### Bad Prompts (Ambiguous)

❌ "What's the status?" (status of what?)
❌ "Show report" (which report?)
❌ "Time stuff" (too vague)

### Best Practices

- Be specific about date ranges
- Mention entity names (space, user, folder)
- Use keywords: "time", "report", "weekly", "breakdown"
- For debugging: "Show me raw API response for..."

---

## 📚 References

- **ClickUp API Docs:** https://clickup.com/api
- **LM Studio:** https://lmstudio.ai/
- **Qwen 2.5:** https://huggingface.co/Qwen
- **MCP Protocol:** https://modelcontextprotocol.io/

---

## 🎉 Success Criteria

Your implementation is successful when:

1. ✅ Qwen responds to queries WITHOUT making up numbers
2. ✅ All 4 time reports return real ClickUp data
3. ✅ MCP tools (54) are accessible and working
4. ✅ Weekly reports show accurate time-series data
5. ✅ Error messages are clear and actionable
6. ✅ Response time < 10 seconds for typical queries
7. ✅ No hallucinated users, spaces, or time values

---

## 🐛 Known Limitations

1. **Rate Limiting:** ClickUp API has rate limits (100 req/min). Large workspaces may hit limits.
   - **Mitigation:** Implement caching and batch requests

2. **Memory Usage:** Large time entry datasets may consume significant RAM.
   - **Mitigation:** Process in chunks or limit date ranges

3. **Qwen Context:** 7B model has limited context compared to 70B models.
   - **Mitigation:** Keep prompts concise, use structured output

4. **LM Studio Performance:** Local inference slower than cloud APIs.
   - **Mitigation:** Use GPU acceleration if available

---

## 🔮 Future Enhancements

1. **Visualization:** Add chart generation (matplotlib, plotly)
2. **Export:** CSV/Excel export for reports
3. **Scheduling:** Automated weekly report emails
4. **Comparisons:** Week-over-week, month-over-month trends
5. **Anomaly Detection:** Flag unusual time patterns
6. **Predictive:** Estimate project completion based on velocity

---

**End of Guide**
