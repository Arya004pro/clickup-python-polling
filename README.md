# ClickUp MCP Reporting Platform

<div align="center">

![Python](https://img.shields.io/badge/Python-3.11-blue)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED)
![FastMCP](https://img.shields.io/badge/MCP-FastMCP-green)
![Automation](https://img.shields.io/badge/Reports-Automated-orange)
![Branch](https://img.shields.io/badge/branch-brevo--railway--setup-purple)

</div>

Production-oriented ClickUp reporting platform with:

- MCP tool server for ClickUp operations and analytics
- API server for dashboard, query, and report endpoints
- Motia runtime for scheduled/manual report automation and email delivery
- Brevo-powered email transport for deployment-friendly delivery

This README documents the current branch behavior and runtime workflow.

## Table of Contents

- Architecture
- End-to-End Workflows
- Local Setup (Exact)
- Railway 24/7 Setup (Exact)
- Mapping Files Guide
- API Endpoints
- Operations Playbook
- Complete MCP Tool Inventory
- Troubleshooting
- Security

## Architecture

Services in `docker-compose.yml`:

- `mcp-server` on `:8001` (FastMCP tools)
- `api-server` on `:8003` (dashboard + REST)
- `motia-reports` on `:3111` (automation + cron/manual trigger)
- `ai-client` optional profile (`--profile interactive`)

Core mounted/config files:

- `project_map.json`
- `monitoring_config.json`
- `report_spaces_config.json`
- reports volume: `reports`

Runtime intent:

- interactive query path -> `/query`
- deterministic automation path -> `/report/space`
- email dispatch path -> Motia `SendReportEmail` step

## End-to-End Workflows

### 1) Interactive Query Workflow

1. User asks question in dashboard or `POST /query`
2. API server calls OpenRouter model(s)
3. MCP tools are selected/invoked
4. response is returned and optionally saved as report markdown

### 2) Automated Report Workflow

1. Trigger source (cron/manual) sends event to Motia flow
2. `GenerateReports` loops configured spaces and calls API server
3. API server generates per-space markdown report
4. `SendReportEmail` builds summary + attachment (PDF preferred)
5. Email is sent via configured transport (`EMAIL_TRANSPORT`)

### 3) Manual Trigger Workflow

Run:

```bash
python trigger_reports.py --period yesterday --label "Manual run"
```

This calls Motia endpoint `/trigger-report`, executes generate + email steps, and logs timing details.

## Local Setup (Exact)

### Prerequisites

- Docker Desktop
- Python 3.11+
- `.env` file present

### 1) Create `.env`

Minimum baseline:

```env
CLICKUP_API_TOKEN=
CLICKUP_TEAM_ID=
OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=qwen/qwen-2.5-7b-instruct

EMAIL_TRANSPORT=brevo_api
BREVO_API_KEY=
EMAIL_FROM=
EMAIL_FROM_NAME=Arya
SMTP_TO=

REPORT_API_MODE=direct
REPORT_CONCURRENCY=2
SUPPORT_CRONS_ENABLED=false
RUST_LOG=warn
```

### 2) Start stack

```bash
docker compose up --build -d
```

Or use npm shortcuts (recommended):

```bash
npm run dev:detach
```

Useful npm commands:

```bash
npm run dev            # up --build (attached)
npm run dev:detach     # up --build -d
npm run dev:watch      # up --build --watch
npm run dev:logs       # tail core service logs
npm run dev:status     # docker compose ps
npm run dev:down       # stop stack
npm run dev:restart    # restart services
```

### 3) Verify health

```bash
docker compose ps
docker compose logs -f mcp-server api-server motia-reports
```

### 4) Open dashboard

- `http://localhost:8003`

## Railway 24/7 Setup (Exact)

Deploy 3 services from the same repo:

- `mcp-server`
- `api-server`
- `motia-reports`

Service runtime:

- `mcp-server`: Dockerfile `Dockerfile`, start `python -m clickup_mcp.server`
- `api-server`: Dockerfile `Dockerfile`, start `python api_server.py`
- `motia-reports`: Dockerfile `Dockerfile.motia`, default image command

Service networking variables:

- in `api-server`: `MCP_SERVER_URL=http://<mcp-private-domain>/sse`
- in `motia-reports`: `API_SERVER_URL=http://<api-private-domain>`

For true 24/7 behavior:

- keep all 3 services always deployed
- enable always-on restart behavior in Railway
- add persistent volumes:
  - `api-server` -> `/app/reports`
  - `motia-reports` -> `/app/data`

Current automatic schedule (IST):

- 9:00 AM -> yesterday report
- 2:00 PM -> today midday report
- 6:00 PM -> today EOD report

For full Railway redeploy checklist, see `RAILWAY_REDEPLOY_GUIDE.md`.

## Mapping Files Guide

The system depends on three JSON files in project root.

### 1) `project_map.json`

Purpose:

- alias-to-ClickUp mapping and discovered hierarchy cache

How to initialize:

- if missing, create from `project_map.example.json`
- system can auto-populate via mapping discovery/maintenance tools

### 2) `monitoring_config.json`

Purpose:

- monitored projects/lists used by monitored-scope reports

How to initialize:

- copy from `monitoring_config.example.json`
- add monitored projects using MCP sync tools

### 3) `report_spaces_config.json`

Purpose:

- controls which spaces are included in scheduled/manual report runs

How to initialize:

- copy from `report_spaces_config.example.json`
- manage entries using MCP sync tools

### Recommended generation/bootstrap flow

1. Start stack
2. Discover workspace hierarchy
3. Map top-level spaces
4. Configure report spaces
5. Configure monitored projects
6. Trigger maintenance refresh

Practical commands:

```bash
# Windows PowerShell bootstrap from examples
Copy-Item project_map.example.json project_map.json -Force
Copy-Item monitoring_config.example.json monitoring_config.json -Force
Copy-Item report_spaces_config.example.json report_spaces_config.json -Force
```

After bootstrap, use tools from dashboard query tab (tool-call style prompts) to keep files in sync:

- `discover_hierarchy`
- `map_project`
- `list_report_spaces` / `add_report_space` / `remove_report_space`
- `list_monitored_projects` / `add_monitored_project` / `remove_monitored_project`
- `trigger_mapping_maintenance`

### Mapping hygiene rules

- keep only valid ClickUp spaces/projects currently accessible by token
- rerun maintenance after workspace structure changes
- avoid manual edits while automation is running
- commit config JSON updates together with workflow/code changes

## API Endpoints

Base URL: `http://localhost:8003`

- `GET /` dashboard
- `GET /status`
- `GET /stats`
- `POST /query`
- `POST /report/space`
- `GET /reports`
- `GET /reports/latest`
- `GET /reports/{name}`
- `POST /reports/send`
- `POST /render/pdf`

Motia trigger endpoint:

- `POST http://localhost:3111/trigger-report`

## Operations Playbook

### Start

```bash
docker compose up --build -d
```

### Tail logs

```bash
docker compose logs -f api-server
docker compose logs -f mcp-server
docker compose logs -f motia-reports
```

### Trigger report run

```bash
python trigger_reports.py --period yesterday --label "Manual yesterday run"
```

or:

```bash
npm run dev:trigger:yesterday
npm run dev:trigger:today
```

### Send saved report

```bash
curl -X POST http://localhost:8003/reports/send \
  -H "Content-Type: application/json" \
  -d '{"report_name":"report_space_blogmanager_YYYY-MM-DD.md"}'
```

## Complete MCP Tool Inventory

This table is generated from current code decorators in `clickup_mcp/*.py`.

| Module                | Tool                                   | Description                                                                                          |
| --------------------- | -------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| PM Analytics          | `get_async_report_result`              | Return result for finished async report job. Safe to call anytime.                                   |
| PM Analytics          | `get_async_report_status`              | Check status for an async report job. HARD LIMIT: 5 polls per job.                                   |
| PM Analytics          | `get_at_risk_tasks`                    | Find tasks overdue or due soon.                                                                      |
| PM Analytics          | `get_employee_daily_time_report`       | Employee daily time report - replicates ClickUp's Timesheet and Time Reporting.                      |
| PM Analytics          | `get_estimation_accuracy`              | Analyze estimation vs actuals using robust metrics.                                                  |
| PM Analytics          | `get_folder_time_report_comprehensive` | Time tracking report for a FOLDER with period filters.                                               |
| PM Analytics          | `get_inactive_assignees`               | Identify inactive team members.                                                                      |
| PM Analytics          | `get_person_tasks_with_time`           | Get tasks a person worked on with timestamps (wrapper for detailed report).                          |
| PM Analytics          | `get_progress_since`                   | Get tasks completed or changed since date.                                                           |
| PM Analytics          | `get_project_report_universal`         | Universal project report generator. Works with ANY project (space, folder, or list).                 |
| PM Analytics          | `get_space_folder_team_report`         | Hierarchical time report: Space > Folder > Team Member.                                              |
| PM Analytics          | `get_space_project_time_report`        | Generate a time report for a SPACE grouped by PROJECT (folder/list).                                 |
| PM Analytics          | `get_space_time_report`                | Time tracking report for entire SPACE (all folders and lists).                                       |
| PM Analytics          | `get_space_time_report_by_period`      | Space time report with async support. Works for any period (day, week, month, etc).                  |
| PM Analytics          | `get_space_time_report_comprehensive`  | Time tracking report for a SPACE with period filters.                                                |
| PM Analytics          | `get_stale_tasks`                      | Find tasks with no updates.                                                                          |
| PM Analytics          | `get_status_summary`                   | Summary of task statuses.                                                                            |
| PM Analytics          | `get_task_status_distribution`         | Get actual task status distribution.                                                                 |
| PM Analytics          | `get_task_time_breakdown`              | Detailed breakdown of a task tree.                                                                   |
| PM Analytics          | `get_time_report_by_period`            | Time report based on time entry intervals. Supports any date range (day, week, month, year, custom). |
| PM Analytics          | `get_time_report_detailed`             | No short docstring in code.                                                                          |
| PM Analytics          | `get_time_tracking_report`             | CRITICAL: This report includes ALL tasks from ALL statuses by default.                               |
| PM Analytics          | `get_untracked_tasks`                  | Find tasks with zero logged time.                                                                    |
| Project Configuration | `add_project`                          | Add unique project.                                                                                  |
| Project Configuration | `discover_projects`                    | Scan workspace to find potential projects.                                                           |
| Project Configuration | `get_all_projects_status`              | Get summary status for all tracked projects.                                                         |
| Project Configuration | `get_project_status`                   | Get high-level status metrics for a project.                                                         |
| Project Configuration | `list_projects`                        | No short docstring in code.                                                                          |
| Project Configuration | `refresh_projects`                     | Verifies all tracked projects still exist in ClickUp.                                                |
| Project Configuration | `remove_project`                       | No short docstring in code.                                                                          |
| Project Intelligence  | `get_list_defined_statuses`            | Fetches the Effective Statuses for a list.                                                           |
| Project Intelligence  | `get_project_at_risk`                  | No short docstring in code.                                                                          |
| Project Intelligence  | `get_project_blockers`                 | No short docstring in code.                                                                          |
| Project Intelligence  | `get_project_daily_standup`            | No short docstring in code.                                                                          |
| Project Intelligence  | `get_project_health_score`             | No short docstring in code.                                                                          |
| Project Intelligence  | `get_project_statuses`                 | Gets defined statuses for a project (List, Folder, or Space).                                        |
| Project Intelligence  | `get_project_team_workload`            | No short docstring in code.                                                                          |
| Project Intelligence  | `get_project_time_tracking`            | No short docstring in code.                                                                          |
| Project Intelligence  | `get_project_weekly_digest`            | Weekly summary for stakeholders.                                                                     |
| Project Intelligence  | `get_workspace_folderless_lists`       | Scans the workspace to find all 'Folderless Lists'.                                                  |
| Sync & Mapping        | `add_monitored_project`                | Add/update one monitored project (folder/list) in monitoring_config.json.                            |
| Sync & Mapping        | `add_report_space`                     | Add/update one space in report_spaces_config.json.                                                   |
| Sync & Mapping        | `clear_sync`                           | Clear all project mappings and discovery cache. Destructive.                                         |
| Sync & Mapping        | `discover_hierarchy`                   | List all spaces, folders, and lists in a workspace for project mapping.                              |
| Sync & Mapping        | `find_project_anywhere`                | Universal project/entity finder. Search for any space, folder, or list by name.                      |
| Sync & Mapping        | `get_environment_context`              | Bootstrap tool: returns MCP environment state including                                              |
| Sync & Mapping        | `get_mapped_project`                   | Get detailed info about a specifically mapped project.                                               |
| Sync & Mapping        | `get_sync_status`                      | Get the overall health of the sync and cache.                                                        |
| Sync & Mapping        | `list_mapped_projects`                 | Show all currently mapped projects.                                                                  |
| Sync & Mapping        | `list_monitored_projects`              | List monitored projects configured in monitoring_config.json.                                        |
| Sync & Mapping        | `list_report_spaces`                   | List spaces selected for automated report generation.                                                |
| Sync & Mapping        | `list_spaces`                          | List all spaces and indicate which ones are already mapped.                                          |
| Sync & Mapping        | `map_project`                          | Map a ClickUp Space as a top-level 'Project'.                                                        |
| Sync & Mapping        | `prune_cache`                          | Remove expired cache entries.                                                                        |
| Sync & Mapping        | `refresh_project`                      | Force a refresh of a project's structure from ClickUp.                                               |
| Sync & Mapping        | `remove_monitored_project`             | Remove one monitored project from monitoring_config.json by alias or clickup_id.                     |
| Sync & Mapping        | `remove_report_space`                  | Remove one space from report_spaces_config.json by name.                                             |
| Sync & Mapping        | `trigger_mapping_maintenance`          | Manually trigger the mapping maintenance routine to update all mapped                                |
| Sync & Mapping        | `unmap_project`                        | Remove a project mapping.                                                                            |
| Task Management       | `create_task`                          | Create a new task.                                                                                   |
| Task Management       | `get_list_progress`                    | Get progress summary for a list (useful for sprints).                                                |
| Task Management       | `get_overdue_tasks`                    | Get all overdue tasks in a list.                                                                     |
| Task Management       | `get_project_tasks`                    | Get all tasks in a project (folder/space) with optional filters.                                     |
| Task Management       | `get_task`                             | Get detailed task information.                                                                       |
| Task Management       | `get_tasks`                            | List tasks in a list with optional filters.                                                          |
| Task Management       | `get_workload`                         | Get workload distribution per team member.                                                           |
| Task Management       | `search_tasks`                         | Search tasks within a folder or space.                                                               |
| Task Management       | `update_task`                          | Update an existing task.                                                                             |
| Task Reports          | `get_low_hours_report`                 | Low Hours Report - employees who tracked fewer than N hours on any working day.                      |
| Task Reports          | `get_member_task_report`               | Team Member-Wise Task Report.                                                                        |
| Task Reports          | `get_missing_estimation_report`        | Missing Time Estimation Report.                                                                      |
| Task Reports          | `get_overtracked_report`               | Overtracked Report                                                                                   |
| Task Reports          | `get_project_task_report`              | Project-Wise Task Report.                                                                            |
| Task Reports          | `get_space_task_report`                | Space-Wise Task Report.                                                                              |
| Task Reports          | `get_task_report_job_result`           | Retrieve result of a finished background task-report job.                                            |
| Task Reports          | `get_task_report_job_status`           | Check status of a background task-report job (max 5 polls).                                          |
| Workspace Structure   | `get_folder`                           | Get details of a specific ClickUp folder, including all lists inside it.                             |
| Workspace Structure   | `get_folderless_lists`                 | List all lists that are directly in the space (not inside any folder).                               |
| Workspace Structure   | `get_folders`                          | List all folders inside a specific ClickUp space.                                                    |
| Workspace Structure   | `get_list`                             | Get detailed information about a specific ClickUp list.                                              |
| Workspace Structure   | `get_lists`                            | List all lists inside a specific ClickUp folder.                                                     |
| Workspace Structure   | `get_space`                            | Get detailed information about a specific ClickUp space.                                             |
| Workspace Structure   | `get_spaces`                           | List all spaces inside a specific workspace (team).                                                  |
| Workspace Structure   | `get_team_members`                     | Get all members/assignees from a workspace.                                                          |
| Workspace Structure   | `get_workspaces`                       | List all accessible workspaces (teams) in ClickUp.                                                   |
| Workspace Structure   | `invalidate_cache`                     | Clear cached ClickUp data for fresh results.                                                         |
| Workspace Structure   | `resolve_assignees`                    | Resolve assignee names/usernames to their user IDs.                                                  |

## Troubleshooting

### Email says sent but not in inbox

1. check Motia send-step logs
2. check Brevo transactional events (delivered/deferred/bounced/suppressed)
3. verify sender identity and recipient status in Brevo
4. check spam/promotions tabs

### Mapping shows stale/removed spaces

1. run `trigger_mapping_maintenance`
2. verify token/team access for missing entities
3. re-check `monitoring_config.json` and `report_spaces_config.json`

### API works but automation fails

- verify `API_SERVER_URL` in motia service
- verify `MCP_SERVER_URL` in api service
- verify required env vars are set in runtime (not just local shell)

## Security

- never commit `.env`
- rotate keys if they appear in logs/history
- use diagnostics that print only `SET/EMPTY` for secret checks

---

If you add or rename tools/endpoints, regenerate this README tool table so docs stay accurate.
