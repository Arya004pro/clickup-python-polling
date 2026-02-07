# MCP Server Changes Required — Time Tracking Reports

## Problem Summary

### 1. Folder-Level Project Resolution is Broken

**Root Cause:** Two independent resolution paths exist, neither properly reads `project_map.json`:

- **`pm_analytics._resolve_to_list_ids(project, list_id)`** — Only matches space/folder by **name** from the live ClickUp API. It does NOT consult the `project_map.json` mapped projects. So when you ask for "Luminique" (a folder mapped in `project_map.json`), it scans all spaces and tries to match folder names live — which is slow and unreliable.

- **`project_intelligence._get_ids(p_name)`** — Searches `TRACKED_PROJECTS` (in-memory list from `project_configuration.py`). But mapped projects from `project_map.json` are stored by `sync_mapping.py` and never loaded into `TRACKED_PROJECTS`. So folder/space mapped projects are invisible to `_get_ids`.

**Fix Needed:** Both `_resolve_to_list_ids` and `_get_ids` must first check `project_map.json` (via `sync_mapping.db`) before falling back to live API resolution.

### 2. Time Tracking Uses `time_spent` (ClickUp API Rollup) Not Actual Time Entries

**Root Cause:** Both `get_time_tracking_report` and `get_project_time_tracking` use the `time_spent` field from task objects. This is ClickUp's **rolled-up** time which:

- May only include active/running timers
- Differs from actual time entries stored in Supabase
- Doesn't include manual time entries properly in all cases
- Gets inflated/deflated by subtask rollup logic

**Fix Needed:** New tools that use the ClickUp `/team/{team_id}/time_entries` API endpoint or read from Supabase `time_entries` table for accurate time data.

### 3. Missing Report Types

The following report types are needed but don't exist as MCP tools:

| Report Type                              | Description                                    | Currently Available? |
| ---------------------------------------- | ---------------------------------------------- | -------------------- |
| Space-wise time entry report             | Total time tracked per space                   | ❌ No                |
| Space > Folder > Team member wise report | Drill-down: Space → Folder → Member time       | ❌ No                |
| Team member wise time entry report       | All time across all spaces per member          | ❌ No                |
| Weekly variants of all above             | Same reports filtered to current/specific week | ❌ No                |

---

## New MCP Tools to Create

### Tool 1: `get_space_time_report`

```python
def get_space_time_report(workspace_id: str = None, week: str = None) -> dict:
    """
    Time entry report aggregated by SPACE.

    Args:
        workspace_id: Optional workspace ID (auto-resolves if empty)
        week: Optional week filter in format "YYYY-Wnn" (e.g., "2026-W06")
              or "current" for current week. If None, returns all-time data.

    Returns:
        {
            "report_type": "space_wise_time_entries",
            "period": "2026-W06" or "all_time",
            "spaces": [
                {
                    "space_id": "...",
                    "space_name": "3D Team",
                    "total_time_ms": 14400000,
                    "total_time_human": "4h 0m",
                    "member_count": 3,
                    "task_count": 12
                },
                ...
            ],
            "grand_total_ms": ...,
            "grand_total_human": "..."
        }

    Data Source: ClickUp /team/{team_id}/time_entries API with date filters
    Resolution: Fetches time entries → maps task_id → list → folder → space
    """
```

### Tool 2: `get_folder_team_time_report`

```python
def get_folder_team_time_report(
    space_id: str = None,
    space_name: str = None,
    project: str = None,
    week: str = None
) -> dict:
    """
    Time entry report for Space > Folder > Team Member breakdown.
    Can be called with a space ID/name or a mapped project name.

    Args:
        space_id: Direct space ID
        space_name: Space name (resolved to ID)
        project: Mapped project name from project_map.json
        week: Optional week filter ("YYYY-Wnn" or "current")

    Returns:
        {
            "report_type": "folder_team_time_entries",
            "space": "JewelleryOS",
            "period": "2026-W06",
            "folders": [
                {
                    "folder_id": "90167907863",
                    "folder_name": "Luminique",
                    "total_time_ms": 7200000,
                    "total_time_human": "2h 0m",
                    "members": [
                        {
                            "user": "john_doe",
                            "time_ms": 3600000,
                            "time_human": "1h 0m",
                            "tasks_worked": 3
                        },
                        ...
                    ]
                },
                ...
            ],
            "folderless_lists": [
                {
                    "list_id": "...",
                    "list_name": "Ongoing RnD",
                    "total_time_ms": ...,
                    "members": [...]
                }
            ]
        }

    Resolution:
    1. Resolve space (from ID, name, or mapped project)
    2. Get all folders/lists in space
    3. For each list, fetch time entries
    4. Group by folder → member
    """
```

### Tool 3: `get_team_member_time_report`

```python
def get_team_member_time_report(
    workspace_id: str = None,
    project: str = None,
    space_id: str = None,
    week: str = None
) -> dict:
    """
    Time entry report grouped by TEAM MEMBER.
    Can scope to workspace-wide, space-level, or project-level.

    Args:
        workspace_id: Scope to entire workspace
        project: Scope to a mapped project
        space_id: Scope to a specific space
        week: Optional week filter ("YYYY-Wnn" or "current")

    Returns:
        {
            "report_type": "team_member_time_entries",
            "scope": "workspace" | "space:JewelleryOS" | "project:Luminique",
            "period": "2026-W06",
            "members": [
                {
                    "user_id": "...",
                    "username": "john_doe",
                    "total_time_ms": 14400000,
                    "total_time_human": "4h 0m",
                    "breakdown_by_space": {
                        "3D Team": {"time_ms": 7200000, "time_human": "2h 0m"},
                        "JewelleryOS": {"time_ms": 7200000, "time_human": "2h 0m"}
                    },
                    "tasks_worked": 8
                },
                ...
            ],
            "grand_total_ms": ...,
            "grand_total_human": "..."
        }

    Data Source: ClickUp Time Entries API (/team/{team_id}/time_entries)
    Resolution:
    1. Determine scope (workspace/space/project)
    2. Fetch time entries with appropriate filters
    3. For project scope, resolve to list IDs from project_map.json
    4. Group by user → breakdown by space
    """
```

### Tool 4: `get_weekly_summary_report`

```python
def get_weekly_summary_report(
    report_type: str = "team_member",
    project: str = None,
    space_id: str = None,
    workspace_id: str = None,
    week: str = "current"
) -> dict:
    """
    Unified weekly report wrapper.

    Args:
        report_type: "space" | "folder_team" | "team_member"
        project: Mapped project name (for folder_team/team_member)
        space_id: Space ID (for folder_team)
        workspace_id: Workspace ID (for space/team_member)
        week: Week to report ("current", "last", or "YYYY-Wnn")

    Delegates to the appropriate report tool with week filter applied.
    """
```

---

## Changes to Existing Code

### Fix 1: `pm_analytics._resolve_to_list_ids` — Add project_map.json lookup

```python
def _resolve_to_list_ids(project: Optional[str], list_id: Optional[str]) -> List[str]:
    if list_id:
        return [list_id]
    if not project:
        return []

    # NEW: Check project_map.json FIRST
    from .sync_mapping import db
    proj_lower = project.lower().strip()
    for alias, data in db.projects.items():
        if alias.lower() == proj_lower or data.get("alias", "").lower() == proj_lower:
            mapped_id = data["clickup_id"]
            mapped_type = data["clickup_type"]

            if mapped_type == "list":
                return [mapped_id]
            elif mapped_type == "folder":
                # Get all lists in this folder
                resp, _ = _api_call("GET", f"/folder/{mapped_id}/list")
                if resp:
                    return [lst["id"] for lst in resp.get("lists", [])]
                # Fallback: use structure from mapping
                structure = data.get("structure", {})
                return [c["id"] for c in structure.get("children", []) if c.get("type") == "list"]
            elif mapped_type == "space":
                ids = []
                resp, _ = _api_call("GET", f"/space/{mapped_id}/list")
                if resp:
                    ids.extend([lst["id"] for lst in resp.get("lists", [])])
                resp2, _ = _api_call("GET", f"/space/{mapped_id}/folder")
                if resp2:
                    for f in resp2.get("folders", []):
                        ids.extend([lst["id"] for lst in f.get("lists", [])])
                return ids

    # EXISTING: Fall back to live API search
    team_id = _get_team_id()
    # ... rest of existing code
```

### Fix 2: `project_intelligence._get_ids` — Add project_map.json lookup

```python
def _get_ids(p_name):
    # 1. Try TRACKED_PROJECTS first (in-memory)
    p = next((x for x in TRACKED_PROJECTS if x["name"] == p_name), None)
    if p:
        # ... existing logic

    # 2. NEW: Check project_map.json
    from .sync_mapping import db
    for alias, data in db.projects.items():
        if alias.lower() == p_name.lower():
            mapped_id = data["clickup_id"]
            mapped_type = data["clickup_type"]
            if mapped_type == "list":
                return [mapped_id]
            elif mapped_type == "folder":
                ep = f"/folder/{mapped_id}"
                data_resp, _ = _api("GET", f"{ep}/list")
                return [lst["id"] for lst in (data_resp or {}).get("lists", [])]
            elif mapped_type == "space":
                ids = []
                ep = f"/space/{mapped_id}"
                data_resp, _ = _api("GET", f"{ep}/list")
                ids = [lst["id"] for lst in (data_resp or {}).get("lists", [])]
                f_data, _ = _api("GET", f"{ep}/folder")
                for f in (f_data or {}).get("folders", []):
                    ids.extend([lst["id"] for lst in f.get("lists", [])])
                return ids

    # 3. EXISTING: Try to resolve name to a List ID dynamically
    found_id, _ = _resolve_name_to_list_id(p_name)
    if found_id:
        return [found_id]
    return []
```

### Fix 3: Time Entry Data Source

The current `get_time_tracking_report` uses `task.time_spent` which is the ClickUp rollup.
For accurate data matching Supabase, new tools should use:

**Option A: ClickUp Time Entries API**

```
GET /team/{team_id}/time_entries
  ?start_date={unix_ms}
  &end_date={unix_ms}
  &assignee={user_ids}  (optional)
```

Each entry gives: task_id, user, start, end, duration, billable.

**Option B: Supabase time_entries table**
If time entries are synced to Supabase, query directly for exact match.

---

## Report Stage Flow for AI Client

When user asks for a time tracking report, the AI should follow these stages:

### Stage 1: Identify Report Type

Parse user intent:

- "space wise time report" → `get_space_time_report`
- "Luminique team member report" → `get_folder_team_time_report(project="Luminique")`
- "team member time report" → `get_team_member_time_report`
- "weekly report for 3D Team" → `get_weekly_summary_report`
- "time tracking for <project>" → `get_time_tracking_report(project=...)`

### Stage 2: Resolve Scope

- If project name given → resolve via project_map.json
- If space name given → resolve space ID from hierarchy
- If workspace-wide → use workspace_id

### Stage 3: Determine Period

- "weekly" / "this week" → week="current"
- "last week" → week="last"
- No period mentioned → all-time

### Stage 4: Execute

Call the appropriate tool with resolved parameters.

### Stage 5: Present

Format the results in a clean table format for the user.
