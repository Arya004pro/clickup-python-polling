# ClickUp → PostgreSQL Sync + Local AI Analytics

Real-time sync of ClickUp tasks, time tracking, and comments to PostgreSQL (Supabase) with **local AI-powered analytics** via LM Studio and MCP (Model Context Protocol).

## 🌟 Features

### Data Sync

- **Auto Sync** - Background scheduler syncs all tasks, time, and comments every 45 seconds
- **Time Tracking** - Session-wise `start_times[]` and `end_times[]` arrays
- **Assigned Comments** - Tracks unresolved comments assigned to users
- **Employee Mapping** - Links ClickUp users to database employees
- **Daily Snapshots** - Stores daily task snapshots for reporting/analytics

### Local AI Analytics 🤖

- **54 MCP Tools** - Comprehensive ClickUp data access via Model Context Protocol
- **100% Local & Private** - Runs on your PC using LM Studio
- **Optimized Model** - gemma-3-4b (4B parameters) for medium-heavy workloads
- **No API Costs** - Zero cloud API fees, fully offline capable
- **Executive Reports** - AI-generated insights for CTOs and project managers

## 🚀 Tech Stack

- **Python 3.11+**
- **FastAPI** – REST API
- **psycopg2** – Direct PostgreSQL connection
- **APScheduler** – Background sync jobs
- **Supabase** – PostgreSQL database (Transaction Pooler)
- **FastMCP** – Model Context Protocol server
- **LM Studio** – Local LLM server (gemma-3-4b)

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

# Local AI Client
lmstudio_client.py       # LM Studio local AI client (gemma-3-4b)
```

## 🎯 Quick Start

### 1. Install LM Studio

1. **Download LM Studio**: Visit [lmstudio.ai](https://lmstudio.ai) and download for your OS
2. **Install and Launch** LM Studio
3. **Download gemma-3-4b**:
   - Open LM Studio
   - Go to "Discover" tab
   - Search for "gemma-3-4b"
   - Click download and wait for completion
4. **Load the Model**:
   - Go to "Chat" tab
   - Select gemma-3-4b from model dropdown
   - Click "Start Server" (default: http://localhost:1234)

### 2. Data Sync Setup

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

# Optional: Customize LM Studio settings
LM_STUDIO_BASE_URL=http://localhost:1234/v1
LM_STUDIO_MODEL=gemma-3-4b

# Run API server
uvicorn app.main:app --reload
```

### 3. Start AI Analytics

```bash
# Terminal 1: Start MCP Server
$env:PYTHONPATH = (Get-Location).Path
fastmcp run app/mcp/mcp_server.py:mcp --transport sse --port 8001

# Terminal 2: Run Local AI Client
python lmstudio_client.py
```

### 4. Try AI Analytics

```
You: Show me all tasks in progress across all spaces

You: Generate a weekly report for the CTO showing task distribution,
     time spent, bottlenecks, and team performance

You: Which employee has the most overdue tasks and why?

You: Get time tracking report for Luminique project grouped by assignee
```

## 📊 Local AI Analytics Features

### Why LM Studio + gemma-3-4b?

| Feature               | Benefit                                 |
| --------------------- | --------------------------------------- |
| **100% Local**        | Your data never leaves your PC          |
| **Zero Cost**         | No API fees, unlimited usage            |
| **Privacy**           | No cloud providers, full control        |
| **Optimized for MCP** | 4B params balanced for 54 complex tools |
| **Fast Inference**    | Runs smoothly on modern CPUs/GPUs       |

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
   - Time tracking summaries with nested subtask support
   - Progress metrics and trends
   - Estimation accuracy analysis

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

### System Requirements

| Component   | Minimum      | Recommended                            |
| ----------- | ------------ | -------------------------------------- |
| **RAM**     | 8 GB         | 16 GB+                                 |
| **CPU**     | 4 cores      | 8+ cores                               |
| **GPU**     | Not required | NVIDIA GPU (CUDA) for faster inference |
| **Storage** | 5 GB free    | 10 GB+                                 |
| **OS**      | Windows 10+  | Windows 11                             |

## 📚 Documentation

- **[QUICK_START.md](QUICK_START.md)** - 10-minute setup guide for LM Studio + MCP
- **[LM Studio Setup Guide](https://lmstudio.ai/docs)** - Official LM Studio documentation
- **[MCP Protocol](https://modelcontextprotocol.io/)** - Model Context Protocol specs
- **Local-first AI** - No cloud dependencies, full privacy

## 🔧 Advanced Configuration

### LM Studio Settings (.env)

```bash
# LM Studio API endpoint (default: http://localhost:1234/v1)
LM_STUDIO_BASE_URL=http://localhost:1234/v1

# Model identifier (as shown in LM Studio)
LM_STUDIO_MODEL=gemma-3-4b

# Optional: Adjust inference parameters in LM Studio UI
# - Temperature: 0.7 (creativity vs accuracy)
# - Max tokens: 2048 (response length)
# - Context length: Auto (based on model)
```

### Performance Tips

1. **Enable GPU Acceleration**: In LM Studio settings, enable GPU offloading if you have an NVIDIA GPU
2. **Adjust Thread Count**: Set to match your CPU cores for optimal CPU inference
3. **Monitor Resource Usage**: Keep Task Manager open to monitor RAM/CPU usage
4. **Model Selection**: gemma-3-4b is optimal for 8-16GB RAM systems

### Troubleshooting

**LM Studio not responding?**

- Check if server is running in LM Studio (should show "Server Running" in UI)
- Verify port 1234 is not blocked by firewall
- Restart LM Studio if needed

**Out of memory errors?**

- Close other applications
- Reduce context length in LM Studio settings
- Consider upgrading RAM or using smaller model

**Slow responses?**

- Enable GPU acceleration if available
- Reduce max_tokens in client
- Check system resources aren't maxed out

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
