# ClickUp MCP Docker Run Guide

This guide reflects the current working runtime (March 2026).

## What runs in Docker
- `mcp-server` on `:8001` (FastMCP ClickUp tools)
- `api-server` on `:8003` (dashboard + REST API)
- `motia-reports` on `:3111` (scheduled/manual report flow + email)

## Prerequisites
- Docker Desktop running
- `.env` in project root with at least:
  - `CLICKUP_API_TOKEN`
  - `OPENROUTER_API_KEY`
  - SMTP variables if email sending is required (`SMTP_EMAIL`, `SMTP_PASSWORD`, `SMTP_TO`)
- `monitoring_config.json` and `project_map.json` present in project root

## Start the stack
```bash
docker compose up --build --watch
```

Healthy URLs:
- MCP SSE: `http://localhost:8001/sse`
- API dashboard: `http://localhost:8003`

Dashboard highlights:
- Query tab for natural-language requests
- Reports tab with paginated saved reports
- One-click send of any saved report to email (`SMTP_TO` by default, optional recipient override)

## Current report flow
- Motia generate step calls API direct endpoint `POST /report/space`
- API server calls MCP report tool and returns markdown
- Email step sends summary table + full report markdown
- No separate manager-summary endpoint is active in the current system

## API endpoints you should use

### 1) Natural-language query
`POST /query`

```bash
curl -X POST http://localhost:8003/query \
  -H "Content-Type: application/json" \
  -d '{"question":"Generate yesterday space task report for BlogManager"}'
```

### 2) Direct report generation (recommended for automation)
`POST /report/space`

```bash
curl -X POST http://localhost:8003/report/space \
  -H "Content-Type: application/json" \
  -d '{"space_name":"Monitored AIX","period_type":"yesterday","include_archived":true}'
```

### 3) Health/stats/reports
- `GET /status`
- `GET /stats`
- `GET /reports`
- `GET /reports/latest`
- `GET /reports/{name}`
- `POST /reports/send`

Send a saved report by email:
```bash
curl -X POST http://localhost:8003/reports/send \
  -H "Content-Type: application/json" \
  -d '{"report_name":"report_space_blogmanager_2026-03-10-2026-03-10_2026-03-10_16-35-48.md","to_email":"manager@example.com"}'
```
Note: this endpoint sends the report as a `.pdf` attachment (falls back to `.md` only if PDF render fails).

## Manual trigger for Motia flow
After stack is up:

```bash
python trigger_reports.py --period today
```

Useful variants:
```bash
python trigger_reports.py --period yesterday
python trigger_reports.py --period today --no-aix
python trigger_reports.py --period this_week --label "Manual weekly check"
```

Notes:
- Default includes all monitored spaces (including AIX as monitored scope)
- Use `--no-aix` to exclude AIX

## Scheduled triggers (current)
- `9AM` -> yesterday report
- `2PM` -> today report
- `6PM` -> today report

## Reports storage
Reports are saved under `/app/reports` inside containers and backed by Docker volume `reports`.

Useful commands:
```bash
docker compose exec api-server ls -lah /app/reports
curl http://localhost:8003/reports
docker compose cp api-server:/app/reports ./reports-export
```

## Common operations
```bash
docker compose ps
docker compose logs -f
docker compose logs -f api-server
docker compose logs -f mcp-server
docker compose logs -f motia-reports
docker compose restart api-server
docker compose down
```

## Troubleshooting

### 1) Dashboard not loading
- Check: `docker compose ps`
- Check logs: `docker compose logs -f api-server`
- Ensure port `8003` is free

### 2) Report includes wrong scope
- For AIX monitored mode use `space_name: "Monitored AIX"`
- Verify `monitoring_config.json` has correct `list_ids`

### 3) No reports appearing
- Call `GET /reports`
- Check container path: `docker compose exec api-server ls -lah /app/reports`
- Confirm `REPORTS_DIR=/app/reports` in compose env

### 4) Motia trigger fails
- Verify `motia-reports` container is up
- Check endpoint: `http://localhost:3111/trigger-report`
- Re-run with retries from `trigger_reports.py`
