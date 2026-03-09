"""
Quick test: generate today's AIX report via the api-server and email it.

Usage:
    python test_aix_report.py

What it does:
  1. Calls api-server (localhost:8003) to generate the AIX report for today
     (same OpenRouter call as clicking "Send Query" on the localhost page)
  2. Prints the markdown to the terminal so you can compare with localhost
  3. Sends the same HTML-formatted email as the Motia daily reports

Pre-requisites: Docker containers must be running (docker compose up).
"""

import os
import sys

import requests
from dotenv import load_dotenv

# Allow importing email_sender from the motia steps folder
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "clickup-motia-reports"))
from steps.email_sender import send_report_email  # noqa: E402

load_dotenv()

API_URL = "http://localhost:8003"
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_EMAIL = os.getenv("SMTP_EMAIL", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_TO = os.getenv("SMTP_TO", "aryapatel.eng@gmail.com")

QUERY = "Generate a space task report for AIX for today"
SCHEDULE_LABEL = "AIX Report - Today"


def generate_report() -> str:
    print(f"[1/3] Calling api-server: {QUERY!r}")
    print(f"      URL: {API_URL}/query  (timeout: 360s - may take a few minutes)")
    resp = requests.post(
        f"{API_URL}/query",
        json={"question": QUERY, "reset_conversation": True},
        timeout=360,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "error":
        print(f"[ERROR] api-server returned error: {data.get('error')}")
        sys.exit(1)
    markdown = data.get("response", "")
    print(f"[1/3] Done - received {len(markdown):,} chars of markdown\n")
    return markdown


def print_report(markdown: str):
    print("=" * 72)
    print("REPORT  (compare this with what you see on http://localhost:8003)")
    print("=" * 72)
    try:
        # Windows terminals may default to cp1252 and fail on unicode arrows/emojis.
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        print(markdown)
    except UnicodeEncodeError:
        print(markdown.encode("utf-8", errors="replace").decode("utf-8", errors="replace"))

    print("=" * 72 + "\n")


def send_email(markdown: str):
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        print("[SKIP] SMTP credentials not set - skipping email.")
        return

    print(f"[3/3] Sending HTML-formatted email to {SMTP_TO} ...")
    result = send_report_email(
        reports_markdown=[{"space": "AIX", "markdown": markdown, "error": None}],
        schedule_label=SCHEDULE_LABEL,
        smtp_host=SMTP_HOST,
        smtp_port=SMTP_PORT,
        smtp_email=SMTP_EMAIL,
        smtp_password=SMTP_PASSWORD,
        to_email=SMTP_TO,
    )
    if result["status"] == "sent":
        print(f"[3/3] Email sent!  Subject: {result['subject']}")
        print(f"\nCheck your inbox (and spam folder) at {SMTP_TO}")
    else:
        print(f"[ERROR] Email failed: {result.get('error')}")


if __name__ == "__main__":
    markdown = generate_report()
    print("[2/3] Printing report to terminal for comparison ...")
    print_report(markdown)
    send_email(markdown)
