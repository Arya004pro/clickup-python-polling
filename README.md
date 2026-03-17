# ClickUp MCP + Automated Reports (Brevo Workflow)

This repository runs a Docker-first ClickUp reporting system with:
- an MCP server exposing ClickUp tools
- an API server for dashboard + report/query endpoints
- a Motia runtime for scheduled/manual report automation and email delivery

This README reflects the current `brevo-railway-setup` branch workflow and excludes Supabase-specific setup.

## 1. Current Architecture

Services in `docker-compose.yml`:
- `mcp-server` -> FastMCP service on `http://localhost:8001`
- `api-server` -> REST + dashboard on `http://localhost:8003`
- `motia-reports` -> automation runtime on `http://localhost:3111`
- `ai-client` -> optional interactive CLI profile (`--profile interactive`)

Data/config files mounted into containers:
- `project_map.json`
- `monitoring_config.json`
- `report_spaces_config.json`
- reports persisted via Docker volume: `reports`

## 2. What the System Does

### A) Interactive querying
Use the dashboard or `POST /query` to ask natural-language questions. The API server uses OpenRouter model(s), calls MCP tools, and returns report/content output.

### B) Direct report generation
Automation primarily uses direct report endpoint mode (`/report/space`) for deterministic, non-chat report generation.

### C) Automated email pipeline
Motia flow:
1. trigger step receives schedule/manual trigger
2. `GenerateReports` calls API server to produce markdown reports for configured spaces
3. `SendReportEmail` builds summary + attachments and sends mail (Brevo API transport supported)

## 3. MCP Tooling (By Module)

The MCP server provides a large ClickUp toolset grouped in modules under `clickup_mcp/`.

### Workspace and structure
- Workspaces/teams/spaces/folders/lists discovery
- Member resolution
- Cache invalidation helpers

Typical tools:
- `get_workspaces`
- `get_spaces`
- `get_folders`
- `get_lists`
- `get_team_members`

### Task management
- task listing/searching
- create/update operations
- overdue/workload/progress helpers

Typical tools:
- `get_tasks`
- `get_task`
- `create_task`
- `update_task`
- `search_tasks`

### PM analytics and time reports
- period-based time reports
- async report jobs + polling
- status/effort/at-risk/stale analysis

Typical tools:
- `get_time_tracking_report`
- `get_space_time_report_by_period`
- `get_async_report_status`
- `get_async_report_result`

### Project configuration and intelligence
- project discovery/mapping
- health scores, blockers, standups, digests

Typical tools:
- `discover_projects`
- `add_project`
- `get_project_health_score`
- `get_project_weekly_digest`

### Sync and mapping
- map/unmap aliases
- maintenance refresh
- stale mapping pruning
- monitoring/report-space alignment

Typical tools:
- `map_project`
- `list_mapped_projects`
- `trigger_mapping_maintenance`
- `find_project_anywhere`

### Task report tools
- space/project/member report generation
- missing estimation / low-hours / overtracked views
- async job status/result

Typical tools:
- `get_space_task_report`
- `get_project_task_report`
- `get_member_task_report`
- `get_task_report_job_status`
- `get_task_report_job_result`

## 4. API Endpoints

Base URL: `http://localhost:8003`

Core endpoints:
- `GET /` -> web dashboard
- `GET /status` -> runtime status
- `GET /stats` -> basic stats
- `POST /query` -> natural-language query
- `POST /report/space` -> direct space report generation
- `GET /reports` -> list saved reports
- `GET /reports/latest` -> latest report metadata
- `GET /reports/{name}` -> fetch report content
- `POST /reports/send` -> send one saved report via email
- `POST /render/pdf` -> render markdown report to PDF

## 5. Automation Workflow

### Space selection
`report_spaces_config.json` controls which spaces run during scheduled/manual triggers.

### Monitoring scope
`monitoring_config.json` controls monitored project/list filtering (used by monitored-space report mode).

### Maintenance behavior
`sync_mapping.py` performs mapping maintenance, refresh, and stale cleanup logic to keep mappings/config aligned with live ClickUp data.

### Triggering reports manually
Use:

```bash
python trigger_reports.py --period yesterday --label "Manual yesterday run"
```

Other period values:
- `today`
- `yesterday`
- `this_week`
- `last_week`
- `this_month`
- `last_month`

## 6. Environment Variables

Create `.env` in repository root.

Required baseline:

```env
CLICKUP_API_TOKEN=
OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=qwen/qwen-2.5-7b-instruct
```

Required for current email flow (Brevo transport):

```env
EMAIL_TRANSPORT=brevo_api
BREVO_API_KEY=
EMAIL_FROM=
EMAIL_FROM_NAME=Arya
SMTP_TO=
```

Notes:
- `SMTP_TO` is currently used as recipient target variable in the report email pipeline.
- SMTP fallback variables are optional if you intentionally run Brevo-only mode.

## 7. Local Setup and Run

### Prerequisites
- Docker Desktop running
- Python 3.11+
- valid `.env`

### Start all services

```bash
docker compose up --build -d
```

### Check status

```bash
docker compose ps
docker compose logs -f mcp-server api-server motia-reports
```

### Dashboard
Open:
- `http://localhost:8003`

## 8. Typical Operations

### Trigger full report pipeline manually

```bash
python trigger_reports.py --period yesterday --label "Manual run"
```

### Send a specific saved report

```bash
curl -X POST http://localhost:8003/reports/send \
  -H "Content-Type: application/json" \
  -d '{"report_name":"report_space_blogmanager_YYYY-MM-DD.md"}'
```

### View generated reports in container

```bash
docker compose exec api-server ls -lah /app/reports
```

## 9. Troubleshooting

### Report generated but email not received
1. verify trigger completed in logs:
   - `docker compose logs --since 30m motia-reports`
2. verify API accepted send request:
   - look for send step `[OK]` logs
3. verify provider status in Brevo dashboard:
   - check delivered/deferred/bounced/suppressed events
4. check spam/promotions inbox

### `/query` works but report automation fails
- confirm `motia-reports` can reach `api-server` (`API_SERVER_URL=http://api-server:8003`)
- verify `report_spaces_config.json` entries are valid
- verify ClickUp token and team access

### Missing/incorrect space data
- run mapping maintenance
- inspect `project_map.json` and `monitoring_config.json`

## 10. Security Notes

- Keep `.env` out of git (already ignored).
- Rotate keys immediately if they are ever printed in logs/terminal output.
- Prefer diagnostics that show `SET/EMPTY` rather than secret values.

---

If you add new tools/modules/endpoints, update this README in the same PR so runtime docs stay accurate.
