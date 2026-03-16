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
from typing import Any, Optional

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


def _looks_like_report_markdown(text: str) -> bool:
    """
    Validate that content resembles task report formatted_output markdown.
    This filters out lookup-only replies such as find_project_anywhere summaries.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if lowered.startswith("error:"):
        return False
    if lowered.startswith("report job ") and "did not complete within the timeout" in lowered:
        return False

    report_headers = (
        "## space report:",
        "## project report:",
        "## member report:",
        "## low hours report",
        "## missing estimation report",
        "## overtime report",
    )
    if not any(h in lowered for h in report_headers):
        return False
    if "**period:**" not in lowered and "|  **period:**" not in lowered:
        return False
    return True


def _space_dedup_key(space_cfg: dict) -> str:
    """
    Build canonical dedup key for a report-space config row.
    Handles aliases like "AIX" + query_label "Monitored AIX".
    """
    name = str(space_cfg.get("name") or "").strip()
    query_label = str(space_cfg.get("query_label") or "").strip()
    scope = str(space_cfg.get("scope") or "full").strip().lower()
    target = (query_label or name).strip().lower()
    if scope == "monitored":
        if target.startswith("monitored:"):
            target = target.split(":", 1)[1].strip()
        elif target.startswith("monitored "):
            target = target[len("monitored ") :].strip()
    return f"{scope}::{target}"


def _monitored_spaces_from_env() -> set[str]:
    """
    Infer monitored spaces generically from MONITORING_CONFIG_JSON env payload.
    Expected shape includes monitored_projects[].space.
    """
    raw = os.getenv("MONITORING_CONFIG_JSON", "").strip()
    if not raw:
        return set()
    try:
        import json

        payload = json.loads(raw)
    except Exception:
        return set()

    spaces: set[str] = set()
    for item in payload.get("monitored_projects", []) or []:
        space_name = str(item.get("space") or "").strip()
        if space_name:
            spaces.add(space_name.lower())
    return spaces


def _build_query(
    space_name: str,
    period: str,
    custom_start: Optional[str] = None,
    custom_end: Optional[str] = None,
    scope: str = "full",
    retry_after_lookup: bool = False,
) -> str:
    """Build the natural language query sent to the api-server."""
    scope_norm = (scope or "full").strip().lower()
    if scope_norm == "monitored":
        guidance = (
            "Scheduled automation mode for monitored scope. "
            "This space uses monitoring_config filtered projects, not the full raw space. "
            "Call get_space_task_report directly with space_name exactly as provided. "
            "Do NOT convert it to plain AIX. Do NOT call find_project_anywhere. "
            "Return only formatted_output."
        )
    else:
        guidance = (
            "Scheduled automation mode. First call find_project_anywhere for entity resolution. "
            "Then continue in the same request and call get_space_task_report with the resolved space_name. "
            "Do not stop after lookup. Return only formatted_output."
        )
        if retry_after_lookup:
            guidance = (
                "Scheduled automation retry. You may have stopped after find_project_anywhere previously. "
                "Now continue and call get_space_task_report for this space immediately. "
                "Return only formatted_output."
            )

    if period == "custom" and custom_start and custom_end:
        period_phrase = f"{custom_start} to {custom_end}"
        return f"Generate a space task report for {space_name} for {period_phrase}. {guidance}"
    period_phrase = _PERIOD_PHRASES.get(period, period)
    return f"Generate a space task report for {space_name} for {period_phrase}. {guidance}"


def _call_api_server_sync(
    api_url: str,
    space_name: str,
    period: str,
    custom_start: Optional[str] = None,
    custom_end: Optional[str] = None,
    scope: str = "full",
    retry_after_lookup: bool = False,
) -> dict:
    """
    Synchronous POST to api-server /query endpoint for LLM report generation.
    """
    try:
        query = _build_query(
            space_name,
            period,
            custom_start,
            custom_end,
            scope=scope,
            retry_after_lookup=retry_after_lookup,
        )
        resp = requests.post(
            f"{api_url}/query",
            json={"question": query, "reset_conversation": True},
            timeout=None,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "error":
            return data

        report_saved = bool(data.get("report_saved"))
        response_text = str(data.get("response") or "")

        # If /query returns good markdown and it was saved, accept immediately.
        if report_saved and _looks_like_report_markdown(response_text):
            return data

        # If /query returns empty/non-report content but includes a saved report URL,
        # hydrate response from the saved markdown file.
        download_url = str(data.get("report_download_url") or "").strip() if report_saved else ""
        if download_url:
            full_url = f"{api_url}{download_url}" if download_url.startswith("/") else download_url
            try:
                dl = requests.get(
                    full_url,
                    timeout=None,
                )
                if dl.status_code == 200 and (dl.text or "").strip():
                    data["response"] = dl.text
                    # Accept saved-report payload even if markdown validator is strict;
                    # dashboard visibility is guaranteed by report_saved=true.
                    return data
                data["download_error"] = (
                    f"Report download returned {dl.status_code}: {dl.text[:120]}"
                )
            except Exception as exc:
                data["download_error"] = str(exc)[:160]
        if report_saved:
            # Avoid regenerating the same space report when file was already saved.
            return data
        return data
    except requests.exceptions.Timeout:
        return {"status": "error", "error": "Request timed out"}
    except requests.exceptions.ConnectionError as exc:
        return {
            "status": "error",
            "error": f"Cannot reach api-server: {str(exc)[:160]}",
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}


_DEDUP_WINDOW_S = 300  # 5 minutes - suppress duplicate triggers within this window
_RUN_LOCK = asyncio.Lock()  # Process-local guard: only one generate handler at a time
_RUN_LOCK_STALE_S = 7200  # 2h stale safety for distributed state marker


async def handler(input_data: dict, ctx: FlowContext[Any]) -> None:
    """
    Generate reports for selected spaces via OpenRouter api-server and enqueue for email.
    Reports are generated sequentially (one space at a time) to avoid flooding the api-server.
    """
    from steps.config import API_SERVER_URL, MONITORED_SPACES

    period = input_data.get("period", "today")
    custom_start = (str(input_data.get("custom_start") or "").strip() or None)
    custom_end = (str(input_data.get("custom_end") or "").strip() or None)
    schedule_label = input_data.get("schedule_label", "Report")
    trigger_epoch_ms = int(
        input_data.get("triggered_at_epoch_ms") or time.time() * 1000
    )
    trigger_source = str(input_data.get("trigger_source") or "unknown")
    trigger_iso = str(
        input_data.get("triggered_at_iso")
        or datetime.fromtimestamp(trigger_epoch_ms / 1000, tz=timezone.utc).isoformat()
    )
    generation_started_epoch_ms = int(time.time() * 1000)
    generation_started_iso = datetime.now(timezone.utc).isoformat()

    # --- Single-run guard: prevent overlapping generation batches ---
    run_marker_key = "global"
    now_epoch_s = time.time()
    existing_run = await ctx.state.get("report_generation_in_progress", run_marker_key)
    if isinstance(existing_run, dict):
        started_at = float(existing_run.get("started_at_epoch_s") or 0)
        age = max(0.0, now_epoch_s - started_at)
        if age < _RUN_LOCK_STALE_S:
            ctx.logger.warn(
                "[DEDUP] Skipping trigger because another report generation run is active "
                f"(age={age:.0f}s, source={existing_run.get('trigger_source')}, "
                f"label={existing_run.get('schedule_label')})"
            )
            return
    # -------------------------------------------------------------------------

    # --- Idempotency guard: skip if the same schedule already fired recently ---
    # This prevents duplicate emails when both the iii CronModule and the motia
    # Python worker fire the same cron step (two triggers → one email).
    dedup_key = f"{schedule_label}::{period}"
    if period == "custom" and custom_start and custom_end:
        dedup_key = f"{dedup_key}::{custom_start}::{custom_end}"
    last_run = await ctx.state.get("report_last_run", dedup_key)
    now_ts = time.time()
    if last_run is not None:
        elapsed = now_ts - float(last_run)
        if elapsed < _DEDUP_WINDOW_S:
            ctx.logger.warn(
                f"[DEDUP] Skipping duplicate trigger for '{dedup_key}' "
                f"(last ran {elapsed:.0f}s ago, window={_DEDUP_WINDOW_S}s)"
            )
            return
    await ctx.state.set("report_last_run", dedup_key, now_ts)
    # -------------------------------------------------------------------------
    requested_spaces = input_data.get("spaces")
    requested_spaces_list = [
        str(name).strip() for name in (requested_spaces or []) if str(name).strip()
    ]
    requested_spaces_lc = {
        name.lower() for name in requested_spaces_list
    }

    # Build lookup from configured spaces.
    configured_by_name: dict[str, dict] = {}
    for s in MONITORED_SPACES:
        key = str(s.get("name") or "").strip().lower()
        if key and key not in configured_by_name:
            configured_by_name[key] = s
    monitored_spaces_lc = _monitored_spaces_from_env()

    if requested_spaces_lc:
        # Follow trigger payload order exactly. If a requested space is missing in config,
        # synthesize a safe default entry so one bad REPORT_SPACES_JSON cannot truncate runs.
        spaces_to_process = []
        for requested_name in requested_spaces_list:
            key = requested_name.lower()
            cfg = configured_by_name.get(key)
            if cfg:
                spaces_to_process.append(cfg)
                continue
            synthesized = {
                "name": requested_name,
                "display": requested_name,
                "scope": "full",
            }
            if key in monitored_spaces_lc:
                synthesized["query_label"] = f"Monitored {requested_name}"
                synthesized["scope"] = "monitored"
            ctx.logger.warn(
                "[CONFIG] Requested space missing from MONITORED_SPACES/REPORT_SPACES_JSON; "
                f"using synthesized config: {synthesized}"
            )
            spaces_to_process.append(synthesized)
    else:
        spaces_to_process = list(MONITORED_SPACES)

    # Defensive dedup: runtime REPORT_SPACES_JSON can accidentally contain duplicate
    # entries (common in Railway env edits). Keep first occurrence by canonical target.
    deduped_spaces: list[dict] = []
    seen_space_targets: set[str] = set()
    for space_cfg in spaces_to_process:
        key = _space_dedup_key(space_cfg)
        if key.endswith("::"):
            continue
        if key in seen_space_targets:
            ctx.logger.warn(
                "[DEDUP] Skipping duplicate configured space entry: "
                f"{space_cfg.get('name')} (key={key})"
            )
            continue
        seen_space_targets.add(key)
        deduped_spaces.append(space_cfg)
    spaces_to_process = deduped_spaces

    if requested_spaces_lc and not spaces_to_process:
        ctx.logger.error(
            f"No monitored spaces matched requested list: {sorted(requested_spaces_lc)}"
        )
        return

    ctx.logger.info(
        f"Generating reports via api-server - period={period}, label={schedule_label}"
    )
    if period == "custom" and custom_start and custom_end:
        ctx.logger.info(f"Custom period range: {custom_start} to {custom_end}")
    ctx.logger.info(
        f"Trigger meta - source={trigger_source}, triggered_at={trigger_iso}"
    )
    ctx.logger.info(f"API server URL: {API_SERVER_URL}")
    ctx.logger.info(
        "Spaces to process: "
        + str(
            [
                {
                    "name": s.get("name"),
                    "query_label": s.get("query_label"),
                    "scope": s.get("scope", "full"),
                }
                for s in spaces_to_process
            ]
        )
    )

    # Strictly sequential mode by design: one space at a time.
    report_concurrency = 1
    ctx.logger.info("Report concurrency: 1 (strict sequential mode)")
    ctx.logger.info("Report API mode: llm (/query) [direct disabled]")
    try:
        max_attempts = int(os.getenv("REPORT_LLM_MAX_ATTEMPTS", "3"))
    except ValueError:
        max_attempts = 3
    max_attempts = max(1, min(max_attempts, 6))
    ctx.logger.info(f"LLM max attempts per space: {max_attempts}")

    async def _process_space(space_cfg: dict) -> dict:
        space_name = space_cfg["name"]
        # Use query_label when set (e.g. "Monitored AIX" for the AIX monitored scope)
        # so the AI model applies the correct Monitored Scope Exception from the system prompt.
        query_label = space_cfg.get("query_label") or space_name
        scope = str(space_cfg.get("scope") or "full").strip().lower()
        started_at = time.perf_counter()
        last_error = "Unknown error"

        for attempt in range(1, max_attempts + 1):
            query = _build_query(
                query_label,
                period,
                custom_start,
                custom_end,
                scope=scope,
                retry_after_lookup=(attempt > 1),
            )
            ctx.logger.info(
                f"  [{space_name}] Attempt {attempt}/{max_attempts} via api-server: {query!r}"
            )
            result = await asyncio.to_thread(
                _call_api_server_sync,
                API_SERVER_URL,
                query_label,
                period,
                custom_start,
                custom_end,
                scope,
                attempt > 1,
            )

            elapsed_s = round(time.perf_counter() - started_at, 2)

            if result.get("status") == "error":
                last_error = str(result.get("error") or "Unknown error").strip()
                ctx.logger.warn(
                    f"  [WARN] {space_name}: attempt {attempt} returned error ({last_error})"
                )
                continue

            markdown = result.get("response") or ""
            report_saved = bool(result.get("report_saved"))
            if report_saved:
                if not str(markdown).strip():
                    markdown = (
                        f"## Space Report: {space_name}\n\n"
                        "_Report file was saved, but inline markdown was empty in /query response._"
                    )
                if not _looks_like_report_markdown(markdown):
                    ctx.logger.warn(
                        f"  [WARN] {space_name}: saved report had non-standard markdown shape; accepting saved file."
                    )
                ctx.logger.info(
                    f"  [OK] {space_name}: report generated on attempt {attempt} ({len(markdown):,} chars, {elapsed_s}s)"
                )
                return {
                    "space": space_name,
                    "markdown": markdown,
                    "error": None,
                    "elapsed_s": elapsed_s,
                }

            hint = str(
                result.get("response")
                or result.get("error")
                or result.get("download_error")
                or ""
            ).strip()
            if hint:
                hint = hint.replace("\n", " ")[:180]
            last_error = (
                "LLM returned non-report output or did not save report file."
                + (f" Hint: {hint}" if hint else "")
            )
            ctx.logger.warn(
                f"  [WARN] {space_name}: attempt {attempt} did not produce saved report."
            )

        elapsed_s = round(time.perf_counter() - started_at, 2)
        ctx.logger.error(
            f"  [FAIL] {space_name}: failed after {max_attempts} attempts ({last_error}) ({elapsed_s}s)"
        )
        return {
            "space": space_name,
            "markdown": None,
            "error": f"Failed after {max_attempts} attempts. {last_error}",
            "elapsed_s": elapsed_s,
        }

    reports_markdown: list[dict] = []
    async with _RUN_LOCK:
        await ctx.state.set(
            "report_generation_in_progress",
            run_marker_key,
            {
                "started_at_epoch_s": now_epoch_s,
                "started_at_iso": generation_started_iso,
                "trigger_source": trigger_source,
                "schedule_label": schedule_label,
                "period": period,
            },
        )
        try:
            for idx, space_cfg in enumerate(spaces_to_process, start=1):
                ctx.logger.info(
                    f"[SEQ] Processing space {idx}/{len(spaces_to_process)}: {space_cfg.get('name')}"
                )
                reports_markdown.append(await _process_space(space_cfg))
        finally:
            # Mark run complete even if exceptions happen.
            await ctx.state.set("report_generation_in_progress", run_marker_key, None)

    generation_finished_epoch_ms = int(time.time() * 1000)
    generation_elapsed_s = round(
        (generation_finished_epoch_ms - generation_started_epoch_ms) / 1000, 2
    )

    ctx.logger.info(
        f"All {len(reports_markdown)} space report attempts finished in {generation_elapsed_s}s."
    )

    failed_reports = [
        r for r in reports_markdown if r.get("error") or not str(r.get("markdown") or "").strip()
    ]
    if failed_reports:
        failed_spaces = [str(r.get("space") or "unknown") for r in failed_reports]
        ctx.logger.error(
            f"[BLOCKED] Not enqueuing email because {len(failed_spaces)} required spaces failed: {failed_spaces}"
        )
        await ctx.state.set(
            "report_generation_last_failure",
            schedule_label,
            {
                "failed_spaces": failed_spaces,
                "reports_markdown": reports_markdown,
                "timing_meta": {
                    "trigger_source": trigger_source,
                    "period": period,
                    "schedule_label": schedule_label,
                    "generation_finished_iso": datetime.now(timezone.utc).isoformat(),
                    "generation_elapsed_s": generation_elapsed_s,
                    "report_api_mode": "llm",
                },
            },
        )
        return

    ctx.logger.info("All required space reports generated. Enqueuing email step...")

    await ctx.enqueue(
        {
            "topic": "report::send-email",
            "data": {
                "reports_markdown": reports_markdown,
                "schedule_label": schedule_label,
                "timing_meta": {
                    "trigger_source": trigger_source,
                    "period": period,
                    "custom_start": custom_start,
                    "custom_end": custom_end,
                    "schedule_label": schedule_label,
                    "triggered_at_epoch_ms": trigger_epoch_ms,
                    "triggered_at_iso": trigger_iso,
                    "generation_started_epoch_ms": generation_started_epoch_ms,
                    "generation_started_iso": generation_started_iso,
                    "generation_finished_epoch_ms": generation_finished_epoch_ms,
                    "generation_finished_iso": datetime.now(timezone.utc).isoformat(),
                    "generation_elapsed_s": generation_elapsed_s,
                    "report_concurrency": report_concurrency,
                    "report_api_mode": "llm",
                },
            },
        }
    )
