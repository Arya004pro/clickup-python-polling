"""
Configuration: spaces to monitor, email settings, MCP server URL.
"""

import json
import os
from pathlib import Path

# API Server (generates reports via OpenRouter + MCP, same as localhost page)
API_SERVER_URL = os.getenv("API_SERVER_URL", "http://api-server:8003")

# MCP Server (running in Docker)
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8001")

# ClickUp API
CLICKUP_API_TOKEN = os.getenv("CLICKUP_API_TOKEN", "")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID", "")

# Email Provider
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "smtp")

# Resend API
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM", "aryapatel.eng@gmail.com")

# Email (Gmail SMTP)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_EMAIL = os.getenv("SMTP_EMAIL", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_TO = os.getenv("SMTP_TO", "aryapatel.eng@gmail.com")

# Default fallback list. Preferred source is report_spaces_config.json.
_DEFAULT_MONITORED_SPACES = [
    {
        "name": "AIX",
        "display": "Monitored AIX",
        # Must stay "Monitored AIX" so monitored scope applies via monitoring_config.json.
        "query_label": "Monitored AIX",
        "scope": "monitored",
    },
    {"name": "VibeScorer", "display": "VibeScorer", "scope": "full"},
    {
        "name": "General Task Management",
        "display": "General Task Mgmt",
        "scope": "full",
    },
    {"name": "Avinashi Chat", "display": "Avinashi Chat", "scope": "full"},
    {"name": "Venture Studio", "display": "Venture Studio", "scope": "full"},
    {"name": "BlogManager", "display": "BlogManager", "scope": "full"},
    {"name": "Avinashi Leaders", "display": "Avinashi Leaders", "scope": "full"},
]


def _report_spaces_candidates() -> list[Path]:
    env_path = os.getenv("REPORT_SPACES_CONFIG_PATH", "").strip()
    if env_path:
        return [Path(env_path)]

    # Docker path first, then local repo root fallback.
    steps_dir = Path(__file__).resolve().parent
    return [
        Path("/app/report_spaces_config.json"),
        steps_dir.parent.parent / "report_spaces_config.json",
    ]


def _load_report_spaces() -> list[dict]:
    for candidate in _report_spaces_candidates():
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
            spaces = data.get("report_spaces", [])
            if isinstance(spaces, list) and spaces:
                return spaces
        except Exception:
            continue
    return _DEFAULT_MONITORED_SPACES


MONITORED_SPACES = _load_report_spaces()
