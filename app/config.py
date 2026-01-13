import os
from dotenv import load_dotenv

# Load variables from .env into environment
load_dotenv()

# =========================
# CLICKUP CONFIG
# =========================
CLICKUP_API_TOKEN = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID")
CLICKUP_SPACE_ID = os.getenv("CLICKUP_SPACE_ID")
BASE_URL = "https://api.clickup.com/api/v2"

# =========================
# DATABASE CONFIG (PostgreSQL)
# =========================
DATABASE_URL = os.getenv("DATABASE_URL")

# =========================
# POLLING CONFIG
# =========================
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))

# =========================
# VALIDATION (FAIL FAST)
# =========================
missing = []

if not CLICKUP_API_TOKEN:
    missing.append("CLICKUP_API_TOKEN")

if not CLICKUP_TEAM_ID:
    missing.append("CLICKUP_TEAM_ID")

# CLICKUP_SPACE_ID is now optional - if not set, all spaces will be synced

if not DATABASE_URL:
    missing.append("DATABASE_URL")

if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
