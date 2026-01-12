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
# SUPABASE CONFIG
# =========================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

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

if not SUPABASE_URL:
    missing.append("SUPABASE_URL")

if not SUPABASE_SERVICE_ROLE_KEY:
    missing.append("SUPABASE_SERVICE_ROLE_KEY")

if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
