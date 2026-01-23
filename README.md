# ClickUp → PostgreSQL Sync

Real-time sync of ClickUp tasks, time tracking, and comments to PostgreSQL (Supabase).

## Features

- **Full Sync** - Full sync every 7 min
- **Time Tracking** - Session-wise `start_times[]` and `end_times[]` arrays
- **Assigned Comments** - Tracks unresolved comments assigned to users
- **Employee Mapping** - Links ClickUp users to database employees
- **REST API** - FastAPI endpoints for querying synced data

## Tech Stack

- **Python 3.11+**
- **FastAPI** - REST API
- **psycopg2** - Direct PostgreSQL connection
- **APScheduler** - Background sync jobs
- **Supabase** - PostgreSQL database (Transaction Pooler)

# ClickUp → PostgreSQL Sync

Real-time and daily snapshot sync of ClickUp tasks, time tracking, and comments to PostgreSQL (Supabase).

## Features

- **Auto Sync:** Background scheduler syncs all tasks, time, and comments every 45 seconds.
- **Full Sync:** All tasks are fully synced on each run.
- **Time Tracking:** Session-wise `start_times[]` and `end_times[]` arrays.
- **Assigned Comments:** Tracks unresolved comments assigned to users.
- **Employee Mapping:** Links ClickUp users to database employees.
- **REST API:** FastAPI endpoints for querying synced data.
- **Daily Sync:** Snapshots all tasks updated today into `daily_syncs` for reporting/analytics.

## Tech Stack

- **Python 3.11+**
- **FastAPI** – REST API
- **psycopg2** – Direct PostgreSQL connection
- **APScheduler** – Background sync jobs
- **Supabase** – PostgreSQL database (Transaction Pooler)

## Project Structure

```
app/
├── main.py          # FastAPI app & endpoints
├── clickup.py       # ClickUp API client
├── sync.py          # Task sync logic (main tasks table)
├── daily_sync.py    # Daily snapshot sync for reporting (daily_syncs table)
├── supabase_db.py   # PostgreSQL queries
├── scheduler.py     # Background job scheduler
├── time_tracking.py # Time entry aggregation
├── employee_sync.py # Employee sync
├── config.py        # Environment config
└── logging_config.py
```

## Quick Start

```bash
# Clone & setup

cd clickup-python-polling
python -m venv myenv
myenv\Scripts\activate  # Windows
pip install -r requirements.txt

# Configure .env
CLICKUP_API_TOKEN=pk_xxx
CLICKUP_TEAM_ID=xxx
DATABASE_URL=postgresql://postgres.xxx:password@aws-0-region.pooler.supabase.com:6543/postgres

# Run API server
uvicorn app.main:app --reload
```

## Daily Sync

The daily sync creates a snapshot of all tasks updated today and stores them in the `daily_syncs` table for reporting and analytics.

- **Manual run:**
  ```bash
  python -c "from app.daily_sync import sync_daily_updated_tasks; sync_daily_updated_tasks()"
  ```
- **What it does:**
  - Fetches all tasks updated today.
  - Joins with employees for user mapping.
  - Cleans and normalizes assignee and tag fields.
  - Inserts a fresh snapshot into `daily_syncs` (old data cleared each run).

### `daily_syncs` Table Schema (main columns)

| Column          | Type    | Description                    |
| --------------- | ------- | ------------------------------ |
| clickup_task_id | TEXT    | ClickUp task ID                |
| title           | TEXT    | Task name                      |
| status          | TEXT    | Task status                    |
| assignee_name   | TEXT[]  | Array of assignee names        |
| assigned_by     | TEXT    | Who assigned the task          |
| tracked_minutes | INTEGER | Total tracked time             |
| start_times     | TEXT[]  | Work session start times (ISO) |
| end_times       | TEXT[]  | Work session end times (ISO)   |
| ...             | ...     | (see code for full schema)     |

## API Endpoints

| Endpoint                                  | Description                  |
| ----------------------------------------- | ---------------------------- |
| `GET /employees`                          | List all employees           |
| `GET /tasks`                              | List all tasks (paginated)   |
| `GET /tasks/by-employee?employee_id=uuid` | Tasks for specific employee  |
| `GET /tasks/with-time`                    | Tasks with tracked time      |
| `GET /tasks/with-comments`                | Tasks with assigned comments |
| `GET /tasks/{task_id}`                    | Single task details          |
| `GET /dependencies/{task_id}`             | Get task dependencies        |
| `GET /sync/tasks`                         | Trigger manual full sync     |
| `GET /sync/employees`                     | Sync employees from ClickUp  |

## How It Works

1. **Scheduler** runs every 45 seconds.
2. **Employees** sync first (ClickUp → DB).
3. **Tasks** fetch from all spaces/lists.
4. **Time entries** & **comments** batch-fetched concurrently.
5. **Bulk upsert** to PostgreSQL with `ON CONFLICT`.
6. **Daily sync** can be run anytime for a fresh daily snapshot.

## Time Tracking Format

```json
{
  "start_times": ["2026-01-13T09:30:00+05:30", "2026-01-12T12:00:00+05:30"],
  "end_times": ["2026-01-13T11:00:00+05:30", "2026-01-12T14:00:00+05:30"],
  "tracked_minutes": 210
}
```

- Arrays sorted by **latest first**
- Each index = one work session

## License

MIT
| Column | Type |
| ---------------- | ------------- |
| id | UUID (PK) |
| clickup_task_id | TEXT (unique) |
| title | TEXT |
| status | TEXT |
| assignee_name | TEXT |
| employee_ids | UUID[] |
| start_times | TEXT[] |
| end_times | TEXT[] |
| tracked_minutes | INTEGER |
| assigned_comment | TEXT |
| ... | (30+ fields) |

## Project Structure

```
app/
├── main.py          # FastAPI app & endpoints
├── clickup.py       # ClickUp API client
├── sync.py          # Task sync logic
├── supabase_db.py   # PostgreSQL queries
├── scheduler.py     # Background job scheduler
├── time_tracking.py # Time entry aggregation
├── employee_sync.py # Employee sync
├── config.py        # Environment config
└── logging_config.py
```

## How It Works

1. **Scheduler** runs every 45 seconds
2. **Employees** sync first (ClickUp → DB)
3. **Tasks** fetch from all spaces/lists
4. **Time entries** & **comments** batch-fetched concurrently
5. **Bulk upsert** to PostgreSQL with `ON CONFLICT`
6. **Incremental sync** only fetches recently updated tasks

## Time Tracking Format

```json
{
  "start_times": ["2026-01-13T09:30:00+05:30", "2026-01-12T12:00:00+05:30"],
  "end_times": ["2026-01-13T11:00:00+05:30", "2026-01-12T14:00:00+05:30"],
  "tracked_minutes": 210
}
```

- Arrays sorted by **latest first**
- Each index = one work session

---

## MCP Server (Model Context Protocol)

This project also provides an **MCP Server** for programmatic, tool-based access to ClickUp workspace and space management, using the [FastMCP](https://github.com/ariya002/fastmcp) protocol.

### Running the MCP Server

```bash
python -m app.mcp.mcp_server --reload
```

The MCP server exposes tools for listing workspaces, spaces, and fetching space details. These tools can be called via HTTP POST requests (JSON-RPC) or from compatible clients.

#### Example: Call a Tool via PowerShell

```powershell
$headers = @{ "Content-Type" = "application/json"; "Accept" = "application/json, text/event-stream" }
$body = '{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "get_workspaces",
    "arguments": {}
  },
  "id": 1
}'
Invoke-RestMethod -Uri http://localhost:8001/mcp -Method Post -Headers $headers -Body $body
```

#### Available MCP Tools

- `get_workspaces`: List all accessible ClickUp workspaces (teams)
- `get_spaces`: List all spaces in a workspace (provide `workspace_id` or leave blank for default)
- `get_space`: Get details for a specific space by `space_id`

See [app/mcp/workspace_structure.py](app/mcp/workspace_structure.py) for tool definitions and arguments.

---

## License

MIT
