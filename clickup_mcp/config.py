import os

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


if load_dotenv:
    load_dotenv()


CLICKUP_API_TOKEN = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID")
CLICKUP_SPACE_ID = os.getenv("CLICKUP_SPACE_ID")
BASE_URL = os.getenv("CLICKUP_BASE_URL", "https://api.clickup.com/api/v2")


if not CLICKUP_API_TOKEN:
    raise RuntimeError("Missing required environment variable: CLICKUP_API_TOKEN")
