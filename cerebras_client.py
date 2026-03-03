"""
ClickUp MCP Server — Cerebras Client
=====================================
Model    : gpt-oss-120b  (128k context — sole model, no fallback)

Features
--------
• Smart two-phase polling  (status ping every 3 s, result fetched instantly)
• Per-report timing: logs start → end → duration for every report job
• Full session summary on exit  (tokens, API calls, tool calls, reports, durations)
• Verbatim formatted_output rendering — never truncated, never re-formatted
• Minimal tool injection per request type (reduces hallucinated calls)
"""

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.sse import sse_client
from openai import AsyncOpenAI, RateLimitError, APIStatusError

load_dotenv()

# ── Windows ANSI colours ──────────────────────────────────────────────────────
if sys.platform == "win32":
    os.system("color")

# ── ANSI ─────────────────────────────────────────────────────────────────────
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"
WHITE = "\033[97m"


def col(colour: str, text: str) -> str:
    return f"{colour}{text}{RESET}"


def sep(char: str = "─", width: int = 72) -> str:
    return col(DIM, char * width)


# ── CONFIG ────────────────────────────────────────────────────────────────────
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/sse")

ROOT_DIR = Path(__file__).parent
CEREBRAS_PROMPT_FILE = ROOT_DIR / "cerebras_system_prompt.md"

# ── Report storage — hardcoded, NOT from .env ─────────────────────────────────
REPORTS_DIR = Path("D:/AI_Reports")   # always D:/AI_Reports — created automatically

# ── Single model — no fallback chain ─────────────────────────────────────────
# gpt-oss-120b is the sole model used. 128k context, deterministic tool calls.
ACTIVE_MODEL = "gpt-oss-120b"

# Tools excluded from ALL requests — get_space only accepts numeric space_id;
# models always pass space_name/period_type causing validation errors.
EXCLUDED_TOOLS = {"get_space"}

# Client-side redirect — silently rewrite banned tool calls to the correct tool.
TOOL_REDIRECT = {
    "get_space": {
        "trigger_args": {"space_name", "period_type", "include_archived", "async_job"},
        "redirect_to":  "get_space_task_report",
        "keep_args":    {"space_name", "period_type", "include_archived",
                         "custom_start", "custom_end", "rolling_days"},
    },
}

# ── Tool routing: per-intent minimal tool sets ────────────────────────────────
# Only tools explicitly listed for a given intent are exposed to the model.
# This prevents hallucinated calls to unrelated tools.
INTENT_TOOL_SETS: dict = {
    "space_report":   {"find_project_anywhere", "get_space_task_report"},
    "project_report": {"find_project_anywhere", "get_project_task_report"},
    "member_report":  {"find_project_anywhere", "get_member_task_report"},
    "low_hours":      {"get_low_hours_report"},
    "overtracked":    {"get_overtracked_report"},
    "missing_est":    {"get_missing_estimation_report"},
    "default":        None,   # None = expose all tools (minus EXCLUDED_TOOLS)
}

RATE_LIMIT_CODES = {429, 503}
MAX_POLL_RETRIES = 5
MAX_TURN_TOOL_CALLS = 20
MAX_SAME_TOOL_FAILURES = 2
STATUS_CHECK_INTERVAL_S = 3
STATUS_CHECK_TIMEOUT_S = 300

MODEL_CONTEXT_WINDOW = 128_000   # gpt-oss-120b
CHARS_PER_TOKEN = 4
OUTPUT_RESERVE = 4096   # reserve enough for full report output


# ── SESSION STATS ─────────────────────────────────────────────────────────────
@dataclass
class ReportTiming:
    """Records wall-clock start/end for a single report job."""

    label: str
    job_id: str
    started_at: float
    ended_at: Optional[float] = None

    @property
    def duration_s(self) -> Optional[int]:
        if self.ended_at is None:
            return None
        return int(self.ended_at - self.started_at)

    def finish(self):
        self.ended_at = time.time()

    def fmt_duration(self) -> str:
        s = self.duration_s
        if s is None:
            return "in-progress"
        m, sec = divmod(s, 60)
        return f"{m}m {sec}s" if m else f"{sec}s"

    def fmt_start(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.started_at))

    def fmt_end(self) -> str:
        if self.ended_at is None:
            return "—"
        return time.strftime("%H:%M:%S", time.localtime(self.ended_at))


@dataclass
class SessionStats:
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_api_calls: int = 0
    tool_calls_made: int = 0
    reports_saved: int = 0
    models_used: dict = field(default_factory=dict)
    report_timings: list = field(default_factory=list)  # List[ReportTiming]
    start_time: float = field(default_factory=time.time)

    def record(self, model: str, inp: int, out: int):
        self.total_input_tokens += inp
        self.total_output_tokens += out
        self.total_api_calls += 1
        self.models_used[model] = self.models_used.get(model, 0) + 1

    def record_tool(self):
        self.tool_calls_made += 1

    def start_report(self, label: str, job_id: str) -> ReportTiming:
        rt = ReportTiming(label=label, job_id=job_id, started_at=time.time())
        self.report_timings.append(rt)
        return rt

    def elapsed(self) -> str:
        s = int(time.time() - self.start_time)
        m, sec = divmod(s, 60)
        return f"{m}m {sec}s"

    def print_summary(self):
        total = self.total_input_tokens + self.total_output_tokens
        print()
        print(sep("="))
        print(col(BOLD + WHITE, "  Session Summary — Cerebras / gpt-oss-120b"))
        print(sep("-"))
        print(
            f"  {col(CYAN, 'Session started')}   : "
            f"{col(WHITE, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.start_time)))}"
        )
        print(
            f"  {col(CYAN, 'Session ended')}     : "
            f"{col(WHITE, time.strftime('%Y-%m-%d %H:%M:%S'))}"
        )
        print(f"  {col(CYAN, 'Duration')}          : {col(WHITE, self.elapsed())}")
        print(sep("-"))
        print(
            f"  {col(CYAN, 'API calls')}         : {col(WHITE, str(self.total_api_calls))}"
        )
        print(
            f"  {col(CYAN, 'MCP tool calls')}    : {col(WHITE, str(self.tool_calls_made))}"
        )
        print(
            f"  {col(CYAN, 'Input tokens')}      : {col(GREEN, f'{self.total_input_tokens:,}')}"
        )
        print(
            f"  {col(CYAN, 'Output tokens')}     : {col(YELLOW, f'{self.total_output_tokens:,}')}"
        )
        print(f"  {col(CYAN, 'Total tokens')}      : {col(WHITE, f'{total:,}')}")
        print(
            f"  {col(CYAN, 'Reports saved')}     : {col(GREEN, str(self.reports_saved))}"
        )
        if self.models_used:
            parts = "  ".join(
                f"{col(MAGENTA, m)} x{n}" for m, n in self.models_used.items()
            )
            print(f"  {col(CYAN, 'Models used')}       :  {parts}")

        # ── Report timings ────────────────────────────────────────────────────
        if self.report_timings:
            print(sep("-"))
            print(col(BOLD + WHITE, "  Report Generation Log"))
            print(sep("-"))
            for i, rt in enumerate(self.report_timings, 1):
                status_col = (
                    col(GREEN, rt.fmt_duration())
                    if rt.ended_at
                    else col(YELLOW, "in-progress")
                )
                short_id = rt.job_id[:16] + "..." if len(rt.job_id) > 16 else rt.job_id
                label_col = col(CYAN, rt.label[:38])
                print(
                    f"  [{i:02d}] {label_col:<40}"
                    f"  started {col(WHITE, rt.fmt_start())}"
                    f"  ended {col(WHITE, rt.fmt_end())}"
                    f"  took {status_col}"
                )
                print(f"       {col(DIM, f'job_id: {short_id}')}")

        print(sep("="))


# ── HELPERS ───────────────────────────────────────────────────────────────────


def load_system_prompt() -> str:
    if CEREBRAS_PROMPT_FILE.exists():
        text = CEREBRAS_PROMPT_FILE.read_text(encoding="utf-8").strip()
        print(
            f"  {col(GREEN, 'OK')} System prompt     : "
            f"{col(MAGENTA, CEREBRAS_PROMPT_FILE.name)} "
            f"{col(DIM, f'({len(text):,} chars)')}"
        )
        return text

    print(f"  {col(YELLOW, '!!')} No prompt file found — using built-in fallback.")
    return (
        "You are a deterministic tool-driven report generator. "
        "Call ONE tool per turn. "
        "Render formatted_output verbatim when present. "
        "Never wrap in code fences."
    )


def mcp_tools_to_openai(tools, allowed: Optional[set] = None) -> list:
    """
    Convert MCP tools to OpenAI function-call format.
    EXCLUDED_TOOLS are always filtered out.
    If `allowed` is a set of tool names, only those tools are returned
    (intent-based minimal exposure — reduces hallucinated calls).
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema or {"type": "object", "properties": {}},
            },
        }
        for t in tools
        if t.name not in EXCLUDED_TOOLS
        and (allowed is None or t.name in allowed)
    ]


def is_model_not_found(exc: Exception) -> bool:
    """404 model_not_found is a hard config error — never rotate on it."""
    if isinstance(exc, APIStatusError) and exc.status_code == 404:
        return True
    msg = str(exc).lower()
    return "model_not_found" in msg or "does not exist" in msg


def is_payment_required(exc: Exception) -> bool:
    """402 payment_required means the account has no credits — hard stop."""
    if isinstance(exc, APIStatusError) and exc.status_code == 402:
        return True
    msg = str(exc).lower()
    return (
        "payment_required" in msg
        or "payment required" in msg
        or "visit your billing" in msg
    )


def is_rate_limit(exc: Exception) -> bool:
    # Hard stops — never rotate on these
    if is_model_not_found(exc):
        return False
    if is_payment_required(exc):
        return False
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code in RATE_LIMIT_CODES:
        return True
    msg = str(exc).lower()
    # "quota" alone is too broad — 402 errors also say param:quota.
    # Only treat as rate-limit when combined with typical rate-limit wording.
    return any(k in msg for k in ("rate limit", "too many", "exhausted", "1113")) or (
        "quota" in msg and "payment" not in msg
    )


def is_context_overflow(exc: Exception) -> bool:
    """400 context_length_exceeded — messages need to be trimmed."""
    if isinstance(exc, APIStatusError) and exc.status_code == 400:
        msg = str(exc).lower()
        return (
            "context_length" in msg
            or "context length" in msg
            or "reduce the length" in msg
        )
    return False


def trim_messages(messages: list) -> list:
    """
    Trim the message list to fit inside gpt-oss-120b's 128k context window.
    Always keeps the system message and the most recent messages.
    Tool results are the main space consumers, so older ones are dropped first.
    """
    ctx_tokens = MODEL_CONTEXT_WINDOW
    budget_chars = (ctx_tokens - OUTPUT_RESERVE) * CHARS_PER_TOKEN

    if not messages:
        return messages

    system_msg = messages[0] if messages[0]["role"] == "system" else None
    non_system = messages[1:] if system_msg else messages[:]
    sys_chars = len(str((system_msg or {}).get("content", "")))
    remaining = budget_chars - sys_chars - 512  # safety margin

    # Walk backwards — keep most recent messages that fit
    kept = []
    used_chars = 0
    for msg in reversed(non_system):
        parts = [str(msg.get("content") or "")]
        if "tool_calls" in msg:
            parts.append(json.dumps(msg["tool_calls"]))
        msg_chars = sum(len(p) for p in parts)
        if used_chars + msg_chars > remaining and kept:
            break
        kept.append(msg)
        used_chars += msg_chars

    kept.reverse()
    dropped = len(non_system) - len(kept)
    result = ([system_msg] if system_msg else []) + kept
    if dropped:
        print(
            col(
                YELLOW,
                f"  !! Context trim: dropped {dropped} older message(s) "
                f"to fit {ACTIVE_MODEL}'s {ctx_tokens:,}-token window",
            )
        )
    return result


def find_in_json(obj, key: str):
    """Recursively find a key anywhere in a nested JSON object."""
    if isinstance(obj, dict):
        if key in obj and obj[key]:
            return obj[key]
        for v in obj.values():
            found = find_in_json(v, key)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_in_json(item, key)
            if found:
                return found
    return None


def _slugify_name(name: str) -> str:
    """Convert a project/space name to a safe filename component."""
    import re
    name = (name or "report").strip()
    name = re.sub(r"[^\w\s-]", "", name)   # strip special chars
    name = re.sub(r"[\s]+", "_", name)      # spaces → underscores
    return name[:40].strip("_")             # cap length


def save_report(
    content: str,
    stats: SessionStats,
    entity_name: str = "",
    period: str = "",
) -> str:
    """
    Save report as .md to D:/AI_Reports/.
    Filename: <entity_name>_<period>_<timestamp>.md
    Directory is created automatically if it does not exist.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    slug = _slugify_name(entity_name)
    period_slug = (period or "unknown").replace(" ", "_")
    filename = f"{slug}_{period_slug}_{timestamp}.md"
    report_file = REPORTS_DIR / filename
    report_file.write_text(content, encoding="utf-8")
    stats.reports_saved += 1
    return str(report_file)


# ── CLIENT ────────────────────────────────────────────────────────────────────


class CerebrasMCPClient:
    def __init__(self):
        if not CEREBRAS_API_KEY:
            print(f"\n{col(RED, 'ERROR')}  CEREBRAS_API_KEY not set in .env")
            print(f"  Get your free key at: {col(CYAN, 'https://cloud.cerebras.ai/')}")
            sys.exit(1)

        self.llm = AsyncOpenAI(
            api_key=CEREBRAS_API_KEY,
            base_url=CEREBRAS_BASE_URL,
        )
        self.system_prompt = load_system_prompt()
        self.mcp_session = None
        self._all_tools: list = []      # every MCP tool (minus EXCLUDED_TOOLS)
        self._raw_tools_by_name: dict = {}  # name → MCP tool object
        self.conversation = []
        self._sse_cm = None
        self._session_cm = None
        self.stats = SessionStats()

    # ── Model (single) ───────────────────────────────────────────────────────

    @property
    def active_model(self) -> str:
        return ACTIVE_MODEL

    # ── MCP ───────────────────────────────────────────────────────────────────

    async def connect_mcp(self):
        print(f"  {col(GREEN, 'OK')} MCP Server        : {col(DIM, MCP_SERVER_URL)}")
        self._sse_cm = sse_client(MCP_SERVER_URL)
        streams = await self._sse_cm.__aenter__()
        self._session_cm = ClientSession(*streams)
        self.mcp_session = await self._session_cm.__aenter__()
        await self.mcp_session.initialize()
        result = await self.mcp_session.list_tools()
        self._all_tools = result.tools
        self._raw_tools_by_name = {t.name: t for t in result.tools}
        n = len(mcp_tools_to_openai(result.tools))  # count minus excluded
        print(
            f"  {col(GREEN, 'OK')} MCP tools loaded  : {col(WHITE, str(n))} "
            f"{col(DIM, '(intent-filtered per request)')}"
        )

    async def disconnect_mcp(self):
        if self._session_cm:
            await self._session_cm.__aexit__(None, None, None)
        if self._sse_cm:
            await self._sse_cm.__aexit__(None, None, None)

    def _required_params_for(self, tool_name: str) -> list:
        """Return the list of required parameter names for a tool (from its MCP schema)."""
        t = self._raw_tools_by_name.get(tool_name)
        if not t or not t.inputSchema:
            return []
        schema = t.inputSchema
        props = schema.get("properties", {})
        return schema.get("required", list(props.keys()))

    # ── Tool call ─────────────────────────────────────────────────────────────

    async def call_mcp_tool(self, name: str, args: dict) -> str:
        # ── Client-side tool redirect ─────────────────────────────────────
        # If the model calls a known-bad tool with report-style args,
        # silently rewrite to the correct tool BEFORE hitting MCP server.
        if name in TOOL_REDIRECT:
            redir = TOOL_REDIRECT[name]
            if set(args.keys()) & redir["trigger_args"]:
                new_name = redir["redirect_to"]
                new_args = {k: v for k, v in args.items() if k in redir["keep_args"]}
                print(col(YELLOW,
                    f"  !! REDIRECT: {name}({list(args.keys())}) "
                    f"-> {new_name}({list(new_args.keys())})"
                ))
                name = new_name
                args = new_args
        self.stats.record_tool()
        try:
            result = await self.mcp_session.call_tool(name, args)
            parts = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
                elif hasattr(block, "data"):
                    parts.append(str(block.data))
            raw = "\n".join(parts) if parts else ""
            # MCP signals tool-level errors via result.isError
            if getattr(result, "isError", False):
                return json.dumps({"tool_call_failed": True, "error": raw})
            return raw or json.dumps({"status": "done"})
        except Exception as exc:
            return json.dumps({"tool_call_failed": True, "error": str(exc)})

    # ── LLM call ─────────────────────────────────────────────────────────────

    def _tools_for_intent(self, intent: str = "default") -> list:
        """
        Return the minimal tool set for the given intent.
        Falls back to all tools (minus EXCLUDED_TOOLS) for unknown intents.
        """
        allowed = INTENT_TOOL_SETS.get(intent)   # None = all tools
        return mcp_tools_to_openai(self._all_tools, allowed=allowed)

    def _detect_intent(self, user_message: str) -> str:
        """
        Lightweight keyword-based intent classifier.
        Returns one of the INTENT_TOOL_SETS keys.
        """
        msg = user_message.lower()
        if any(k in msg for k in ("space", "monitored", "aix")):
            return "space_report"
        if any(k in msg for k in ("project", "folder")):
            return "project_report"
        if any(k in msg for k in ("member", "employee", "who", "person")):
            return "member_report"
        if "low hours" in msg or "low_hours" in msg:
            return "low_hours"
        if "overtrack" in msg:
            return "overtracked"
        if "missing" in msg and "estim" in msg:
            return "missing_est"
        return "default"

    def _estimate_tokens(self, messages: list, tools: list) -> int:
        """Rough token estimate (chars / 4) for messages + tool schemas."""
        msg_chars = sum(
            len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls", [])))
            for m in messages
        )
        tool_chars = sum(len(str(t)) for t in tools)
        return (msg_chars + tool_chars) // CHARS_PER_TOKEN

    async def llm_call(self, messages: list, tools: Optional[list] = None):
        """Single-model LLM call with proactive context trimming."""
        current = list(messages)
        if tools is None:
            tools = mcp_tools_to_openai(self._all_tools)  # full minus excluded

        # ── Proactive pre-trim (avoids oversized requests) ──────────────────
        est = self._estimate_tokens(current, tools)
        if est > MODEL_CONTEXT_WINDOW - OUTPUT_RESERVE:
            print(
                col(
                    YELLOW,
                    f"  !! Pre-trim: ~{est:,} est. tokens > "
                    f"{MODEL_CONTEXT_WINDOW - OUTPUT_RESERVE:,} budget. "
                    "Trimming history...",
                )
            )
            current = trim_messages(current)
            est2 = self._estimate_tokens(current, tools)
            if est2 > MODEL_CONTEXT_WINDOW - OUTPUT_RESERVE:
                raise RuntimeError(
                    "Context too large even after trimming. "
                    "Try a shorter query or start a new session."
                )

        try:
            resp = await self.llm.chat.completions.create(
                model=ACTIVE_MODEL,
                messages=current,
                tools=tools or None,
                tool_choice="auto",
                temperature=0.2,
                max_tokens=8192,
            )
            if resp.usage:
                u = resp.usage
                self.stats.record(ACTIVE_MODEL, u.prompt_tokens, u.completion_tokens)
                print(
                    col(
                        DIM,
                        f"  >> in:{u.prompt_tokens:,}  out:{u.completion_tokens:,}"
                        f"  total:{u.total_tokens:,}  [{ACTIVE_MODEL}]",
                    )
                )
            return resp

        except Exception as exc:
            if is_model_not_found(exc):
                raise RuntimeError(
                    f"Model '{ACTIVE_MODEL}' not found on Cerebras (404). "
                    "Check ACTIVE_MODEL in cerebras_client.py."
                ) from exc
            if is_payment_required(exc):
                raise RuntimeError(
                    "Cerebras account requires payment (402). "
                    "Add credits at: https://cloud.cerebras.ai/billing"
                ) from exc
            if is_context_overflow(exc):
                raise RuntimeError(
                    "Context length exceeded. Try a shorter query or start a new session."
                ) from exc
            raise

    # ── Smart poll ────────────────────────────────────────────────────────────

    async def smart_poll_job(
        self,
        job_id: str,
        messages: list,
        report_ref: Optional[ReportTiming] = None,
    ) -> Optional[str]:
        """
        Two-phase smart polling:

        Phase 1 — Status watch (cheap)
            Calls get_task_report_job_status every STATUS_CHECK_INTERVAL_S seconds.
            Loops until status == "finished" OR timeout.

        Phase 2 — Result fetch (once, immediately on ready)
            Calls get_task_report_job_result exactly once.
            Extracts formatted_output and returns it.

        report_ref: ReportTiming object — if provided, .finish() is called
                    the instant the result is received so the log is accurate.
        """
        short_id = job_id[:20] + "..."
        started_at = time.time()

        print(f"\n  {col(CYAN, '>>')} Job queued {col(DIM, f'({short_id})')}")
        print(
            f"  {col(DIM, f'   Watching completion every {STATUS_CHECK_INTERVAL_S}s...')}"
        )

        elapsed_s = 0
        check_num = 0
        last_status = ""

        # ── Phase 1: status watch ─────────────────────────────────────────────
        while elapsed_s < STATUS_CHECK_TIMEOUT_S:
            await asyncio.sleep(STATUS_CHECK_INTERVAL_S)
            elapsed_s = int(time.time() - started_at)
            check_num += 1

            raw_status = await self.call_mcp_tool(
                "get_task_report_job_status", {"job_id": job_id}
            )

            try:
                parsed = json.loads(raw_status)
                status = find_in_json(parsed, "status") or "unknown"
            except json.JSONDecodeError:
                status = "unknown"

            if status != last_status:
                print(
                    f"\r  {col(CYAN, '>>')} [{check_num}] status: "
                    f"{col(YELLOW, status)}  {col(DIM, f'({elapsed_s}s)')}   ",
                    flush=True,
                )
                last_status = status
            else:
                print(
                    f"\r  {col(DIM, f'   [{check_num}] {status} ... {elapsed_s}s elapsed   ')}",
                    end="",
                    flush=True,
                )

            if status == "finished":
                print()
                break

            if status in ("failed", "error"):
                err = find_in_json(parsed, "error") or "unknown"
                print(f"\n  {col(RED, '!!')} Job failed: {err}")
                if report_ref:
                    report_ref.finish()
                return None

        else:
            print(f"\n  {col(YELLOW, '!!')} Timeout after {STATUS_CHECK_TIMEOUT_S}s.")
            if report_ref:
                report_ref.finish()
            return None

        # ── Phase 2: fetch result once ────────────────────────────────────────
        fetch_elapsed = int(time.time() - started_at)
        print(
            f"  {col(GREEN, 'OK')} Backend finished! "
            f"{col(DIM, f'Fetching result... ({fetch_elapsed}s total wait)')} "
        )

        raw_result = await self.call_mcp_tool(
            "get_task_report_job_result", {"job_id": job_id}
        )

        # Mark timing immediately when result arrives
        if report_ref:
            report_ref.finish()
            total_dur = report_ref.fmt_duration()
            print(
                f"  {col(GREEN, 'OK')} Report timing     : "
                f"started {col(WHITE, report_ref.fmt_start())}  "
                f"→  ended {col(WHITE, report_ref.fmt_end())}  "
                f"({col(CYAN, total_dur)})"
            )

        # Determine entity/period for smart filename
        _entity = (
            messages[-1].get("_report_entity", "")
            if messages and isinstance(messages[-1], dict)
            else ""
        ) or ""
        _period = (
            messages[-1].get("_report_period", "")
            if messages and isinstance(messages[-1], dict)
            else ""
        ) or ""

        try:
            parsed = json.loads(raw_result)
            fo = find_in_json(parsed, "formatted_output")
            if fo:
                path = save_report(fo, self.stats, entity_name=_entity, period=_period)
                print(
                    f"  {col(GREEN, 'OK')} Result received. "
                    f"{col(DIM, f'Saved → {Path(path).name}')}\n"
                )
                return fo

        except json.JSONDecodeError:
            if len(raw_result) > 200:
                path = save_report(
                    raw_result, self.stats, entity_name=_entity, period=_period
                )
                print(
                    f"  {col(GREEN, 'OK')} Result received. "
                    f"{col(DIM, f'Saved → {Path(path).name}')}\n"
                )
                return raw_result

        print(f"  {col(YELLOW, '!!')} Result fetched but no formatted_output found.")
        return raw_result or None

    # ── Chat loop ─────────────────────────────────────────────────────────────

    async def chat(self, user_message: str) -> str:
        self.conversation.append({"role": "user", "content": user_message})
        messages = [
            {"role": "system", "content": self.system_prompt},
            *self.conversation,
        ]
        # Detect intent to inject only relevant tools
        intent = self._detect_intent(user_message)
        tools = self._tools_for_intent(intent)
        poll_count = 0
        tool_fail_counts: dict = {}  # tool_name → consecutive failure count
        turn_tool_calls = 0  # total tool calls this turn (loop guard)
        # Track latest entity/period for smart report filenames
        _report_entity = ""
        _report_period = ""

        while True:
            response = await self.llm_call(messages, tools=tools)
            msg = response.choices[0].message

            if not msg.tool_calls:
                text = msg.content or ""
                self.conversation.append({"role": "assistant", "content": text})
                return text

            # Hard cap: prevent runaway loops
            turn_tool_calls += len(msg.tool_calls)
            if turn_tool_calls > MAX_TURN_TOOL_CALLS:
                summary = f"Stopped after {turn_tool_calls} tool calls in one turn — possible loop detected."
                print(col(YELLOW, f"  !! {summary}"))
                self.conversation.append({"role": "assistant", "content": summary})
                return summary

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                }
            )

            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                preview = json.dumps(args, ensure_ascii=False)[:80]
                # Capture entity/period for smart report filename
                _report_entity = (
                    args.get("space_name")
                    or args.get("project_name")
                    or args.get("member_name")
                    or _report_entity
                )
                _report_period = args.get("period_type", _report_period)
                print(
                    f"\n  {col(YELLOW, '->>')} {col(MAGENTA, ACTIVE_MODEL)}  "
                    f"{col(CYAN, name)}"
                    f"{col(DIM, f'  ({preview}...)')}"
                )

                raw = await self.call_mcp_tool(name, args)

                # ── Detect tool failure and break retry loops ─────────────────
                is_failed = False
                try:
                    parsed_raw = json.loads(raw)
                    if parsed_raw.get("tool_call_failed"):
                        is_failed = True
                        tool_fail_counts[name] = tool_fail_counts.get(name, 0) + 1
                        err_text = str(parsed_raw.get("error", "unknown error"))
                        print(
                            col(
                                YELLOW,
                                f"  !! Tool '{name}' failed "
                                f"({tool_fail_counts[name]}x): {err_text[:120]}",
                            )
                        )
                        # Inject correction on FIRST failure
                        required = self._required_params_for(name)
                        # Build explicit redirect hint
                        redir_hint = ""
                        if name in TOOL_REDIRECT:
                            rt = TOOL_REDIRECT[name]
                            redir_hint = (
                                f" Use '{rt['redirect_to']}' instead "
                                f"(e.g. {rt['redirect_to']}(space_name=..., period_type=...))."
                            )
                        correction = (
                            f"STOP calling '{name}' — it failed "
                            f"{tool_fail_counts[name]} time(s). "
                            f"Its ONLY required parameter is: {required}. "
                            f"You sent wrong args: {list(args.keys())}."
                            f"{redir_hint}"
                        )
                        print(col(YELLOW, f"  !! Correction injected for '{name}'"))
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.id, "content": raw}
                        )
                        messages.append({"role": "user", "content": correction})
                        continue
                except (json.JSONDecodeError, AttributeError):
                    pass

                if not is_failed:
                    tool_fail_counts[name] = 0  # reset consecutive count on success

                # ── Detect job_id → smart poll ────────────────────────────────
                job_id = None
                try:
                    parsed = json.loads(raw)
                    job_id = parsed.get("job_id")
                except (json.JSONDecodeError, AttributeError):
                    pass

                if job_id:
                    # Start report timing immediately when job_id is received
                    report_label = (
                        args.get("project_name")
                        or args.get("space_name")
                        or args.get("member_name")
                        or name
                    )
                    report_ref = self.stats.start_report(
                        label=f"{report_label} [{args.get('period_type', '?')}]",
                        job_id=job_id,
                    )
                    print(
                        f"  {col(CYAN, '⏱')}  Report timer started "
                        f"{col(DIM, f'({report_ref.fmt_start()})')}  "
                        f"job: {col(DIM, job_id[:24])}"
                    )

                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": raw}
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"Job {job_id} queued. Client is handling polling. "
                                "Use get_task_report_job_result only when asked. "
                                "Render formatted_output verbatim when delivered."
                            ),
                        }
                    )

                    # Annotate messages so smart_poll_job can read entity/period
                    if messages:
                        messages[-1]["_report_entity"] = _report_entity
                        messages[-1]["_report_period"] = _report_period
                    formatted = await self.smart_poll_job(job_id, messages, report_ref)
                    if formatted:
                        self.conversation.append(
                            {"role": "assistant", "content": formatted}
                        )
                        return formatted
                    continue

                messages.append({"role": "tool", "tool_call_id": tc.id, "content": raw})

                if name == "get_task_report_job_result":
                    poll_count += 1
                    if poll_count >= MAX_POLL_RETRIES:
                        messages.append(
                            {
                                "role": "user",
                                "content": "STOP_POLLING:true — summarise what you have.",
                            }
                        )


# ── SHELL ─────────────────────────────────────────────────────────────────────


async def main():
    print()
    print(sep("="))
    print(col(BOLD + WHITE, "   ClickUp MCP Server  ──  Cerebras / gpt-oss-120b Client"))
    print(sep("-"))

    client = CerebrasMCPClient()
    await client.connect_mcp()

    print(f"  {col(GREEN, 'OK')} Cerebras endpoint : {col(DIM, CEREBRAS_BASE_URL)}")
    print(
        f"  {col(GREEN, 'OK')} Model             : {col(MAGENTA + BOLD, ACTIVE_MODEL)}"
    )
    print(
        f"  {col(GREEN, 'OK')} Smart polling     : "
        f"{col(DIM, f'status every {STATUS_CHECK_INTERVAL_S}s, result fetched instantly')}"
    )
    print(f"  {col(GREEN, 'OK')} Reports saved to  : {col(DIM, str(REPORTS_DIR))}")

    print(sep("-"))
    print(col(BOLD, "  Chat ready!  Type your message below."))
    print(
        col(
            DIM,
            "  Commands: 'quit' / 'exit' → end session  |  'reset' → model info",
        )
    )
    print(sep("="))
    print()

    try:
        while True:
            try:
                user_input = input(col(CYAN, "You › ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user_input:
                continue

            if user_input.lower() in {"quit", "exit", "q"}:
                break

            if user_input.lower() == "reset":
                print(f"  {col(GREEN, 'OK')} Model is fixed: {col(MAGENTA, ACTIVE_MODEL)} (no fallback chain)\n")
                continue

            try:
                reply = await client.chat(user_input)
                print()
                print(reply)
                print()

            except Exception as exc:
                print(f"\n  {col(RED, 'ERROR')}  {exc}\n")

    finally:
        # ── Disconnect MCP ─────────────────────────────────────────────────────
        try:
            await client.disconnect_mcp()
        except Exception:
            pass

        # ── Print session summary (always, even on Ctrl+C) ────────────────────
        client.stats.print_summary()


if __name__ == "__main__":
    asyncio.run(main())