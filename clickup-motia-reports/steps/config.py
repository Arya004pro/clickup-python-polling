"""
Configuration — spaces to monitor, email settings, MCP server URL.
"""

import os

# ── API Server (generates reports via OpenRouter + MCP, same as localhost page) ──
API_SERVER_URL = os.getenv("API_SERVER_URL", "http://api-server:8003")

# ── MCP Server (running in Docker) ──────────────────────────────────────────
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8001")

# ── ClickUp API ─────────────────────────────────────────────────────────────
CLICKUP_API_TOKEN = os.getenv("CLICKUP_API_TOKEN", "")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID", "")

# ── Email (Gmail SMTP) ─────────────────────────────────────────────────────
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_EMAIL = os.getenv("SMTP_EMAIL", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_TO = os.getenv("SMTP_TO", "aryapatel.eng@gmail.com")

# ── Spaces to Monitor ──────────────────────────────────────────────────────
# From "Projects that need to be Monitored.txt"
MONITORED_SPACES = [
    {
        "name": "AIX",
        "display": "Monitored AIX",
        # query_label is what gets sent to the AI model — must say "Monitored AIX"
        # so the system-prompt Monitored Scope Exception fires and the model
        # restricts the report to only the projects listed in monitoring_config.json.
        "query_label": "Monitored AIX",
        "scope": "monitored",  # use monitoring_config.json scoped lists
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
    {"name": "DevOps & Networking", "display": "DevOps & Networking", "scope": "full"},
]
