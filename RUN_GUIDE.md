# ClickUp MCP Docker Run Guide

This guide covers running the Docker stack, using the web dashboard/API, and locating generated report files.

## Prerequisites

- Docker Desktop installed and running
- `.env` file present in project root
- `monitoring_config.json` present in project root if you use monitored-scope queries
- `project_map.json` present in project root for mapped-project resolution/sync
- Required values in `.env`:
  - `CLICKUP_API_TOKEN`
  - `OPENROUTER_API_KEY` (for OpenRouter testing)

Example `.env` values (placeholders only):

```bash
CLICKUP_API_TOKEN=pk_your_clickup_token
AI_CLIENT_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-your-openrouter-key
OPENROUTER_MODEL=qwen/qwen-2.5-7b-instruct
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
MCP_SERVER_URL=http://mcp-server:8001/sse
REPORTS_DIR=/app/reports
```

Optional OpenRouter headers:

```bash
OPENROUTER_HTTP_REFERER=https://your-app-url.example
OPENROUTER_APP_TITLE=ClickUp MCP
OPENROUTER_ENABLE_ZAI_FALLBACK=true
OPENROUTER_ZAI_FALLBACK_MODELS=glm-4.7-flash,glm-4.5-flash
```

## Start the stack

```bash
docker compose up --build --watch
```

When services are healthy:

- MCP server: `http://localhost:8001/sse`
- API dashboard: `http://localhost:8003`

Notes:

- `docker-compose.yml` bind-mounts `./monitoring_config.json` and
  `./project_map.json` into MCP server runtime.
- If either file is missing, monitored reports can become broad or fail scope checks.
- With `--watch`, code/config changes trigger container rebuild/restart automatically.
- The dashboard auto-refreshes when API comes back after restart.

## Dashboard behavior

Open `http://localhost:8003` and submit a query.

- Responses are rendered as Markdown in the browser.
- Markdown tables are shown as HTML tables with readable styling.
- Press `Enter` to submit quickly (`Shift+Enter` adds a new line).
- If a report is generated and saved, the UI shows:
  - saved file name
  - direct link to open/download the saved file

## API endpoints

### Query

`POST /query`

```bash
curl -X POST http://localhost:8003/query \
  -H "Content-Type: application/json" \
  -d '{"question":"Generate last month space task report for BlogManager"}'
```

Example response fields:

- `status`
- `response`
- `tokens_used`
- `report_saved`
- `report_file`
- `report_download_url`

### Health and stats

- `GET /status`
- `GET /stats`

### Reports

- `GET /reports` (list saved reports)
- `GET /reports/latest` (download latest markdown report)
- `GET /reports/{report_name}` (download a specific report)

## Where reports are stored

Reports are written by `zai_client.py` to `REPORTS_DIR` (default: `/app/reports` in Docker).
Filenames now include report context:

`report_<type>_<entity>_<period>_<timestamp>.md`

In this compose setup, `/app/reports` is backed by a Docker named volume:

```yaml
volumes:
  reports:
```

Useful commands:

```bash
# list reports inside container
docker compose exec api-server ls -lah /app/reports

# list reports via API
curl http://localhost:8003/reports

# copy reports from container to host folder
docker compose cp api-server:/app/reports ./reports-export
```

## Optional: store reports directly on host filesystem

If you want testers to see report files directly on disk, replace `reports:/app/reports` with `./reports:/app/reports` in `docker-compose.yml` for services that use reports.

Then create host folder once:

```bash
mkdir reports
```

## Logs and operations

```bash
docker compose ps
docker compose logs -f
docker compose logs -f api-server
docker compose logs -f mcp-server
docker compose down
docker compose restart
```

## Troubleshooting

### Dashboard not loading

- Verify API container is up: `docker compose ps`
- Check API logs: `docker compose logs -f api-server`
- Confirm port `8003` is free

### Reports not appearing

- Check API list endpoint: `GET /reports`
- Check report directory in container: `docker compose exec api-server ls -lah /app/reports`
- Verify `REPORTS_DIR` is set consistently in compose env

### Monitored scope returns extra projects

- Confirm monitored config exists on host: `ls monitoring_config.json`
- Check MCP startup logs for missing config warning
- Verify monitored entries use correct `space` and `list_ids`
- Retry with explicit scope phrase: `Monitored AIX`

### MCP client not ready

- MCP may still be initializing; retry in a few seconds
- Check logs: `docker compose logs -f mcp-server`
