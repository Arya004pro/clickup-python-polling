# ClickUp MCP Analytics Server

> A full-stack backend system that syncs ClickUp project management data into a PostgreSQL database and exposes it through an MCP (Model Context Protocol) server — enabling a locally-hosted AI model (Qwen2.5-7B via LM Studio) to answer project management questions, generate reports, and provide team analytics through natural language.

<div align="center">

![Python](https://img.shields.io/badge/python-3.11.9+-blue.svg)
![FastMCP](https://img.shields.io/badge/MCP-FastMCP%203.0-green.svg)
![LM Studio](https://img.shields.io/badge/AI-LM%20Studio%20%2B%20Qwen2.5--7B-purple.svg)
![Database](https://img.shields.io/badge/database-PostgreSQL%20%28Supabase%29-blue.svg)

</div>

---

## Table of Contents

- [Project Overview](#project-overview)
- [System Architecture](#system-architecture)
- [Technology Stack](#technology-stack)
- [Development Phases](#development-phases)
- [Project Structure](#project-structure)
- [MCP Tools Reference](#mcp-tools-reference)
- [Database Schema](#database-schema)
- [Setup & Running](#setup--running)
- [Environment Variables](#environment-variables)

---

## Project Overview

This project solves a real problem: **ClickUp contains rich project data, but extracting meaningful reports and insights from it is tedious**. This system automates the entire pipeline:

1. **Pulls all ClickUp data** (tasks, time logs, employees, spaces, folders, lists) via the ClickUp REST API
2. **Stores it in a PostgreSQL database** (hosted on Supabase) with a well-designed schema
3. **Exposes analytics through an MCP server** with 54 specialized tools
4. **Connects the MCP server to LM Studio** running Qwen2.5-7B Q4_K_M locally so the AI can call tools and generate human-readable reports on demand

The AI does not guess — it calls real tools, fetches real data, and produces real reports.

---

## System Architecture

```

                  LM Studio  (Local)                          
              Qwen2.5-7B Q4_K_M Model                         
         Connected via MCP client (SSE transport)             

                        Tool calls (SSE / HTTP)
                       

              MCP Server  (mcp_server.py)                     
                   FastMCP 3.0  |  Port 8001                  
                                                              
        
     Workspace            Task           PM Analytics  
     Structure          Management                     
     (10 tools)         (12 tools)        (12 tools)   
        
        
      Project            Project          Sync &       
   Configuration       Intelligence       Mapping      
     (7 tools)          (12 tools)        (10 tools)   
        

                                  
                                  
    
     ClickUp REST API     PostgreSQL Database   
     (Real-time data)     (Supabase-hosted)     
    


              Sync Backend  (FastAPI + APScheduler)           
                                                              
   main.py    scheduler.py    clickup.py                
                                                             
                                                             
                               sync.py    supabase_db.py   

```

The system has two independently running processes:
- **Sync Backend** — FastAPI app that polls ClickUp and writes to PostgreSQL
- **MCP Server** — FastMCP server that the AI model connects to via LM Studio

---

## Technology Stack

| Layer | Technology | Purpose |
|---|---|---|
| **AI Model** | Qwen2.5-7B-Instruct Q4_K_M | Local LLM for report generation and tool calling |
| **AI Runtime** | LM Studio | Local model hosting with built-in MCP client support |
| **MCP Framework** | FastMCP 3.0 | MCP server with tool registration and SSE transport |
| **API Framework** | FastAPI + Uvicorn | Sync backend REST endpoints |
| **Scheduler** | APScheduler 3.x | Automated daily data sync |
| **Database** | PostgreSQL (Supabase) | Persistent storage for tasks, time logs, employees |
| **DB Driver** | psycopg2-binary | Direct PostgreSQL connection with connection pooling |
| **ClickUp API** | ClickUp REST API v2 | Source of all project management data |
| **HTTP Client** | requests + urllib3 | Connection pooling, retry logic, rate limiting |
| **Language** | Python 3.11.9+ | Backend runtime |

---

## Development Phases

### Phase 1 — Data Sync Foundation

**Goal:** Connect ClickUp to a persistent database with a normalized schema.

- Integrated the ClickUp REST API v2 with full authentication and error handling
- Designed the PostgreSQL schema to store:
  - `tasks` — full task data including status, priority, assignees, time estimates, tags, description
  - `employees` — team member records mapped to ClickUp user IDs
  - `time_logs` — per-task time tracking entries with durations and user attribution
  - Location metadata — space, folder, list names stored directly on each task row
- Built the `clickup.py` API client with:
  - Enterprise-grade token-bucket rate limiter (1000 req/min)
  - Connection pooling via `requests.Session` with `HTTPAdapter` (250 pool connections)
  - Concurrent task fetching across all lists using `ThreadPoolExecutor`
  - Automatic retry with exponential backoff for transient errors (429, 5xx)
- Built `supabase_db.py` as the database layer with context-managed connections, auto-commit, retry logic, and `execute_batch` for efficient bulk writes
- Implemented secure API handling — all credentials loaded from `.env`, fail-fast validation on startup

---

### Phase 2 — Automated Daily Sync

**Goal:** Keep the database continuously up-to-date without manual intervention.

- Built `sync.py` to transform raw ClickUp API responses into normalized database rows:
  - Resolves ClickUp user IDs to internal employee UUIDs
  - Aggregates time entries per task using `time_tracking.py`
  - Stores location context (space/folder/list names) on each task
  - Handles task deletion with a soft-delete `is_deleted` flag
- Built `scheduler.py` using APScheduler with:
  - Full sync on first startup to populate the entire database
  - Incremental sync on subsequent runs using `date_updated` timestamps with a 2-minute overlap buffer
  - Concurrency guard — skips if the previous sync is still running
  - IST timezone-aware scheduling
- Built `daily_sync.py` to populate a `daily_syncs` snapshot table for daily activity tracking
- Exposed manual sync REST endpoints through `main.py` (FastAPI):
  - `GET /sync/tasks` — trigger full task sync
  - `GET /sync/employees` — sync employee records from ClickUp
  - `GET /sync/daily` — populate the daily snapshot table
- Added structured logging via `logging_config.py` with per-module log propagation

---

### Phase 3 — MCP Analytics Server

**Goal:** Expose the database and ClickUp API through a structured tool interface that an AI model can call.

- Built `mcp_server.py` using **FastMCP 3.0** running on `http://0.0.0.0:8001` over SSE transport
- Registered 54 tools across 6 modules — all tools return structured JSON
- Module breakdown:

  **`workspace_structure.py`** — Workspace browsing tools
  - Navigate the full ClickUp hierarchy: workspace  spaces  folders  lists
  - Resolve entity names to IDs dynamically
  - Cache invalidation support
  - Self-contained — operates directly against the live ClickUp API, independent of the sync backend

  **`task_management.py`** — Task operations
  - Fetch tasks with filters (status, assignee, date, list)
  - Create and update tasks via the ClickUp API
  - Search across a project
  - Workload distribution and overdue task detection
  - Bottom-up time summation with cross-list parent task resolution

  **`pm_analytics.py`** — Analytics and reporting engine
  - Core analytics engine powering all time-based reports
  - Space-level and folder-level project time reports
  - Employee-wise time breakdown per project
  - Weekly, monthly, and date-range timesheets
  - Status distribution and at-risk task detection
  - Concurrent time entry fetching using up to 300 worker threads per batch
  - Centralized `_calculate_task_metrics()` for accurate bottom-up time sums — nested subtask time is never double-counted with parent task time

  **`project_configuration.py`** — Project tracking registry
  - In-memory + persistent (JSON file) project mapping layer
  - Discover and register ClickUp lists/folders as tracked projects with custom aliases
  - Real-time health status per tracked project

  **`project_intelligence.py`** — AI-focused project insights
  - Project health score (A–F letter grade)
  - Daily standup report generation
  - Weekly project digest
  - Blocker identification
  - Team workload analysis per project
  - Risk assessment with reasoning

  **`sync_mapping.py`** — Data mapping and caching layer
  - Source-of-truth registry for tracked projects (persisted to `project_map.json`)
  - TTL cache (1 hour) for expensive structure queries
  - Bulk space/folder/list discovery

- Configured Uvicorn with 5-minute keep-alive timeouts to support long-running tool calls (time reports over large spaces can take 30–60 seconds)
- Built `status_helpers.py` as a shared utility module:
  - Unified status category mapping across all modules (not_started / active / done / closed)
  - Custom status name overrides for non-standard ClickUp status names (e.g. "SHIPPED"  done, "SCOPING"  active)
  - Date/timestamp helpers (YYYY-MM-DD  Unix milliseconds)
  - Week validation and current-week detection
  - Workspace member resolution

---

### Phase 4 — Report Generation

**Goal:** Produce structured, accurate analytics reports through natural language queries.

Reports implemented and validated end-to-end:

| Report | Description |
|---|---|
| **Space-wise project time report** | Total tracked hours per project within a ClickUp space, broken down by task and assignee |
| **Employee-wise time report** | Hours logged by each team member across all projects in a space or folder |
| **Weekly timesheet** | Per-employee time log for a Monday–Sunday week with daily breakdown |
| **Monthly timesheet** | Per-employee time log for a given month |
| **Yesterday's report** | Quick daily summary of what was tracked the previous day |
| **Folder-level time report** | Time breakdown for all lists within a specific folder |
| **Project health score** | A–F grade based on tracked vs estimated time, completion rate, and blocker count |
| **Daily standup** | Structured standup summary: what was done, what is in progress, what is blocked |
| **Weekly digest** | High-level weekly summary of progress, blockers, and completion rate |

Key design decisions in report accuracy:
- All time values use **bottom-up summation** — subtask time is not double-counted in parent task totals
- **Cross-list parent tasks** are fetched dynamically to ensure time rollups are accurate even when parent and child tasks live in different lists
- Time entries for **tasks shared between multiple assignees** are attributed individually to each person — not split equally
- All outputs are **structured JSON**, formatted for immediate AI summarization and human readability

---

### Phase 5 — Current State (Local Deployment)

**Goal:** Run the full system locally with a locally-hosted AI model.

Current running setup:
- **MCP server** runs locally at `http://localhost:8001`
- **LM Studio** is configured with the MCP server URL and connects via SSE transport
- **Model in use:** Qwen2.5-7B-Instruct Q4_K_M (4-bit quantized, runs on consumer GPU or CPU)
- The model is guided by a detailed system prompt that describes the workspace structure, available tools, expected output format, and reporting conventions
- Report generation workflows are tested and validated for accuracy against real ClickUp data
- The workspace browser (`workspace_structure.py`) operates **independently** from the sync backend — it queries the live ClickUp API directly, so it always reflects the current workspace state even if the database sync is behind

---

## Project Structure

```
clickup-python-polling/

 app/
    main.py              # FastAPI app — manual sync REST endpoints
    config.py            # Environment variable loading and validation
    clickup.py           # ClickUp API client (rate limiter, pooling, pagination)
    supabase_db.py       # PostgreSQL database layer (CRUD, bulk upsert)
    sync.py              # ClickUp  DB transformation and upsert logic
    daily_sync.py        # Daily snapshot sync to daily_syncs table
    scheduler.py         # APScheduler — automated sync jobs
    employee_sync.py     # Employee record sync from ClickUp workspace members
    time_tracking.py     # Time entry aggregation per task
    logging_config.py    # Structured logging setup

    mcp/
        mcp_server.py            # FastMCP server entry point (port 8001)
        workspace_structure.py   # Tools: browse workspace hierarchy
        task_management.py       # Tools: task CRUD, filters, workload
        pm_analytics.py          # Tools: time reports, timesheets, analytics
        project_configuration.py # Tools: project registry management
        project_intelligence.py  # Tools: health scores, standups, digests
        sync_mapping.py          # Tools: project mapping, cache management
        status_helpers.py        # Shared: status logic, date utils, member lookup
        time_stamp_helpers.py    # Shared: timestamp conversion utilities

 project_map.json         # Persistent storage for tracked project mappings
 requirements.txt         # Python dependencies
 README.md
```

---

## MCP Tools Reference

### Workspace Structure (10 tools)

| Tool | Description |
|---|---|
| `get_workspaces` | List all ClickUp workspaces accessible via the API token |
| `get_spaces` | List all spaces in the workspace |
| `get_space` | Get detailed info for a specific space |
| `get_folders` | List all folders in a space |
| `get_folder` | Get folder details including its lists |
| `get_lists` | List all lists in a folder |
| `get_folderless_lists` | List direct-space lists not inside any folder |
| `get_list` | Get detailed list information |
| `get_workspace_members` | List all workspace members |
| `invalidate_cache` | Clear locally cached workspace structure data |

### Task Management (12 tools)

| Tool | Description |
|---|---|
| `get_tasks` | Fetch tasks with filters (status, assignee, list, date range) |
| `get_task` | Get full details of a single task including subtasks |
| `create_task` | Create a new task in a specified list |
| `update_task` | Update task fields (status, assignee, priority, due date) |
| `search_tasks` | Search tasks by keyword across a project |
| `get_project_tasks` | Fetch all tasks from a tracked project |
| `get_list_progress` | Sprint/list completion progress summary |
| `get_workload` | Team member workload distribution |
| `get_overdue_tasks` | List tasks past their due date |
| `get_task_hierarchy` | Get parent-child task tree |
| `get_tasks_by_assignee` | Filter tasks by team member name |
| `get_tasks_by_status` | Filter tasks by status category |

### PM Analytics (12 tools)

| Tool | Description |
|---|---|
| `get_space_project_time_report` | Full time report for all projects in a space |
| `get_folder_time_report` | Time report scoped to a specific folder |
| `get_employee_time_report` | Per-employee time breakdown across projects |
| `get_weekly_timesheet` | Weekly timesheet for a Monday–Sunday range |
| `get_monthly_timesheet` | Monthly timesheet broken down by employee |
| `get_yesterday_report` | Quick summary of yesterday's tracked time |
| `get_progress_since` | Tasks completed and time logged since a given date |
| `get_time_tracking_report` | Time tracking summary with estimates vs actuals |
| `get_task_time_breakdown` | Detailed time log per individual task |
| `get_estimation_accuracy` | Estimate vs actual time analysis across a project |
| `get_at_risk_tasks` | Tasks with significant time overrun risk |
| `get_untracked_tasks` | Tasks assigned but with no time logged |

### Project Configuration (7 tools)

| Tool | Description |
|---|---|
| `discover_projects` | Scan a space and list all discoverable projects |
| `add_project` | Register a ClickUp list or folder as a tracked project with an alias |
| `list_projects` | List all currently tracked projects |
| `remove_project` | Stop tracking a project by alias |
| `refresh_projects` | Re-validate all tracked projects against live ClickUp data |
| `get_project_status` | Current status metrics for a specific tracked project |
| `get_all_projects_status` | Summary dashboard across all tracked projects |

### Project Intelligence (12 tools)

| Tool | Description |
|---|---|
| `get_project_health_score` | A–F health grade based on time, completion, and blockers |
| `get_project_daily_standup` | Structured standup report for a project |
| `get_project_weekly_digest` | Weekly progress digest |
| `get_project_blockers` | Tasks currently blocked or blocking others |
| `get_project_at_risk` | Risk assessment with per-task reasoning |
| `get_project_team_workload` | Team member workload within a project |
| `get_project_time_tracking` | Aggregated time report at the project level |
| `get_workspace_folderless_lists` | Discover all lists not inside any folder |
| `get_list_defined_statuses` | Full status workflow definitions for a list |
| `get_project_statuses` | All statuses used across a project |
| `get_project_completion_rate` | Completed tasks vs total tasks ratio |
| `get_project_velocity` | Weekly task completion velocity trend |

### Sync & Mapping (10 tools)

| Tool | Description |
|---|---|
| `map_project` | Map a ClickUp list or folder to a named alias |
| `unmap_project` | Remove a project mapping |
| `list_mappings` | List all current project mappings |
| `get_mapping` | Get details for a specific mapping |
| `discover_space` | Discover all lists in a space with metadata |
| `discover_folder` | Discover all lists within a folder |
| `cache_status` | Check what structure data is currently cached |
| `clear_cache` | Wipe all cached structure data |
| `refresh_mapping` | Re-sync a specific project mapping |
| `export_mappings` | Export all mappings as a JSON file |

---

## Database Schema

### `tasks`
Stores all ClickUp tasks with flattened location and assignment metadata.

| Column | Type | Description |
|---|---|---|
| `id` | UUID | Internal primary key |
| `clickup_task_id` | VARCHAR | ClickUp task ID |
| `title` | TEXT | Task name |
| `description` | TEXT | Task description |
| `status` | VARCHAR | Current status name |
| `status_type` | VARCHAR | ClickUp status type (open / custom / done / closed) |
| `priority` | VARCHAR | Task priority level |
| `assignee_name` | TEXT[] | Array of assigned member display names |
| `employee_id` | UUID | FK to employees table |
| `tracked_minutes` | INTEGER | Total tracked time in minutes |
| `time_estimate_minutes` | INTEGER | Estimated time in minutes |
| `start_date` | DATE | Task start date |
| `due_date` | DATE | Task due date |
| `date_created` | TIMESTAMPTZ | Task creation timestamp (IST) |
| `date_closed` | TIMESTAMPTZ | Task closed/completed timestamp (IST) |
| `last_status_change` | TIMESTAMPTZ | Most recent status transition timestamp |
| `space_name` | VARCHAR | ClickUp space the task belongs to |
| `folder_name` | VARCHAR | Folder the task belongs to |
| `list_name` | VARCHAR | List the task belongs to |
| `tags` | TEXT[] | Task tags |
| `sprint_points` | INTEGER | Sprint points if assigned |
| `type` | VARCHAR | Task type (task / subtask / milestone) |
| `is_deleted` | BOOLEAN | Soft-delete flag |

### `employees`
Team members synced from ClickUp workspace.

| Column | Type | Description |
|---|---|---|
| `id` | UUID | Internal primary key |
| `clickup_user_id` | BIGINT | ClickUp user ID |
| `name` | VARCHAR | Display name |
| `email` | VARCHAR | Email address |
| `role` | VARCHAR | Workspace role |

### `daily_syncs`
Point-in-time snapshot of tasks updated on a given day. Used for daily activity reports and monitoring.

---

## Setup & Running

### Prerequisites
- Python 3.11.9+
- PostgreSQL database (Supabase or self-hosted)
- ClickUp account with a personal API token
- LM Studio with Qwen2.5-7B-Instruct Q4_K_M loaded

### Installation

```bash
git clone https://github.com/Arya004pro/clickup-python-polling.git
cd clickup-python-polling

python -m venv myenv
myenv\Scripts\activate

pip install -r requirements.txt
```

### Running the Sync Backend

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

First run triggers a full sync of all tasks from all spaces. Subsequent runs use incremental sync.

### Running the MCP Server

```bash
python -m app.mcp.mcp_server
```

The server starts at `http://localhost:8001` over SSE transport.

### Connecting LM Studio

1. Open LM Studio and load **Qwen2.5-7B-Instruct Q4_K_M**
2. Navigate to the MCP / Agent Tools settings
3. Add MCP server URL: `http://localhost:8001/sse`
4. Start a new chat session — the model will automatically discover all 54 tools and use them to answer queries

---

## Environment Variables

Create a `.env` file at the project root:

```env
# ClickUp
CLICKUP_API_TOKEN=pk_xxxxxxxxxxxxxxxxxxxx
CLICKUP_TEAM_ID=your_team_id        # Optional — auto-detected if not set
CLICKUP_SPACE_ID=your_space_id      # Optional — for default space scoping

# PostgreSQL (Supabase or self-hosted)
DATABASE_URL=postgresql://user:password@host:5432/dbname
```
