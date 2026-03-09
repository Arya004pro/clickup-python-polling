"""
Generate Reports Step - queue-triggered, generates reports for monitored spaces.

Receives period + label from cron/manual step, then calls the api-server /query endpoint
for each selected space. The api-server uses OpenRouter + ClickUp MCP tools to produce
exactly the same markdown report that is displayed on the localhost web UI.
"""

import asyncio
from typing import Any

import requests
from motia import FlowContext, queue


config = {
    "name": "GenerateReports",
    "description": "Generates space task reports via OpenRouter api-server (matching localhost output)",
    "flows": ["clickup-daily-reports"],
    "triggers": [
        queue("report::generate"),
    ],
    "enqueues": ["report::send-email"],
}

# Map period keys to human-readable phrases used in natural language queries.
_PERIOD_PHRASES = {
    "yesterday": "yesterday",
    "today": "today",
    "this_week": "this week",
    "last_week": "last week",
    "this_month": "this month",
    "last_month": "last month",
}


def _build_query(space_name: str, period: str) -> str:
    """Build the natural language query sent to the api-server."""
    period_phrase = _PERIOD_PHRASES.get(period, period)
    return f"Generate a space task report for {space_name} for {period_phrase}"


def _call_api_server_sync(api_url: str, query: str, timeout_s: int = 360) -> dict:
    """
    Synchronous POST to api-server /query endpoint.
    Sets reset_conversation=True so each space starts with a fresh context.
    """
    try:
        resp = requests.post(
            f"{api_url}/query",
            json={"question": query, "reset_conversation": True},
            timeout=timeout_s,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        return {"status": "error", "error": f"Request timed out after {timeout_s}s"}
    except requests.exceptions.ConnectionError as exc:
        return {
            "status": "error",
            "error": f"Cannot reach api-server: {str(exc)[:160]}",
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}


async def handler(input_data: dict, ctx: FlowContext[Any]) -> None:
    """
    Generate reports for selected spaces via OpenRouter api-server and enqueue for email.
    Reports are generated sequentially (one space at a time) to avoid flooding the api-server.
    """
    from steps.config import API_SERVER_URL, MONITORED_SPACES

    period = input_data.get("period", "today")
    schedule_label = input_data.get("schedule_label", "Report")
    requested_spaces = input_data.get("spaces")
    requested_spaces_lc = {
        str(name).strip().lower()
        for name in (requested_spaces or [])
        if str(name).strip()
    }

    spaces_to_process = [
        s
        for s in MONITORED_SPACES
        if not requested_spaces_lc or s["name"].lower() in requested_spaces_lc
    ]

    if requested_spaces_lc and not spaces_to_process:
        ctx.logger.error(
            f"No monitored spaces matched requested list: {sorted(requested_spaces_lc)}"
        )
        return

    ctx.logger.info(
        f"Generating reports via api-server - period={period}, label={schedule_label}"
    )
    ctx.logger.info(f"API server URL: {API_SERVER_URL}")
    ctx.logger.info(f"Spaces to process: {[s['name'] for s in spaces_to_process]}")

    reports_markdown = []

    for space_cfg in spaces_to_process:
        space_name = space_cfg["name"]
        query = _build_query(space_name, period)

        ctx.logger.info(f"  [{space_name}] Querying api-server: {query!r}")

        # Run synchronous HTTP call in a thread to avoid blocking the async event loop.
        result = await asyncio.to_thread(_call_api_server_sync, API_SERVER_URL, query)

        if result.get("status") == "error":
            error_msg = result.get("error", "Unknown error")
            ctx.logger.error(f"  [FAIL] {space_name}: {error_msg}")
            reports_markdown.append(
                {
                    "space": space_name,
                    "markdown": None,
                    "error": error_msg,
                }
            )
        else:
            markdown = result.get("response") or ""
            ctx.logger.info(
                f"  [OK] {space_name}: received {len(markdown):,} chars of markdown"
            )
            reports_markdown.append(
                {
                    "space": space_name,
                    "markdown": markdown,
                    "error": None,
                }
            )

    ctx.logger.info(
        f"All {len(reports_markdown)} space reports done. Enqueuing email step..."
    )

    await ctx.enqueue(
        {
            "topic": "report::send-email",
            "data": {
                "reports_markdown": reports_markdown,
                "schedule_label": schedule_label,
            },
        }
    )
