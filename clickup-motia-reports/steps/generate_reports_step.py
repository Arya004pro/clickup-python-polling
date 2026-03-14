"""
Generate Reports Step - queue-triggered, generates reports for monitored spaces.

Receives period + label from cron/manual step, then calls the api-server /query endpoint
for each selected space. The api-server uses OpenRouter + ClickUp MCP tools to produce
exactly the same markdown report that is displayed on the localhost web UI.
"""

import asyncio
import os
import time
from datetime import datetime, timezone
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


def _call_api_server_sync(
    api_url: str,
    space_name: str,
    period: str,
    timeout_s: int = 1200,
    use_direct_endpoint: bool = True,
) -> dict:
    """
    Synchronous POST to api-server endpoint for report generation.
    Fast path uses /report/space (direct MCP tool call, no LLM round-trip).
    Falls back to /query when needed.
    """
    try:
        if use_direct_endpoint:
            resp = requests.post(
                f"{api_url}/report/space",
                json={
                    "space_name": space_name,
                    "period_type": period,
                    "include_archived": True,
                },
                timeout=timeout_s,
            )
            resp.raise_for_status()
            return resp.json()

        query = _build_query(space_name, period)
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

_DEDUP_WINDOW_S = 300  # 5 minutes - suppress duplicate triggers within this window




async def handler(input_data: dict, ctx: FlowContext[Any]) -> None:
    """
    Generate reports for selected spaces via OpenRouter api-server and enqueue for email.
    Reports are generated sequentially (one space at a time) to avoid flooding the api-server.
    """
    from steps.config import API_SERVER_URL, MONITORED_SPACES

    period = input_data.get("period", "today")
    schedule_label = input_data.get("schedule_label", "Report")
    trigger_epoch_ms = int(input_data.get("triggered_at_epoch_ms") or time.time() * 1000)
    trigger_source = str(input_data.get("trigger_source") or "unknown")
    trigger_iso = str(
        input_data.get("triggered_at_iso")
        or datetime.fromtimestamp(trigger_epoch_ms / 1000, tz=timezone.utc).isoformat()
    )
    generation_started_epoch_ms = int(time.time() * 1000)
    generation_started_iso = datetime.now(timezone.utc).isoformat()

    # --- Idempotency guard: skip if the same schedule already fired recently ---
    # This prevents duplicate emails when both the iii CronModule and the motia
    # Python worker fire the same cron step (two triggers → one email).
    dedup_key = f"{schedule_label}::{period}"
    last_run = await ctx.state.get("report_last_run", dedup_key)
    now_ts = time.time()
    if last_run is not None:
        elapsed = now_ts - float(last_run)
        if elapsed < _DEDUP_WINDOW_S:
            ctx.logger.warning(
                f"[DEDUP] Skipping duplicate trigger for '{dedup_key}' "
                f"(last ran {elapsed:.0f}s ago, window={_DEDUP_WINDOW_S}s)"
            )
            return
    await ctx.state.set("report_last_run", dedup_key, now_ts)
    # -------------------------------------------------------------------------
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
    ctx.logger.info(
        f"Trigger meta - source={trigger_source}, triggered_at={trigger_iso}"
    )
    ctx.logger.info(f"API server URL: {API_SERVER_URL}")
    ctx.logger.info(f"Spaces to process: {[s['name'] for s in spaces_to_process]}")

    # Controlled parallelism can reduce end-to-end runtime significantly.
    # Keep this bounded to avoid overloading api-server/OpenRouter.
    try:
        report_concurrency = int(os.getenv("REPORT_CONCURRENCY", "2"))
    except ValueError:
        report_concurrency = 2
    report_concurrency = max(1, min(report_concurrency, 4))
    ctx.logger.info(f"Report concurrency: {report_concurrency}")
    use_direct_endpoint = (
        os.getenv("REPORT_API_MODE", "direct").strip().lower() != "llm"
    )
    ctx.logger.info(
        f"Report API mode: {'direct (/report/space)' if use_direct_endpoint else 'llm (/query)'}"
    )

    semaphore = asyncio.Semaphore(report_concurrency)

    async def _process_space(space_cfg: dict) -> dict:
        space_name = space_cfg["name"]
        # Use query_label when set (e.g. "Monitored AIX" for the AIX monitored scope)
        # so the AI model applies the correct Monitored Scope Exception from the system prompt.
        query_label = space_cfg.get("query_label") or space_name
        started_at = time.perf_counter()
        async with semaphore:
            if use_direct_endpoint:
                ctx.logger.info(
                    f"  [{space_name}] Querying api-server direct endpoint (/report/space)"
                )
            else:
                query = _build_query(query_label, period)
                ctx.logger.info(f"  [{space_name}] Querying api-server: {query!r}")
            result = await asyncio.to_thread(
                _call_api_server_sync,
                API_SERVER_URL,
                query_label,
                period,
                360,
                use_direct_endpoint,
            )
        elapsed_s = round(time.perf_counter() - started_at, 2)

        if result.get("status") == "error":
            error_msg = result.get("error", "Unknown error")
            ctx.logger.error(f"  [FAIL] {space_name}: {error_msg} ({elapsed_s}s)")
            return {
                "space": space_name,
                "markdown": None,
                "error": error_msg,
                "elapsed_s": elapsed_s,
            }

        markdown = result.get("response") or ""
        ctx.logger.info(
            f"  [OK] {space_name}: received {len(markdown):,} chars of markdown ({elapsed_s}s)"
        )
        return {
            "space": space_name,
            "markdown": markdown,
            "error": None,
            "elapsed_s": elapsed_s,
        }

    reports_markdown = await asyncio.gather(
        *[_process_space(s) for s in spaces_to_process]
    )
    generation_finished_epoch_ms = int(time.time() * 1000)
    generation_elapsed_s = round(
        (generation_finished_epoch_ms - generation_started_epoch_ms) / 1000, 2
    )

    ctx.logger.info(
        f"All {len(reports_markdown)} space reports done in {generation_elapsed_s}s. Enqueuing email step..."
    )

    await ctx.enqueue(
        {
            "topic": "report::send-email",
            "data": {
                "reports_markdown": reports_markdown,
                "schedule_label": schedule_label,
                "timing_meta": {
                    "trigger_source": trigger_source,
                    "period": period,
                    "schedule_label": schedule_label,
                    "triggered_at_epoch_ms": trigger_epoch_ms,
                    "triggered_at_iso": trigger_iso,
                    "generation_started_epoch_ms": generation_started_epoch_ms,
                    "generation_started_iso": generation_started_iso,
                    "generation_finished_epoch_ms": generation_finished_epoch_ms,
                    "generation_finished_iso": datetime.now(timezone.utc).isoformat(),
                    "generation_elapsed_s": generation_elapsed_s,
                    "report_concurrency": report_concurrency,
                    "report_api_mode": "direct" if use_direct_endpoint else "llm",
                },
            },
        }
    )
