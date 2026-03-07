# ClickUp MCP Server with OpenRouter AI Client

> **Transform ClickUp project management with natural language AI** - A production-ready Model Context Protocol (MCP) server with **81 specialized tools**, connected to OpenRouter-hosted LLMs via API.

<div align="center">

![Python Version](https://img.shields.io/badge/python-3.11.9+-blue.svg)
![MCP Protocol](https://img.shields.io/badge/MCP-FastMCP%203.0-green.svg)
![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Tools](https://img.shields.io/badge/tools-81-orange.svg)

</div>

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Model Configuration](#model-configuration)
- [Features](#features)
- [Quick Start Guide](#quick-start-guide)
- [Detailed Setup Instructions](#detailed-setup-instructions)
- [AI Client Usage](#ai-client-usage)
- [Configuration Reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)

---

## Overview

This project provides a comprehensive **Model Context Protocol (MCP) Server** that bridges your ClickUp workspace with AI language models. The MCP server runs as a persistent HTTP service and exposes **81 ClickUp tools** via SSE transport. The OpenRouter AI client connects to the server, discovers available tools, and calls them on demand using natural language.

### What is MCP?

The Model Context Protocol (MCP) is an open standard that enables AI models to interact with external tools and data sources in a structured, secure way. This server exposes ClickUp-related tools; the AI client connects to the backend over HTTP/SSE, discovers all 81 available tools, and calls them automatically based on the user's query.

---

## Architecture

```
+------------------------------------------------------------------+
|                      USER (Natural Language Query)               |
|           "Show weekly time report for Development team"         |
+-----------------------------------+------------------------------+
                                    |
                                    v
+------------------------------------------------------------------+
|                AI CLIENT (openrouter_client.py)                  |
|                                                                  |
|  +------------------------------------------------------------+  |
|  |             OpenRouter API (OpenAI-compatible)            |  |
|  |                                                            |  |
|  |  Model chain from env: OPENROUTER_MODEL(_CHAIN)           |  |
|  |  Example: qwen/qwen-2.5-7b-instruct -> backup model        |  |
|  +------------------------------------------------------------+  |
|                                                                  |
|  +----------------------+   +--------------------------------+  |
|  |     MCP Session      |   |     Smart Polling Engine       |  |
|  |    (SSE Transport)   |   |   (3s status check interval)   |  |
|  +----------------------+   +--------------------------------+  |
+-----------------------------------+------------------------------+
                                    |
                           SSE Transport (HTTP)
                     http://127.0.0.1:8001/sse
                                    |
                                    v
+------------------------------------------------------------------+
|              MCP SERVER (clickup_mcp/mcp_server.py)              |
|              Uvicorn + FastMCP 3.0 + Port 8001                   |
|                                                                  |
|  +--------------+ +-----------+ +-------------+ +-----------+   |
|  |  Workspace   | |   Task    | | PM Analytics| |  Project  |   |
|  |  Structure   | | Management| | (23 tools)  | |  Config   |   |
|  |  (11 tools)  | | (9 tools) | |             | | (7 tools) |   |
|  +--------------+ +-----------+ +-------------+ +-----------+   |
|                                                                  |
|  +--------------+ +------------+ +----------------------+       |
|  |   Project    | |  Sync &    | |    Task Reports      |       |
|  | Intelligence | |  Mapping   | |    (8 tools)         |       |
|  |  (10 tools)  | | (13 tools) | |                      |       |
|  +--------------+ +------------+ +----------------------+       |
+--------------------+---------------------------------------------+
                     |
         +-----------+-----------+
         v           v           v
   +---------+  +---------+  +---------+
   | ClickUp |  | Supabase|  |  Local  |
   |   API   |  |Postgres |  |  Cache  |
   +---------+  +---------+  +---------+
```

### How the MCP-to-LLM Connection Works

1. **MCP Server starts** as a persistent HTTP service on port `8001`, exposing 81 tools via SSE.
2. **AI Client connects** to `http://127.0.0.1:8001/sse` using the MCP client session.
3. **Tools are discovered** - the client loads all 81 tool schemas from the server.
4. **User sends a query** - the client forwards it to OpenRouter via OpenAI-compatible API, attaching all tool schemas.
5. **LLM selects tools** - it returns a structured tool-call request with the right arguments.
6. **Client executes the tool** - the MCP session calls the tool on the backend server, which queries the ClickUp API or Supabase.
7. **Result returned to LLM** - the LLM produces the final natural-language response for the user.

---

## Model Configuration

OpenRouter is the only runtime provider in this repository.

| Variable                 | Purpose                                                      |
| ------------------------ | ------------------------------------------------------------ |
| `OPENROUTER_MODEL`       | Primary model when `OPENROUTER_MODEL_CHAIN` is not set       |
| `OPENROUTER_MODEL_CHAIN` | Comma-separated model fallback chain                         |
| `OPENROUTER_API_KEY`     | OpenRouter API key                                           |
| `OPENROUTER_BASE_URL`    | OpenRouter base URL (default `https://openrouter.ai/api/v1`) |

Example fallback chain:

```
OPENROUTER_MODEL_CHAIN=qwen/qwen-2.5-7b-instruct,meta-llama/llama-3.1-8b-instruct
```

---

## Features

### 81 MCP Tools in 7 Categories

<details>
<summary><b>Workspace Structure (11 tools)</b></summary>

| Tool                   | Description                              |
| ---------------------- | ---------------------------------------- |
| `get_workspaces`       | List all accessible workspaces (teams)   |
| `get_spaces`           | List spaces in a workspace               |
| `get_space`            | Get detailed space information           |
| `get_folders`          | List folders in a space                  |
| `get_folder`           | Get folder details with lists            |
| `get_lists`            | List all lists in a folder               |
| `get_folderless_lists` | List direct space lists (no folder)      |
| `get_list`             | Get detailed list information            |
| `invalidate_cache`     | Clear cached workspace data              |
| `get_team_members`     | Fetch all team members in a workspace    |
| `resolve_assignees`    | Resolve names/emails to ClickUp user IDs |

</details>

<details>
<summary><b>Task Management (9 tools)</b></summary>

| Tool                | Description                                      |
| ------------------- | ------------------------------------------------ |
| `get_tasks`         | List tasks with filters (status, assignee, date) |
| `get_task`          | Get detailed task info including subtasks        |
| `create_task`       | Create a new task in a list                      |
| `update_task`       | Update task properties (status, assignee, dates) |
| `search_tasks`      | Full-text search across projects                 |
| `get_project_tasks` | Get all tasks in a project with subtask tree     |
| `get_list_progress` | Sprint/list progress summary                     |
| `get_workload`      | Team workload distribution across assignees      |
| `get_overdue_tasks` | Find all overdue tasks in a list                 |

</details>

<details>
<summary><b>PM Analytics (23 tools)</b></summary>

| Tool                                   | Description                                        |
| -------------------------------------- | -------------------------------------------------- |
| `get_progress_since`                   | Progress made since a specified date               |
| `get_time_tracking_report`             | Time tracking summary by list/project              |
| `get_task_time_breakdown`              | Detailed time breakdown for a single task          |
| `get_project_report_universal`         | Universal project report with configurable filters |
| `get_estimation_accuracy`              | Estimate vs actual time analysis (bottom-up)       |
| `get_at_risk_tasks`                    | Find tasks at risk of missing deadline             |
| `get_stale_tasks`                      | Find stagnant tasks with no recent activity        |
| `get_untracked_tasks`                  | Tasks with zero time logged                        |
| `get_inactive_assignees`               | Assignees with no recent activity                  |
| `get_status_summary`                   | Status distribution across a project               |
| `get_space_time_report`                | Time report aggregated at space level              |
| `get_space_folder_team_report`         | Space-folder-team combined time breakdown          |
| `get_time_report_by_period`            | Time report filtered by period (week/month/custom) |
| `get_async_report_status`              | Check status of a background report job            |
| `get_async_report_result`              | Fetch result of a completed background report job  |
| `get_space_time_report_by_period`      | Space-level time report for a specific period      |
| `get_space_project_time_report`        | Time report by space -> project -> member          |
| `get_time_report_detailed`             | Granular time report with full task details        |
| `get_person_tasks_with_time`           | All tasks + time tracked for a specific person     |
| `get_task_status_distribution`         | Status distribution with percentage breakdown      |
| `get_space_time_report_comprehensive`  | Full space-level comprehensive time report         |
| `get_folder_time_report_comprehensive` | Full folder-level comprehensive time report        |
| `get_employee_daily_time_report`       | Employee time report grouped by day                |

</details>

<details>
<summary><b>Project Configuration (7 tools)</b></summary>

| Tool                      | Description                                 |
| ------------------------- | ------------------------------------------- |
| `discover_projects`       | Scan workspace and auto-discover projects   |
| `add_project`             | Add a project to the tracked list           |
| `list_projects`           | List all currently tracked projects         |
| `remove_project`          | Remove a project from tracking              |
| `refresh_projects`        | Re-verify all tracked projects are valid    |
| `get_project_status`      | Project status metrics and health           |
| `get_all_projects_status` | Overview of health for all tracked projects |

</details>

<details>
<summary><b>Project Intelligence (10 tools)</b></summary>

| Tool                             | Description                                |
| -------------------------------- | ------------------------------------------ |
| `get_workspace_folderless_lists` | Find lists not inside any folder           |
| `get_list_defined_statuses`      | Get all status definitions for a list      |
| `get_project_statuses`           | Get project-level status workflow          |
| `get_project_health_score`       | Health score graded A-F with breakdown     |
| `get_project_daily_standup`      | Auto-generated daily standup report        |
| `get_project_time_tracking`      | Project-level time tracking summary        |
| `get_project_blockers`           | Identify blocked or stale tasks            |
| `get_project_at_risk`            | Risk assessment for tasks nearing deadline |
| `get_project_weekly_digest`      | Auto-generated weekly project summary      |
| `get_project_team_workload`      | Per-member workload analysis               |

</details>

<details>
<summary><b>Sync and Mapping (13 tools)</b></summary>

| Tool                          | Description                                          |
| ----------------------------- | ---------------------------------------------------- |
| `discover_hierarchy`          | Traverse full workspace tree (spaces/folders/lists)  |
| `map_project`                 | Map a ClickUp entity (space/folder/list) by alias    |
| `list_mapped_projects`        | Show all current project mappings                    |
| `get_mapped_project`          | Get details of a specific mapped project             |
| `refresh_project`             | Refresh a mapping with latest ClickUp data           |
| `unmap_project`               | Remove a project mapping by alias                    |
| `get_sync_status`             | Check overall sync health and cache state            |
| `list_spaces`                 | List all spaces with mapping metadata                |
| `clear_sync`                  | Reset all mappings and cache (confirm required)      |
| `prune_cache`                 | Clean expired cache entries                          |
| `find_project_anywhere`       | Universal entity search by name across workspace     |
| `get_environment_context`     | Bootstrap - returns server state, mappings, guidance |
| `trigger_mapping_maintenance` | Manually run the mapping maintenance routine         |

</details>

<details>
<summary><b>Task Reports (8 tools)</b></summary>

| Tool                            | Description                                         |
| ------------------------------- | --------------------------------------------------- |
| `get_space_task_report`         | Space-wide task report with per-project breakdown   |
| `get_project_task_report`       | Project report with per-team-member breakdown       |
| `get_member_task_report`        | Individual team member task and time report         |
| `get_low_hours_report`          | Members who tracked less than 8h on any working day |
| `get_missing_estimation_report` | Tasks without time estimates, grouped by assignee   |
| `get_overtracked_report`        | Members where tracked time exceeds estimate         |
| `get_task_report_job_status`    | Poll status of an async report job                  |
| `get_task_report_job_result`    | Retrieve result of a completed async report job     |

**Async Report Pattern**: Large reports run as background jobs. Use `get_task_report_job_status`
to poll (smart 3s interval in `openrouter_client.py`) and `get_task_report_job_result` once done.

</details>

### Other Key Features

- **Smart Async Polling** - Background report jobs polled every 3s; results appear within 3s of completion.
- **Zero Data Truncation** - All API responses delivered in full, no content trimming.
- **Connection Pooling** - Shared `api_client.py` with persistent HTTP session for efficient ClickUp API calls.
- **Bottom-Up Time Rollup** - Accurate time calculation from subtasks to parent tasks across all reports.
- **Auto-Watchdog** - `npm start` launches `watch-restart.py`, auto-restarting the MCP server on code changes.
- **Mapping Maintenance Scheduler** - APScheduler background job periodically syncs project mappings with ClickUp.
- **IST Timezone Support** - All date calculations default to Asia/Kolkata (+05:30).
- **Report Saving** - Reports saved as Markdown files to a configurable `REPORTS_DIR`.

---

## Quick Start Guide

### Setup in 5 Minutes

```bash
# 1. Clone repository
git clone https://github.com/Arya004pro/clickup-python-polling.git
cd clickup-python-polling

# 2. Create virtual environment
python -m venv myenv
myenv\Scripts\activate        # Windows
# source myenv/bin/activate   # Linux/Mac

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create .env (see Configuration Reference)

# 5. Start MCP server + watchdog
npm start
```

### Minimum `.env`

```bash
CLICKUP_API_TOKEN=pk_YOUR_TOKEN_HERE
DATABASE_URL=postgresql://user:pass@host:6543/postgres
MCP_SERVER_URL=http://127.0.0.1:8001/sse

# OpenRouter
OPENROUTER_API_KEY=sk-or-v1_your_key_here
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=qwen/qwen-2.5-7b-instruct
# OPENROUTER_MODEL_CHAIN=qwen/qwen-2.5-7b-instruct,meta-llama/llama-3.1-8b-instruct
```

---

## Detailed Setup Instructions

### Step 1: Install Python 3.11.9

<details>
<summary><b>Windows</b></summary>

1. Download Python 3.11.9 from [python.org](https://www.python.org/downloads/release/python-3119/)
2. Run installer: check **Add Python to PATH**, **Install pip**, **Install for all users**
3. Verify: `python --version` (should show Python 3.11.9)

</details>

<details>
<summary><b>Linux/Mac</b></summary>

```bash
curl https://pyenv.run | bash
export PATH="$HOME/.pyenv/bin:$PATH"
eval "$(pyenv init -)"
pyenv install 3.11.9 && pyenv global 3.11.9
```

</details>

### Step 2: Create and Activate Virtual Environment

```bash
cd clickup-python-polling
python -m venv myenv

# Windows PowerShell
myenv\Scripts\Activate.ps1

# Windows CMD
myenv\Scripts\activate.bat

# Linux/Mac
source myenv/bin/activate
```

### Step 3: Install Dependencies

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt

# Verify
python -c "import fastmcp; print('FastMCP:', fastmcp.__version__)"
python -c "from mcp import ClientSession; print('MCP client: OK')"
```

### Step 4: Get API Keys

#### ClickUp API Token

1. Go to [ClickUp Settings -> Apps](https://app.clickup.com/settings/apps)
2. Generate or copy your token (`pk_123456_XXXX...`)

#### ClickUp Team ID

Copy from your ClickUp URL: `https://app.clickup.com/{TEAM_ID}/v/...`

#### OpenRouter API Key

1. Sign up at [OpenRouter](https://openrouter.ai/)
2. Generate an API key from the keys page
3. Set `OPENROUTER_API_KEY` in `.env`
4. Set `OPENROUTER_MODEL` or `OPENROUTER_MODEL_CHAIN` in `.env`

#### Supabase / PostgreSQL

1. Create a project at [Supabase](https://supabase.com)
2. Copy the Transaction Pooler connection string from Settings -> Database

### Step 5: Configure Environment File

Create `.env` in the project root:

```env
# Required
CLICKUP_API_TOKEN=pk_123456_XXXXXXXXXXXXXXXXXXXXXXXXXXXX
DATABASE_URL=postgresql://postgres.xxxxx:password@aws-0-region.pooler.supabase.com:6543/postgres
MCP_SERVER_URL=http://127.0.0.1:8001/sse

# OpenRouter - LLM Provider
OPENROUTER_API_KEY=sk-or-v1_your_openrouter_key_here
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=qwen/qwen-2.5-7b-instruct
# Optional: comma-separated fallback chain
# OPENROUTER_MODEL_CHAIN=qwen/qwen-2.5-7b-instruct,meta-llama/llama-3.1-8b-instruct
# Optional headers
# OPENROUTER_HTTP_REFERER=https://your-app-url.example
# OPENROUTER_APP_TITLE=ClickUp MCP

# Optional
CLICKUP_TEAM_ID=12345678
CLICKUP_SPACE_ID=
REPORTS_DIR=D:\reports
```

### Step 6: Start the MCP Server

**Recommended (with file-watcher/watchdog):**

```bash
npm start
```

**Manual start:**

```bash
python clickup_mcp/mcp_server.py
```

Expected output:

```
Starting ClickUp MCP Server in 2s to allow initialization...
INFO:     Uvicorn running on http://0.0.0.0:8001
INFO:     Application startup complete.
```

Keep this terminal open -- the server must stay running for the AI client to function.

### Step 7: Start the AI Client

```bash
# Terminal 2
python openrouter_client.py
```

Expected output:

```
========================================================================
  ClickUp MCP Server  --  OpenRouter Client
========================================================================
  OK System prompt     : openrouter_system_prompt.md  (X chars)
  OK MCP endpoint      : http://127.0.0.1:8001/sse
  OK OpenRouter URL    : https://openrouter.ai/api/v1
  OK Active model      : qwen/qwen-2.5-7b-instruct

========================================================================
  You:
```

---

## AI Client Usage

### Commands

| Command | Description               |
| ------- | ------------------------- |
| `help`  | Show available commands   |
| `tools` | List all 81 MCP tools     |
| `quit`  | Exit with session summary |

### Example Queries

```
You: Show me all workspaces
```

Response: 1 workspace found with members, plan, and ID details.

```
You: What's the project health for Marketing?
```

Response: Health Score 78/100 (Grade B) with breakdown by overdue tasks, freshness, progress, assignment coverage, and time estimates + recommendations.

```
You: Generate daily standup for Development
```

Response: Auto-generated standup with completed tasks (yesterday), in-progress items, blockers, and tasks due today.

```
You: Show time tracking report for last week grouped by assignee
```

```
You: Find all tasks that are overdue or due within 3 days and assign risk levels
```

```
You: Show me the employee daily time report for this month
```

```
You: Which team members tracked less than 8 hours on any day this week?
```

### Session Summary

When you type `quit`, the client prints a full session summary:

```
====================================================================
  Session Summary
--------------------------------------------------------------------
  API calls        : 12
  MCP tool calls   : 18
  Input tokens     : 24,500
  Output tokens    : 8,200
  Total tokens     : 32,700
  Reports saved    : 2
  Duration         : 4m 32s
  Models used      :  qwen/qwen-2.5-7b-instruct x12
====================================================================
```

---

## Configuration Reference

### Environment Variables

| Variable                  | Required | Default                        | Description                            |
| ------------------------- | -------- | ------------------------------ | -------------------------------------- |
| `CLICKUP_API_TOKEN`       | Yes      | -                              | ClickUp personal API token             |
| `DATABASE_URL`            | Yes      | -                              | PostgreSQL/Supabase connection string  |
| `MCP_SERVER_URL`          | Yes      | `http://127.0.0.1:8001/sse`    | MCP server SSE endpoint                |
| `OPENROUTER_API_KEY`      | Yes      | -                              | OpenRouter API key                     |
| `OPENROUTER_BASE_URL`     | No       | `https://openrouter.ai/api/v1` | OpenRouter base URL                    |
| `OPENROUTER_MODEL`        | No       | `qwen/qwen-2.5-7b-instruct`    | Primary model                          |
| `OPENROUTER_MODEL_CHAIN`  | No       | -                              | Comma-separated model fallback chain   |
| `OPENROUTER_HTTP_REFERER` | No       | -                              | Optional OpenRouter attribution header |
| `OPENROUTER_APP_TITLE`    | No       | `ClickUp MCP`                  | Optional OpenRouter app title header   |
| `CLICKUP_TEAM_ID`         | No       | Auto-detected                  | Default workspace/team ID              |
| `CLICKUP_SPACE_ID`        | No       | -                              | Default space ID for filtering         |
| `REPORTS_DIR`             | No       | `D:\reports`                   | Directory for saved Markdown reports   |

### Supported Period Types (for report tools)

| Period Type    | Description                                                      |
| -------------- | ---------------------------------------------------------------- |
| `today`        | Current calendar day                                             |
| `yesterday`    | Previous calendar day                                            |
| `this_week`    | Monday to today (current week)                                   |
| `last_week`    | Full previous Monday-Sunday week                                 |
| `this_month`   | First of month to today                                          |
| `last_month`   | Full previous month                                              |
| `this_year`    | Jan 1 to today                                                   |
| `last_30_days` | Rolling 30-day window                                            |
| `rolling`      | Custom rolling window (use with `rolling_days`)                  |
| `custom`       | Specific date range (use `custom_start`/`custom_end` YYYY-MM-DD) |

---

## Troubleshooting

<details>
<summary><b>MCP Server won't start - Address already in use</b></summary>

```bash
# Find and kill process on port 8001
netstat -ano | findstr :8001
taskkill /PID <PID> /F
```

</details>

<details>
<summary><b>MCP Server won't start - ModuleNotFoundError</b></summary>

```bash
# Ensure virtual environment is activated
myenv\Scripts\activate
pip install -r requirements.txt
```

</details>

<details>
<summary><b>AI Client can't connect to MCP server</b></summary>

1. Ensure MCP server is running in another terminal (`npm start` or `python clickup_mcp/mcp_server.py`)
2. Verify `MCP_SERVER_URL=http://127.0.0.1:8001/sse` in `.env`
3. Try `http://localhost:8001/sse` as an alternative

</details>

<details>
<summary><b>OpenRouter API errors - Rate limit / quota</b></summary>

1. Check `OPENROUTER_API_KEY` is valid and has available credits/quota.
2. If using `OPENROUTER_MODEL_CHAIN`, ensure each model slug is valid.
3. If the primary model is rate-limited, the client rotates to the next model in chain.
4. If all configured models fail, wait briefly or add an additional fallback model.

</details>

<details>
<summary><b>Reports are not being saved</b></summary>

1. Check `REPORTS_DIR` in `.env` points to a writable directory
2. Create the directory manually if it does not exist: `mkdir D:\reports`
3. The client saves reports when it detects a `formatted_output` key in the tool response

</details>

<details>
<summary><b>Async reports time out or never complete</b></summary>

Large space/folder reports run as async background jobs on the MCP server. The client polls every 3s for up to 5 minutes (`STATUS_CHECK_TIMEOUT_S = 300`). If a job is stuck, use `get_environment_context` to check server state or restart the server with `npm start`.

</details>
