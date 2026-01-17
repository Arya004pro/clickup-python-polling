import os
from dotenv import load_dotenv

# Load variables from .env into environment
load_dotenv()

# =========================
# CLICKUP CONFIG
# =========================
CLICKUP_API_TOKEN = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_TEAM_ID = os.getenv(
    "CLICKUP_TEAM_ID"
)  # Optional, will be fetched dynamically if not set
CLICKUP_SPACE_ID = os.getenv(
    "CLICKUP_SPACE_ID"
)  # Optional, will be fetched dynamically if not set
BASE_URL = "https://api.clickup.com/api/v2"

# =========================
# DATABASE CONFIG (PostgreSQL)
# =========================
DATABASE_URL = os.getenv("DATABASE_URL")

# =========================
# VALIDATION (FAIL FAST)
# =========================
missing = []
if not CLICKUP_API_TOKEN:
    missing.append("CLICKUP_API_TOKEN")
if not DATABASE_URL:
    missing.append("DATABASE_URL")
if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
