# Railway Redeploy Guide (OpenRouter + Brevo)

This guide is for a fresh Railway project with 3 services:
- `mcp-server`
- `api-server`
- `motia-reports`

It assumes:
- OpenRouter is your only AI provider
- Brevo API is your email transport
- Runtime JSON files are provided via env vars (no local file mounts)

## 1) Branch Strategy

Recommended:
1. Keep `docker-motia` untouched.
2. Use branch `brevo-railway-setup` for Railway changes.
3. Merge to main branch only after Railway smoke tests pass.

If you still want to reset your old branch:
```powershell
git switch docker-motia
git reset --hard 3a15815
```

## 2) Brevo Setup

1. In Brevo: `Settings -> SMTP & API -> API keys`, create API key.
2. In Brevo: `Settings -> Senders, domains, IPs -> Senders`, verify sender email.
3. Keep:
- `BREVO_API_KEY`
- verified sender email for `EMAIL_FROM`

## 3) Create Railway Services

Create one Railway project, then add 3 services from the same repo:

### A) `mcp-server`
- Dockerfile path: `Dockerfile`
- Start command:
```bash
python -m clickup_mcp.server
```
- Expose/public domain: not required (private service is enough)

### B) `api-server`
- Dockerfile path: `Dockerfile`
- Start command:
```bash
python api_server.py
```
- Expose/public domain: required (dashboard + API)

### C) `motia-reports`
- Dockerfile path: `Dockerfile.motia`
- Start command: keep image default (`iii-cli start -c iii-config.yaml`)
- Expose/public domain: optional
  - required only if you trigger `POST /trigger-report` externally

## 4) Networking Between Services

Set service-to-service URLs using Railway private domains:
- In `api-server`: `MCP_SERVER_URL=http://<mcp-private-domain>/sse`
- In `motia-reports`: `API_SERVER_URL=http://<api-private-domain>`

Use Railway provided private-domain values from each service.

## 5) Variables (OpenRouter + Brevo Only)

Set these at project-level shared variables:

```env
CLICKUP_API_TOKEN=
CLICKUP_TEAM_ID=
OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=qwen/qwen-2.5-7b-instruct

EMAIL_TRANSPORT=auto
BREVO_API_KEY=
EMAIL_FROM=
EMAIL_FROM_NAME=Arya
SMTP_TO=

REPORT_API_MODE=direct
REPORT_CONCURRENCY=2
SUPPORT_CRONS_ENABLED=false
RUST_LOG=warn
```

Set service-specific variables:
- `api-server`:
  - `MCP_SERVER_URL=http://<mcp-private-domain>/sse`
  - `REPORTS_DIR=/app/reports`
- `motia-reports`:
  - `API_SERVER_URL=http://<api-private-domain>`
- `mcp-server`:
  - `REPORTS_DIR=/app/reports` (optional but safe)

## 6) Runtime Config Files (No Local Mounts)

Use env vars instead of local files:
- `MONITORING_CONFIG_JSON`
- `PROJECT_MAP_JSON`
- `REPORT_SPACES_JSON`

The app now hydrates these at runtime:
- `mcp-server` writes to:
  - `/app/monitoring_config.json`
  - `/app/project_map.json`
  - `/app/report_spaces_config.json`
- `motia-reports` reads:
  - `REPORT_SPACES_JSON` directly
  - `MONITORING_CONFIG_JSON` directly in monitored-scope report logic

How to set JSON safely:
1. Minify JSON into one line.
2. Paste as Railway variable value.
3. Do not wrap with extra quotes.

Example conversion:
```powershell
Get-Content .\report_spaces_config.json -Raw | ConvertFrom-Json | ConvertTo-Json -Compress
```

## 7) Remove Unused Variables

Unset these from Railway if not used:
- `CEREBRAS_API_KEY`
- `GROQ_API_KEY`
- `GEMINI_API_KEY`
- `OLLAMA_BASE_URL`
- `OLLAMA_MODEL`
- SMTP login vars (`SMTP_EMAIL`, `SMTP_PASSWORD`, `SMTP_HOST`, `SMTP_PORT`) unless you explicitly need SMTP fallback

## 8) 24/7 Reliability Notes

- Keep all 3 services deployed continuously.
- For strict always-on behavior, use Railway plan/restart policy that supports `Always`.
- Add persistent volumes:
  - `api-server`: `/app/reports`
  - `motia-reports`: `/app/data`

## 9) Smoke Test Checklist

1. `GET /status` on `api-server` is healthy.
2. Trigger report manually:
```bash
curl -X POST https://<motia-public-domain>/trigger-report \
  -H "Content-Type: application/json" \
  -d "{\"period\":\"today\",\"schedule_label\":\"railway-smoke\"}"
```
3. Check `motia-reports` logs:
  - report generation completes
  - `Email transport selected: brevo_api`
  - `[OK] Email sent ...`

