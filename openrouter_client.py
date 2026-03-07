"""
ClickUp MCP Server - OpenRouter Client

OpenRouter-backed client with smart MCP job polling.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.sse import sse_client
from openai import APIStatusError, AsyncOpenAI, RateLimitError

load_dotenv()

if sys.platform == "win32":
    os.system("color")

# ANSI colors
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


def col(color, text):
    return f"{color}{text}{RESET}"


def sep(char="-", width=72):
    return col(DIM, char * width)


OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "").strip()
OPENROUTER_MODEL_CHAIN = os.getenv("OPENROUTER_MODEL_CHAIN", "").strip()
OPENROUTER_HTTP_REFERER = os.getenv("OPENROUTER_HTTP_REFERER", "").strip()
OPENROUTER_APP_TITLE = os.getenv("OPENROUTER_APP_TITLE", "ClickUp MCP").strip()

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/sse")
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", r"D:\reports"))

ROOT_DIR = Path(__file__).parent
OPENROUTER_PROMPT_FILE = ROOT_DIR / "openrouter_system_prompt.md"
FALLBACK_PROMPT_FILE = ROOT_DIR / "lm_studio_system_prompt.md"

RATE_LIMIT_CODES = {402, 429, 503}
MAX_POLL_RETRIES = 5
STATUS_CHECK_INTERVAL_S = 3
STATUS_CHECK_TIMEOUT_S = 300

CORE_PM_TOOLS: set[str] = {
    "find_project_anywhere",
    "discover_hierarchy",
    "get_environment_context",
    "get_space_task_report",
    "get_project_task_report",
    "get_member_task_report",
    "get_low_hours_report",
    "get_missing_estimation_report",
    "get_overtracked_report",
    "get_task_report_job_status",
    "get_task_report_job_result",
    "get_project_report_universal",
    "get_workspaces",
    "get_spaces",
    "get_team_members",
    "get_tasks",
    "search_tasks",
    "get_task",
}

_ENTITY_PARAMS = ("space_name", "project_name", "entity_name")


@dataclass
class SessionStats:
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_api_calls: int = 0
    tool_calls_made: int = 0
    reports_saved: int = 0
    models_used: dict = field(default_factory=dict)
    start_time: float = field(default_factory=time.time)

    def record(self, model, inp, out):
        self.total_input_tokens += inp
        self.total_output_tokens += out
        self.total_api_calls += 1
        self.models_used[model] = self.models_used.get(model, 0) + 1

    def record_tool(self):
        self.tool_calls_made += 1

    def elapsed(self):
        s = int(time.time() - self.start_time)
        return f"{s // 60}m {s % 60}s"

    def print_summary(self):
        total = self.total_input_tokens + self.total_output_tokens
        print()
        print(sep("="))
        print(col(BOLD + WHITE, "  Session Summary"))
        print(sep("-"))
        print(
            f"  {col(CYAN, 'API calls')}        : {col(WHITE, str(self.total_api_calls))}"
        )
        print(
            f"  {col(CYAN, 'MCP tool calls')}   : {col(WHITE, str(self.tool_calls_made))}"
        )
        print(
            f"  {col(CYAN, 'Input tokens')}     : {col(GREEN, f'{self.total_input_tokens:,}')}"
        )
        print(
            f"  {col(CYAN, 'Output tokens')}    : {col(YELLOW, f'{self.total_output_tokens:,}')}"
        )
        print(f"  {col(CYAN, 'Total tokens')}     : {col(WHITE, f'{total:,}')}")
        print(
            f"  {col(CYAN, 'Reports saved')}    : {col(GREEN, str(self.reports_saved))}"
        )
        print(f"  {col(CYAN, 'Duration')}         : {col(WHITE, self.elapsed())}")
        if self.models_used:
            parts = "  ".join(
                f"{col(MAGENTA, m)} x{n}" for m, n in self.models_used.items()
            )
            print(f"  {col(CYAN, 'Models used')}      :  {parts}")
        print(sep("="))


def _model_chain_from_env() -> list[str]:
    if OPENROUTER_MODEL_CHAIN:
        models = [m.strip() for m in OPENROUTER_MODEL_CHAIN.split(",") if m.strip()]
        if models:
            return models
    if OPENROUTER_MODEL:
        return [OPENROUTER_MODEL]
    return ["qwen/qwen-2.5-7b-instruct"]


def load_openrouter_system_prompt() -> str:
    if OPENROUTER_PROMPT_FILE.exists():
        text = OPENROUTER_PROMPT_FILE.read_text(encoding="utf-8").strip()
        print(
            f"  {col(GREEN, 'OK')} System prompt     : "
            f"{col(MAGENTA, OPENROUTER_PROMPT_FILE.name)} "
            f"{col(DIM, f'({len(text):,} chars)')}"
        )
        return text

    if FALLBACK_PROMPT_FILE.exists():
        text = FALLBACK_PROMPT_FILE.read_text(encoding="utf-8").strip()
        print(
            f"  {col(YELLOW, '!!')} System prompt     : "
            f"{col(DIM, FALLBACK_PROMPT_FILE.name)} "
            f"{col(DIM, f'(fallback, {len(text):,} chars)')}"
        )
        return text

    print(f"  {col(YELLOW, '!!')} No prompt file found - using minimal fallback.")
    return (
        "You are a ClickUp PM assistant. "
        "Call ONE tool per turn. "
        "Render formatted_output verbatim when present."
    )


def mcp_tools_to_openai(tools):
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
    ]


def is_rate_limit(exc):
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code in RATE_LIMIT_CODES:
        return True
    msg = str(exc).lower()
    return any(
        k in msg
        for k in (
            "rate limit",
            "quota",
            "too many",
            "exhausted",
            "insufficient",
            "credit",
            "payment required",
            "1113",
        )
    )


def is_mcp_connection_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        k in msg
        for k in (
            "all connection attempts failed",
            "connection closed",
            "connection reset",
            "connection aborted",
            "broken pipe",
            "server disconnected",
            "stream closed",
            "transport closed",
            "clientsession is closed",
            "nonetype",
            "remoteprotocolerror",
            "incomplete chunked read",
            "peer closed connection without sending complete message body",
            "read error",
            "readerror",
            "connection lost",
        )
    )


def find_in_json(obj, key):
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


def _name_variants(name: str) -> list[str]:
    variants: list[str] = []
    if "&" in name:
        v = re.sub(r"\s*&\s*", " and ", name)
        if v != name:
            variants.append(v)
    if re.search(r"\band\b", name, flags=re.IGNORECASE):
        v = re.sub(r"\s+\band\b\s+", " & ", name, flags=re.IGNORECASE)
        if v != name:
            variants.append(v)
    return list(dict.fromkeys(variants))


def _is_entity_not_found(raw: str) -> bool:
    try:
        d = json.loads(raw)
        err = (d.get("error") or "") if isinstance(d, dict) else ""
        return any(
            p in err.lower() for p in ("not found", "no lists found", "no monitored")
        )
    except Exception:
        return False


def _slugify(text: str, fallback: str = "na", max_len: int = 40) -> str:
    value = (text or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    if not value:
        value = fallback
    return value[:max_len].strip("-") or fallback


def _extract_report_name_parts(
    content: str, query_text: str = ""
) -> tuple[str, str, str]:
    kind = "generic"
    entity = _slugify(query_text, fallback="query", max_len=44)
    period = "period-na"

    header_match = re.search(
        r"^\s*##\s*([^:\n]+?)\s*:\s*(.+?)\s*$", content or "", flags=re.MULTILINE
    )
    if header_match:
        title = header_match.group(1).strip().lower()
        entity = _slugify(header_match.group(2), fallback="entity", max_len=44)
        if "space report" in title:
            kind = "space"
        elif "project report" in title:
            kind = "project"
        elif "member report" in title:
            kind = "member"
        elif "low hours report" in title:
            kind = "low-hours"
        elif "missing estimation report" in title:
            kind = "missing-estimation"
        elif "overtracked report" in title:
            kind = "overtracked"
        else:
            kind = _slugify(title, fallback="report", max_len=24)

    period_match = re.search(
        r"^\s*\*\*Period:\*\*\s*(.+?)\s*$", content or "", flags=re.MULTILINE
    )
    if period_match:
        period = _slugify(period_match.group(1), fallback="period-na", max_len=36)

    return kind, entity, period


def save_report(content: str, stats: SessionStats, query_text: str = "") -> str:
    REPORTS_DIR.mkdir(exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    kind, entity, period = _extract_report_name_parts(content, query_text=query_text)
    base_name = f"report_{kind}_{entity}_{period}_{timestamp}"
    report_file = REPORTS_DIR / f"{base_name}.md"
    suffix = 1
    while report_file.exists():
        report_file = REPORTS_DIR / f"{base_name}_{suffix}.md"
        suffix += 1
    report_file.write_text(content, encoding="utf-8")
    stats.reports_saved += 1
    return str(report_file)


class OpenRouterMCPClient:
    def __init__(self):
        if not OPENROUTER_API_KEY:
            print(f"\n{col(RED, 'ERROR')}  OPENROUTER_API_KEY not set in .env")
            print(f"  Get your key at: {col(CYAN, 'https://openrouter.ai/keys')}")
            sys.exit(1)

        self.model_chain = _model_chain_from_env()
        self.provider_name = "openrouter"

        default_headers = {}
        if OPENROUTER_HTTP_REFERER:
            default_headers["HTTP-Referer"] = OPENROUTER_HTTP_REFERER
        if OPENROUTER_APP_TITLE:
            default_headers["X-Title"] = OPENROUTER_APP_TITLE

        self.llm = AsyncOpenAI(
            api_key=OPENROUTER_API_KEY,
            base_url=OPENROUTER_BASE_URL,
            default_headers=default_headers or None,
        )
        self.system_prompt = load_openrouter_system_prompt()
        self.mcp_session = None
        self.openai_tools = []
        self.conversation = []
        self._model_index = 0
        self._sse_cm = None
        self._session_cm = None
        self.stats = SessionStats()

    @property
    def active_model(self):
        return self.model_chain[self._model_index]

    @property
    def active_provider(self):
        return self.provider_name

    def rotate_model(self):
        if self._model_index + 1 >= len(self.model_chain):
            return False
        self._model_index += 1
        print(
            f"\n  {col(YELLOW, '!!')} Rate limit -> switching to {col(MAGENTA, self.active_model)}"
        )
        return True

    def reset_model(self):
        self._model_index = 0
        print(
            f"  {col(GREEN, 'OK')} Reset to primary: {col(MAGENTA, self.active_model)}\n"
        )

    async def connect_mcp(self):
        print(f"  {col(GREEN, 'OK')} MCP Server        : {col(DIM, MCP_SERVER_URL)}")
        self._sse_cm = sse_client(MCP_SERVER_URL)
        streams = await self._sse_cm.__aenter__()
        self._session_cm = ClientSession(*streams)
        self.mcp_session = await self._session_cm.__aenter__()
        await self.mcp_session.initialize()
        result = await self.mcp_session.list_tools()
        self.openai_tools = mcp_tools_to_openai(result.tools)

        if CORE_PM_TOOLS:
            before = len(self.openai_tools)
            self.openai_tools = [
                t for t in self.openai_tools if t["function"]["name"] in CORE_PM_TOOLS
            ]
            after = len(self.openai_tools)
            print(
                f"  {col(GREEN, 'OK')} Tool filter       : "
                f"{col(DIM, f'{after} tools visible (filtered {before - after} of {before})')}"
            )

    async def disconnect_mcp(self):
        session_cm = self._session_cm
        sse_cm = self._sse_cm
        self._session_cm = None
        self._sse_cm = None
        self.mcp_session = None

        try:
            if session_cm:
                await session_cm.__aexit__(None, None, None)
        finally:
            if sse_cm:
                await sse_cm.__aexit__(None, None, None)

    def _tool_result_to_text(self, result):
        parts = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            elif hasattr(block, "data"):
                parts.append(str(block.data))
        return "\n".join(parts) if parts else json.dumps({"status": "done"})

    async def call_mcp_tool(self, name, args):
        self.stats.record_tool()
        try:
            result = await self.mcp_session.call_tool(name, args)
            raw = self._tool_result_to_text(result)

            if _is_entity_not_found(raw):
                for param in _ENTITY_PARAMS:
                    original = args.get(param)
                    if not original:
                        continue
                    for variant in _name_variants(original):
                        retry_args = {**args, param: variant}
                        retry_result = await self.mcp_session.call_tool(
                            name, retry_args
                        )
                        retry_raw = self._tool_result_to_text(retry_result)
                        if not _is_entity_not_found(retry_raw):
                            print(
                                f"\n  {col(YELLOW, '!!')} Name auto-corrected: "
                                f"{col(DIM, repr(original))} -> {col(GREEN, repr(variant))}"
                            )
                            return retry_raw

            return raw

        except Exception as exc:
            if is_mcp_connection_error(exc):
                print(f"  {col(YELLOW, '!!')} MCP disconnected. Reconnecting...")
                reconnect_attempts = 3
                for attempt in range(reconnect_attempts):
                    try:
                        await self.disconnect_mcp()
                    except Exception:
                        pass
                    try:
                        await self.connect_mcp()
                        result = await self.mcp_session.call_tool(name, args)
                        return self._tool_result_to_text(result)
                    except Exception as reconnect_exc:
                        if attempt < reconnect_attempts - 1:
                            await asyncio.sleep(attempt + 1)
                            continue
                        return json.dumps(
                            {"error": f"MCP reconnect failed: {str(reconnect_exc)}"}
                        )
            return json.dumps({"error": str(exc)})

    async def llm_call(self, messages):
        while True:
            try:
                resp = await self.llm.chat.completions.create(
                    model=self.active_model,
                    messages=messages,
                    tools=self.openai_tools or None,
                    tool_choice="auto",
                    temperature=0.1,
                    max_tokens=4096,
                )
                if resp.usage:
                    usage = resp.usage
                    self.stats.record(
                        self.active_model, usage.prompt_tokens, usage.completion_tokens
                    )
                    print(
                        col(
                            DIM,
                            f"  >> in:{usage.prompt_tokens:,}  out:{usage.completion_tokens:,}"
                            f"  total:{usage.total_tokens:,}  [{self.active_model}]",
                        )
                    )
                return resp
            except Exception as exc:
                if not is_rate_limit(exc):
                    raise
                if self.rotate_model():
                    continue
                raise RuntimeError(
                    "OpenRouter quota/rate limit reached for all configured models."
                ) from exc

    async def smart_poll_job(self, job_id: str, messages: list, query_text: str = ""):
        short_id = job_id[:20] + "..."
        started_at = time.time()

        print(f"\n  {col(CYAN, '>>')} Job queued {col(DIM, f'({short_id})')}")
        print(
            f"  {col(DIM, f'   Watching for completion (checking every {STATUS_CHECK_INTERVAL_S}s)...')}"
        )

        elapsed_s = 0
        check_num = 0
        last_status = ""

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
                return None
        else:
            print(f"\n  {col(YELLOW, '!!')} Timeout after {STATUS_CHECK_TIMEOUT_S}s.")
            return None

        print(f"  {col(GREEN, 'OK')} Backend finished! Fetching result now...")

        raw_result = await self.call_mcp_tool(
            "get_task_report_job_result", {"job_id": job_id}
        )

        messages.append(
            {"role": "tool", "tool_call_id": "smart_poll_result", "content": raw_result}
        )

        try:
            parsed = json.loads(raw_result)
            fo = find_in_json(parsed, "formatted_output")
            if fo:
                path = save_report(fo, self.stats, query_text=query_text)
                total_wait = int(time.time() - started_at)
                print(
                    f"  {col(GREEN, 'OK')} Report ready in {total_wait}s! "
                    f"{col(DIM, f'Saved -> reports/{Path(path).name}')}\n"
                )
                return fo
        except json.JSONDecodeError:
            if len(raw_result) > 200:
                path = save_report(raw_result, self.stats, query_text=query_text)
                print(
                    f"  {col(GREEN, 'OK')} Result received. "
                    f"{col(DIM, f'Saved -> reports/{Path(path).name}')}\n"
                )
                return raw_result

        print(f"  {col(YELLOW, '!!')} Result fetched but no formatted_output found.")
        return raw_result or None

    async def chat(self, user_message):
        self.conversation.append({"role": "user", "content": user_message})
        messages = [
            {"role": "system", "content": self.system_prompt},
            *self.conversation,
        ]
        poll_count = 0

        while True:
            response = await self.llm_call(messages)
            msg = response.choices[0].message

            if not msg.tool_calls:
                text = msg.content or ""
                self.conversation.append({"role": "assistant", "content": text})
                return text

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
                print(
                    f"\n  {col(YELLOW, '->>')} {col(MAGENTA, self.active_model)}  "
                    f"{col(CYAN, name)}{col(DIM, f'  ({preview}...)')}"
                )

                raw = await self.call_mcp_tool(name, args)

                job_id = None
                try:
                    parsed = json.loads(raw)
                    job_id = parsed.get("job_id")
                except (json.JSONDecodeError, AttributeError):
                    pass

                if job_id:
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

                    formatted = await self.smart_poll_job(
                        job_id, messages, query_text=user_message
                    )
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
                                "content": "STOP_POLLING:true - summarise what you have.",
                            }
                        )


async def main():
    print()
    print(sep("="))
    print(col(BOLD + WHITE, "   ClickUp MCP Server  --  OpenRouter Client"))
    print(sep("-"))

    client = OpenRouterMCPClient()
    await client.connect_mcp()

    print(f"  {col(GREEN, 'OK')} MCP endpoint      : {col(DIM, MCP_SERVER_URL)}")
    print(f"  {col(GREEN, 'OK')} OpenRouter URL    : {col(DIM, OPENROUTER_BASE_URL)}")
    print(
        f"  {col(GREEN, 'OK')} Active model      : {col(MAGENTA + BOLD, client.active_model)}"
    )
    print(
        f"  {col(GREEN, 'OK')} Fallback model    : "
        f"{col(DIM, client.model_chain[1] if len(client.model_chain) > 1 else 'none')}"
    )
    print(f"  {col(GREEN, 'OK')} Reports saved to  : {col(DIM, str(REPORTS_DIR))}")

    print(sep("-"))
    print(col(BOLD, "  Chat Ready!"))
    print(col(DIM, "  Commands: quit | clear | model | reset | tools | stats"))
    print(sep("="))
    print()

    try:
        while True:
            try:
                user_input = input(
                    col(BOLD + BLUE, "You") + col(DIM, " > ") + WHITE
                ).strip()
                print(RESET, end="")
            except (EOFError, KeyboardInterrupt):
                break

            if not user_input:
                continue

            cmd = user_input.lower()

            if cmd in {"quit", "exit", "q"}:
                break
            if cmd == "clear":
                client.conversation = []
                print(f"  {col(GREEN, 'OK')} Conversation cleared.\n")
                continue
            if cmd == "model":
                idx = client._model_index
                remaining = client.model_chain[idx + 1 :]
                print(
                    f"\n  Active  : {col(MAGENTA + BOLD, client.active_model)} "
                    f"{col(DIM, f'(#{idx + 1}/{len(client.model_chain)})')}"
                )
                if remaining:
                    print(f"  Remaining: {col(DIM, ', '.join(remaining))}")
                print()
                continue
            if cmd == "reset":
                client.reset_model()
                continue
            if cmd == "stats":
                client.stats.print_summary()
                print()
                continue
            if cmd == "tools":
                print(f"\n  {len(client.openai_tools)} MCP tools:")
                for tool in client.openai_tools:
                    print(f"    {col(DIM, '*')} {tool['function']['name']}")
                print()
                continue

            print()
            started = asyncio.get_running_loop().time()
            try:
                reply = await client.chat(user_input)
                elapsed = asyncio.get_running_loop().time() - started
                print()
                print(sep("-"))
                print(
                    col(BOLD + GREEN, f"  [{client.active_model}]")
                    + col(DIM, f"  ({elapsed:.1f}s)\n")
                )
                print(reply)
                print()
                print(sep("-"))
                print()
            except RuntimeError as exc:
                print(f"\n  {col(RED, 'ERROR')}  {exc}\n")
            except Exception as exc:
                print(f"\n  {col(RED, 'ERROR')}  {exc}\n")
    finally:
        print(col(DIM, "\n  Disconnecting..."))
        await client.disconnect_mcp()
        client.stats.print_summary()
        print(col(DIM, "\n  Goodbye!\n"))


if __name__ == "__main__":
    asyncio.run(main())
