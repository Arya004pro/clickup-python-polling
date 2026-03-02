"""
ClickUp MCP Server - Z.AI GLM Client

Smart polling: checks job status every 3s (lightweight).
The moment backend says 'finished', fetches result immediately.
Zero unnecessary waiting.

Free Z.AI models:
  1. glm-4.7-flash  (primary)
  2. glm-4.5-flash  (fallback)
"""

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.sse import sse_client
from openai import AsyncOpenAI, RateLimitError, APIStatusError

load_dotenv()

if sys.platform == "win32":
    os.system("color")

# ─────────────────────────────────────────────────────────────────────────────
# ANSI COLOURS
# ─────────────────────────────────────────────────────────────────────────────
RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
CYAN    = "\033[36m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
RED     = "\033[31m"
MAGENTA = "\033[35m"
BLUE    = "\033[34m"
WHITE   = "\033[97m"

def col(colour, text):
    return f"{colour}{text}{RESET}"

def sep(char="─", width=72):
    return col(DIM, char * width)


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
ZAI_API_KEY    = os.getenv("ZAI_API_KEY", "")
ZAI_BASE_URL   = os.getenv("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4/")
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/sse")

ROOT_DIR             = Path(__file__).parent
ZAI_PROMPT_FILE      = ROOT_DIR / "zai_system_prompt.md"
FALLBACK_PROMPT_FILE = ROOT_DIR / "lm_studio_system_prompt.md"
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", r"D:\reports"))

MODEL_CHAIN = [
    "glm-4.7-flash",
    "glm-4.5-flash",
]

RATE_LIMIT_CODES = {429, 503}
MAX_POLL_RETRIES = 5

# ── SMART POLLING ────────────────────────────────────────────────────────────
#
#  Old approach (dumb):   wait 60s → fetch result → wait 60s → fetch result
#  New approach (smart):  check status every 3s (cheap) → the INSTANT backend
#                         says "finished" → fetch full result immediately
#
#  Result: report appears within 3s of backend finishing, not up to 60s late.
#
STATUS_CHECK_INTERVAL_S = 3    # how often to ping get_task_report_job_status
STATUS_CHECK_TIMEOUT_S  = 300  # give up after 5 min (handles stuck jobs)


# ─────────────────────────────────────────────────────────────────────────────
# SESSION STATS
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class SessionStats:
    total_input_tokens:  int   = 0
    total_output_tokens: int   = 0
    total_api_calls:     int   = 0
    tool_calls_made:     int   = 0
    reports_saved:       int   = 0
    models_used:         dict  = field(default_factory=dict)
    start_time:          float = field(default_factory=time.time)

    def record(self, model, inp, out):
        self.total_input_tokens  += inp
        self.total_output_tokens += out
        self.total_api_calls     += 1
        self.models_used[model]   = self.models_used.get(model, 0) + 1

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
        print(f"  {col(CYAN,'API calls')}        : {col(WHITE, str(self.total_api_calls))}")
        print(f"  {col(CYAN,'MCP tool calls')}   : {col(WHITE, str(self.tool_calls_made))}")
        print(f"  {col(CYAN,'Input tokens')}     : {col(GREEN, f'{self.total_input_tokens:,}')}")
        print(f"  {col(CYAN,'Output tokens')}    : {col(YELLOW, f'{self.total_output_tokens:,}')}")
        print(f"  {col(CYAN,'Total tokens')}     : {col(WHITE, f'{total:,}')}")
        print(f"  {col(CYAN,'Reports saved')}    : {col(GREEN, str(self.reports_saved))}")
        print(f"  {col(CYAN,'Duration')}         : {col(WHITE, self.elapsed())}")
        if self.models_used:
            parts = "  ".join(
                f"{col(MAGENTA, m)} x{n}" for m, n in self.models_used.items()
            )
            print(f"  {col(CYAN,'Models used')}      :  {parts}")
        print(sep("="))


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def load_system_prompt():
    if ZAI_PROMPT_FILE.exists():
        text = ZAI_PROMPT_FILE.read_text(encoding="utf-8").strip()
        print(f"  {col(GREEN,'OK')} System prompt     : "
              f"{col(MAGENTA, ZAI_PROMPT_FILE.name)} "
              f"{col(DIM, f'({len(text):,} chars)')}")
        return text
    if FALLBACK_PROMPT_FILE.exists():
        text = FALLBACK_PROMPT_FILE.read_text(encoding="utf-8").strip()
        print(f"  {col(YELLOW,'!!')} System prompt     : "
              f"{col(DIM, FALLBACK_PROMPT_FILE.name)} "
              f"{col(DIM, f'(fallback, {len(text):,} chars)')}")
        return text
    print(f"  {col(YELLOW,'!!')} No prompt file found - using minimal fallback.")
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
                "name":        t.name,
                "description": t.description or "",
                "parameters":  t.inputSchema or {"type": "object", "properties": {}},
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
    return any(k in msg for k in ("rate limit", "quota", "too many", "exhausted", "1113"))


def find_in_json(obj, key):
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


def save_report(content: str, stats: SessionStats) -> str:
    REPORTS_DIR.mkdir(exist_ok=True)
    timestamp   = time.strftime("%Y-%m-%d_%H-%M-%S")
    report_file = REPORTS_DIR / f"report_{timestamp}.md"
    report_file.write_text(content, encoding="utf-8")
    stats.reports_saved += 1
    return str(report_file)


# ─────────────────────────────────────────────────────────────────────────────
# CLIENT
# ─────────────────────────────────────────────────────────────────────────────

class ZaiMCPClient:

    def __init__(self):
        if not ZAI_API_KEY:
            print(f"\n{col(RED,'ERROR')}  ZAI_API_KEY not set in .env")
            print(f"  Get your free key at: {col(CYAN,'https://z.ai/model-api')}")
            sys.exit(1)

        self.llm           = AsyncOpenAI(api_key=ZAI_API_KEY, base_url=ZAI_BASE_URL)
        self.system_prompt = load_system_prompt()
        self.mcp_session   = None
        self.openai_tools  = []
        self.conversation  = []
        self._model_index  = 0
        self._sse_cm       = None
        self._session_cm   = None
        self.stats         = SessionStats()

    @property
    def active_model(self):
        return MODEL_CHAIN[self._model_index]

    def rotate_model(self):
        if self._model_index + 1 >= len(MODEL_CHAIN):
            return False
        self._model_index += 1
        print(f"\n  {col(YELLOW,'!!')} Rate limit -> switching to "
              f"{col(MAGENTA, self.active_model)}")
        return True

    def reset_model(self):
        self._model_index = 0
        print(f"  {col(GREEN,'OK')} Reset to primary: {col(MAGENTA, self.active_model)}\n")

    # ── MCP ───────────────────────────────────────────────────────────────────

    async def connect_mcp(self):
        print(f"  {col(GREEN,'OK')} MCP Server        : {col(DIM, MCP_SERVER_URL)}")
        self._sse_cm     = sse_client(MCP_SERVER_URL)
        streams          = await self._sse_cm.__aenter__()
        self._session_cm = ClientSession(*streams)
        self.mcp_session = await self._session_cm.__aenter__()
        await self.mcp_session.initialize()
        result            = await self.mcp_session.list_tools()
        self.openai_tools = mcp_tools_to_openai(result.tools)
        print(f"  {col(GREEN,'OK')} MCP tools loaded  : {col(WHITE, str(len(self.openai_tools)))}")

    async def disconnect_mcp(self):
        if self._session_cm:
            await self._session_cm.__aexit__(None, None, None)
        if self._sse_cm:
            await self._sse_cm.__aexit__(None, None, None)

    # ── TOOL CALL ─────────────────────────────────────────────────────────────

    async def call_mcp_tool(self, name, args):
        self.stats.record_tool()
        try:
            result = await self.mcp_session.call_tool(name, args)
            parts  = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
                elif hasattr(block, "data"):
                    parts.append(str(block.data))
            return "\n".join(parts) if parts else json.dumps({"status": "done"})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # ── LLM CALL ─────────────────────────────────────────────────────────────

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
                    u = resp.usage
                    self.stats.record(self.active_model, u.prompt_tokens, u.completion_tokens)
                    print(col(DIM,
                        f"  >> in:{u.prompt_tokens:,}  out:{u.completion_tokens:,}"
                        f"  total:{u.total_tokens:,}  [{self.active_model}]"
                    ))
                return resp
            except Exception as exc:
                if not is_rate_limit(exc):
                    raise
                if not self.rotate_model():
                    raise RuntimeError(
                        "All Z.AI models are rate-limited. Wait a few minutes and retry."
                    ) from exc

    # ── SMART POLL ────────────────────────────────────────────────────────────

    async def smart_poll_job(self, job_id: str, messages: list):
        """
        Two-phase smart polling:

        Phase 1 — Status watch (cheap, fast)
          Calls get_task_report_job_status every STATUS_CHECK_INTERVAL_S seconds.
          This only returns a small JSON like {"status": "running"} — no payload.
          Keeps looping until status == "finished" OR timeout.

        Phase 2 — Result fetch (called ONCE, immediately on ready signal)
          Calls get_task_report_job_result exactly once.
          Extracts formatted_output and returns it.

        Result: report appears within ~3s of backend finishing.
        No fixed waits, no unnecessary 60s gaps.
        """
        short_id   = job_id[:20] + "..."
        started_at = time.time()

        print(f"\n  {col(CYAN,'>>')} Job queued {col(DIM, f'({short_id})')}")
        print(f"  {col(DIM, f'   Watching for completion (checking every {STATUS_CHECK_INTERVAL_S}s)...')}")

        elapsed_s  = 0
        check_num  = 0
        last_status = ""

        # ── PHASE 1: watch status ─────────────────────────────────────────────
        while elapsed_s < STATUS_CHECK_TIMEOUT_S:
            await asyncio.sleep(STATUS_CHECK_INTERVAL_S)
            elapsed_s = int(time.time() - started_at)
            check_num += 1

            raw_status = await self.call_mcp_tool(
                "get_task_report_job_status", {"job_id": job_id}
            )

            try:
                parsed  = json.loads(raw_status)
                status  = find_in_json(parsed, "status") or "unknown"
            except json.JSONDecodeError:
                status = "unknown"

            # Only print when status changes (avoids spamming same line)
            status_display = f"running ({elapsed_s}s elapsed)"
            if status != last_status:
                print(f"\r  {col(CYAN,'>>')} [{check_num}] status: "
                      f"{col(YELLOW, status)}  {col(DIM, f'({elapsed_s}s)')}   ",
                      flush=True)
                last_status = status
            else:
                # Same status — just update the elapsed counter in place
                print(f"\r  {col(DIM, f'   [{check_num}] {status} ... {elapsed_s}s elapsed   ')}",
                      end="", flush=True)

            if status == "finished":
                print()  # newline after inline counter
                break

            if status in ("failed", "error"):
                err = find_in_json(parsed, "error") or "unknown"
                print(f"\n  {col(RED,'!!')} Job failed: {err}")
                return None

        else:
            print(f"\n  {col(YELLOW,'!!')} Timeout after {STATUS_CHECK_TIMEOUT_S}s.")
            return None

        # ── PHASE 2: fetch result ONCE, immediately ───────────────────────────
        print(f"  {col(GREEN,'OK')} Backend finished! Fetching result now...")

        raw_result = await self.call_mcp_tool(
            "get_task_report_job_result", {"job_id": job_id}
        )

        # Add to messages context
        messages.append({
            "role":         "tool",
            "tool_call_id": "smart_poll_result",
            "content":      raw_result,
        })

        try:
            parsed = json.loads(raw_result)
            fo     = find_in_json(parsed, "formatted_output")

            if fo:
                path = save_report(fo, self.stats)
                total_wait = int(time.time() - started_at)
                print(f"  {col(GREEN,'OK')} Report ready in {total_wait}s! "
                      f"{col(DIM, f'Saved -> reports/{Path(path).name}')}\n")
                return fo

        except json.JSONDecodeError:
            if len(raw_result) > 200:
                path = save_report(raw_result, self.stats)
                print(f"  {col(GREEN,'OK')} Result received. "
                      f"{col(DIM, f'Saved -> reports/{Path(path).name}')}\n")
                return raw_result

        print(f"  {col(YELLOW,'!!')} Result fetched but no formatted_output found.")
        return raw_result or None

    # ── CHAT LOOP ─────────────────────────────────────────────────────────────

    async def chat(self, user_message):
        self.conversation.append({"role": "user", "content": user_message})
        messages   = [
            {"role": "system", "content": self.system_prompt},
            *self.conversation,
        ]
        poll_count = 0

        while True:
            response = await self.llm_call(messages)
            msg      = response.choices[0].message

            if not msg.tool_calls:
                text = msg.content or ""
                self.conversation.append({"role": "assistant", "content": text})
                return text

            messages.append({
                "role":       "assistant",
                "content":    msg.content,
                "tool_calls": [
                    {
                        "id":       tc.id,
                        "type":     "function",
                        "function": {
                            "name":      tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            })

            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                preview = json.dumps(args, ensure_ascii=False)[:80]
                print(f"\n  {col(YELLOW,'->>')} {col(MAGENTA, self.active_model)}  "
                      f"{col(CYAN, name)}"
                      f"{col(DIM, f'  ({preview}...)')}")

                raw = await self.call_mcp_tool(name, args)

                # Detect job_id -> smart poll
                job_id = None
                try:
                    parsed = json.loads(raw)
                    job_id = parsed.get("job_id")
                except (json.JSONDecodeError, AttributeError):
                    pass

                if job_id:
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": raw})
                    messages.append({
                        "role":    "user",
                        "content": (
                            f"Job {job_id} queued. Client is handling polling. "
                            "Use get_task_report_job_result only when asked. "
                            "Render formatted_output verbatim when delivered."
                        ),
                    })

                    formatted = await self.smart_poll_job(job_id, messages)
                    if formatted:
                        self.conversation.append({"role": "assistant", "content": formatted})
                        return formatted
                    continue

                messages.append({"role": "tool", "tool_call_id": tc.id, "content": raw})

                if name == "get_task_report_job_result":
                    poll_count += 1
                    if poll_count >= MAX_POLL_RETRIES:
                        messages.append({
                            "role":    "user",
                            "content": "STOP_POLLING:true - summarise what you have.",
                        })


# ─────────────────────────────────────────────────────────────────────────────
# SHELL
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    print()
    print(sep("="))
    print(col(BOLD + WHITE, "   ClickUp MCP Server  --  Z.AI GLM Client"))
    print(sep("-"))

    client = ZaiMCPClient()
    await client.connect_mcp()

    print(f"  {col(GREEN,'OK')} Z.AI endpoint     : {col(DIM, ZAI_BASE_URL)}")
    print(f"  {col(GREEN,'OK')} Active model      : {col(MAGENTA + BOLD, client.active_model)}")
    print(f"  {col(GREEN,'OK')} Fallback model    : "
          f"{col(DIM, MODEL_CHAIN[1] if len(MODEL_CHAIN) > 1 else 'none')}")
    print(f"  {col(GREEN,'OK')} Smart polling     : "
          f"{col(DIM, f'status every {STATUS_CHECK_INTERVAL_S}s, result fetched instantly on ready')}")
    print(f"  {col(GREEN,'OK')} Reports saved to  : {col(DIM, str(REPORTS_DIR))}")

    print(sep("-"))
    print(col(BOLD, "  Chat Ready!"))
    print(col(DIM,  "  Commands: quit | clear | model | reset | tools | stats"))
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
            elif cmd == "clear":
                client.conversation = []
                print(f"  {col(GREEN,'OK')} Conversation cleared.\n")
            elif cmd == "model":
                idx = client._model_index
                remaining = MODEL_CHAIN[idx + 1:]
                print(f"\n  Active  : {col(MAGENTA + BOLD, client.active_model)} "
                      f"{col(DIM, f'(#{idx+1}/{len(MODEL_CHAIN)})')}")
                if remaining:
                    print(f"  Remaining: {col(DIM, ', '.join(remaining))}")
                print()
            elif cmd == "reset":
                client.reset_model()
            elif cmd == "stats":
                client.stats.print_summary()
                print()
            elif cmd == "tools":
                print(f"\n  {len(client.openai_tools)} MCP tools:")
                for t in client.openai_tools:
                    print(f"    {col(DIM,'*')} {t['function']['name']}")
                print()
            else:
                print()
                t0 = time.time()
                try:
                    reply   = await client.chat(user_input)
                    elapsed = time.time() - t0
                    print()
                    print(sep("-"))
                    print(col(BOLD + GREEN, f"  [{client.active_model}]") +
                          col(DIM, f"  ({elapsed:.1f}s)\n"))
                    print(reply)
                    print()
                    print(sep("-"))
                    print()
                except RuntimeError as exc:
                    print(f"\n  {col(RED,'ERROR')}  {exc}\n")
                except Exception as exc:
                    print(f"\n  {col(RED,'ERROR')}  {exc}\n")

    finally:
        print(col(DIM, "\n  Disconnecting..."))
        await client.disconnect_mcp()
        client.stats.print_summary()
        print(col(DIM, "\n  Goodbye!\n"))


if __name__ == "__main__":
    asyncio.run(main())