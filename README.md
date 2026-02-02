# ClickUp → PostgreSQL Sync + MCP AI Analytics

Real-time sync of ClickUp tasks, time tracking, and comments to PostgreSQL (Supabase) with AI-powered analytics via MCP (Model Context Protocol).

## 🌟 Features

### Data Sync

- **Auto Sync** - Background scheduler syncs all tasks, time, and comments every 45 seconds
- **Time Tracking** - Session-wise `start_times[]` and `end_times[]` arrays
- **Assigned Comments** - Tracks unresolved comments assigned to users
- **Employee Mapping** - Links ClickUp users to database employees
- **Daily Snapshots** - Stores daily task snapshots for reporting/analytics

### AI Analytics (NEW!)

- **54 MCP Tools** - Comprehensive ClickUp data access via Model Context Protocol
- **Multi-Model Support** - Works with Gemini, OpenRouter, Groq, local LLMs
- **Large Context Windows** - Up to 1M-2M tokens for complex multi-turn analysis
- **Executive Reports** - AI-generated insights for CTOs and project managers

## 🚀 Tech Stack

- **Python 3.11+**
- **FastAPI** – REST API
- **psycopg2** – Direct PostgreSQL connection
- **APScheduler** – Background sync jobs
- **Supabase** – PostgreSQL database (Transaction Pooler)
- **FastMCP** – Model Context Protocol server
- **Gemini/OpenRouter** – AI models for analytics

## 📂 Project Structure

```
app/
├── main.py              # FastAPI app & endpoints
├── clickup.py           # ClickUp API client
├── sync.py              # Task sync logic (main tasks table)
├── daily_sync.py        # Daily snapshot sync for reporting
├── supabase_db.py       # PostgreSQL queries
├── scheduler.py         # Background job scheduler
├── time_tracking.py     # Time entry aggregation
├── employee_sync.py     # Employee sync
├── config.py            # Environment config
├── logging_config.py    # Logging configuration
└── mcp/
    ├── mcp_server.py           # MCP server (54 tools)
    ├── workspace_structure.py  # Workspace/Space/Folder/List tools
    ├── task_management.py      # Task query and management tools
    ├── pm_analytics.py         # Project manager analytics tools
    ├── project_intelligence.py # AI insights and pattern detection
    ├── project_configuration.py # Configuration access tools
    └── sync_mapping.py         # Database sync tools

# AI Clients
gemini_client.py         # Gemini 2.0 Flash client (1M context)
openrouter_client.py     # Multi-model client (RECOMMENDED)
slm_client_groq_backup.py # Groq backup client
```

## 🎯 Quick Start

### 1. Data Sync Setup

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

### 2. AI Analytics Setup (NEW!)

```bash
# Terminal 1: Start MCP Server
python -m app.mcp.mcp_server

# Terminal 2: Get FREE API key from OpenRouter
# Visit: https://openrouter.ai/ (no credit card required)

# Add to .env
OPENROUTER_API_KEY=sk-or-v1-your-key-here

# Run AI client
python openrouter_client.py
```

### 3. Try AI Analytics

```
You: Show me all tasks in progress across all spaces

You: Generate a weekly report for the CTO showing task distribution,
     time spent, bottlenecks, and team performance

You: Which employee has the most overdue tasks and why?
```

## 📊 AI Analytics Features

### Available Capabilities (54 Tools)

1. **Workspace Structure** (9 tools)
   - Get workspaces, spaces, folders, lists
   - Navigate hierarchies
   - Search by name or ID

2. **Task Management** (9 tools)
   - Query tasks by status, assignee, dates
   - Filter by tags, priorities, custom fields
   - Get task details and relationships

3. **PM Analytics** (9 tools)
   - Task distribution reports
   - Time tracking summaries
   - Progress metrics and trends

4. **Project Intelligence** (10 tools)
   - Bottleneck detection
   - Employee performance analysis
   - Pattern recognition
   - Risk assessment

5. **Configuration** (7 tools)
   - Custom fields access
   - Status and tag management
   - Priority settings

6. **Sync Mapping** (10 tools)
   - Database queries
   - Cross-reference ClickUp ↔ DB
   - Historical data access

### Model Options

| Option                      | Context     | Cost           | Best For                       |
| --------------------------- | ----------- | -------------- | ------------------------------ |
| **OpenRouter (Gemini 2.0)** | 1M tokens   | FREE           | ✅ Complex reports, multi-turn |
| **Gemini Direct**           | 1M tokens   | FREE (limited) | Daily quota limits             |
| **OpenRouter (Llama 3.3)**  | 128K tokens | FREE           | Quick queries                  |
| **Local Ollama**            | Varies      | FREE           | Privacy, offline               |

## 📚 Documentation

- **[IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md)** - Complete setup guide
- **[GEMINI_SETUP.md](GEMINI_SETUP.md)** - Gemini-specific setup
- **[QUOTA_SOLUTIONS.md](QUOTA_SOLUTIONS.md)** - Troubleshooting guide

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
