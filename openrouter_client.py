"""
ClickUp MCP Server - OpenRouter Client

OpenRouter-backed client that reuses the existing MCP + tool workflow.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

from zai_client import (
    BLUE,
    BOLD,
    CYAN,
    DIM,
    GREEN,
    MAGENTA,
    RED,
    WHITE,
    YELLOW,
    SessionStats,
    ZaiMCPClient,
    col,
    is_rate_limit,
    sep,
)

load_dotenv()


OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "").strip()
OPENROUTER_MODEL_CHAIN = os.getenv("OPENROUTER_MODEL_CHAIN", "").strip()
OPENROUTER_HTTP_REFERER = os.getenv("OPENROUTER_HTTP_REFERER", "").strip()
OPENROUTER_APP_TITLE = os.getenv("OPENROUTER_APP_TITLE", "ClickUp MCP").strip()
OPENROUTER_ENABLE_ZAI_FALLBACK = os.getenv(
    "OPENROUTER_ENABLE_ZAI_FALLBACK", "true"
).strip().lower() in {"1", "true", "yes", "on"}
OPENROUTER_ZAI_FALLBACK_MODELS = os.getenv(
    "OPENROUTER_ZAI_FALLBACK_MODELS", "glm-4.7-flash,glm-4.5-flash"
).strip()

ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")
ZAI_BASE_URL = os.getenv("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4/")

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/sse")
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", r"D:\reports"))

ROOT_DIR = Path(__file__).parent
OPENROUTER_PROMPT_FILE = ROOT_DIR / "openrouter_system_prompt.md"
FALLBACK_PROMPT_FILES = (
    ROOT_DIR / "zai_system_prompt.md",
    ROOT_DIR / "lm_studio_system_prompt.md",
)

# ---------------------------------------------------------------------------
# Tool allowlist — only these tools are exposed to the OpenRouter model.
# Keeps the tool-schema portion of the prompt small (~15 tools vs 60+).
# Add names here if you need to expose additional MCP tools.
# ---------------------------------------------------------------------------
CORE_PM_TOOLS: set[str] = {
    # Entity resolution & discovery
    "find_project_anywhere",
    "discover_hierarchy",
    "get_environment_context",
    # Task reports (primary reporting suite)
    "get_space_task_report",
    "get_project_task_report",
    "get_member_task_report",
    "get_low_hours_report",
    "get_missing_estimation_report",
    "get_overtracked_report",
    "get_task_report_job_status",
    "get_task_report_job_result",
    # Universal / analytics
    "get_project_report_universal",
    # Workspace basics
    "get_workspaces",
    "get_spaces",
    "get_team_members",
    # Task basics
    "get_tasks",
    "search_tasks",
    "get_task",
}

# ---------------------------------------------------------------------------
# Name-variant helpers — used for client-side auto-retry when entity not found
# ---------------------------------------------------------------------------
_ENTITY_PARAMS = ("space_name", "project_name", "entity_name")


def _name_variants(name: str) -> list[str]:
    """Generate alternate name forms for entity not-found auto-retry.
    Handles & <-> and substitution (the most common mismatch).
    """
    variants: list[str] = []
    if "&" in name:
        # "DevOps & Networking" → "DevOps and Networking"
        v = re.sub(r"\s*&\s*", " and ", name)
        if v != name:
            variants.append(v)
    if re.search(r"\band\b", name, flags=re.IGNORECASE):
        # "DevOps and Networking" → "DevOps & Networking"
        v = re.sub(r"\s+\band\b\s+", " & ", name, flags=re.IGNORECASE)
        if v != name:
            variants.append(v)
    return list(dict.fromkeys(variants))  # deduplicated, order preserved


def _is_entity_not_found(raw: str) -> bool:
    """Return True when a tool response is an entity-not-found error."""
    try:
        d = json.loads(raw)
        err = (d.get("error") or "") if isinstance(d, dict) else ""
        return any(
            p in err.lower() for p in ("not found", "no lists found", "no monitored")
        )
    except Exception:
        return False


def _model_chain_from_env() -> list[str]:
    if OPENROUTER_MODEL_CHAIN:
        models = [m.strip() for m in OPENROUTER_MODEL_CHAIN.split(",") if m.strip()]
        if models:
            return models
    if OPENROUTER_MODEL:
        return [OPENROUTER_MODEL]
    return ["qwen/qwen-2.5-7b-instruct"]


def _zai_fallback_chain_from_env() -> list[str]:
    models = [m.strip() for m in OPENROUTER_ZAI_FALLBACK_MODELS.split(",") if m.strip()]
    return models or ["glm-4.7-flash", "glm-4.5-flash"]


def load_openrouter_system_prompt() -> str:
    if OPENROUTER_PROMPT_FILE.exists():
        text = OPENROUTER_PROMPT_FILE.read_text(encoding="utf-8").strip()
        print(
            f"  {col(GREEN, 'OK')} System prompt     : "
            f"{col(MAGENTA, OPENROUTER_PROMPT_FILE.name)} "
            f"{col(DIM, f'({len(text):,} chars)')}"
        )
        return text

    for fallback in FALLBACK_PROMPT_FILES:
        if fallback.exists():
            text = fallback.read_text(encoding="utf-8").strip()
            print(
                f"  {col(YELLOW, '!!')} System prompt     : "
                f"{col(DIM, fallback.name)} "
                f"{col(DIM, f'(fallback, {len(text):,} chars)')}"
            )
            return text

    print(f"  {col(YELLOW, '!!')} No prompt file found - using minimal fallback.")
    return (
        "You are a ClickUp PM assistant. "
        "Call ONE tool per turn. "
        "Render formatted_output verbatim when present."
    )


class OpenRouterMCPClient(ZaiMCPClient):
    def __init__(self):
        if not OPENROUTER_API_KEY:
            print(f"\n{col(RED, 'ERROR')}  OPENROUTER_API_KEY not set in .env")
            print(f"  Get your key at: {col(CYAN, 'https://openrouter.ai/keys')}")
            sys.exit(1)

        self.model_chain = _model_chain_from_env()
        self.zai_fallback_chain = _zai_fallback_chain_from_env()
        self.provider_name = "openrouter"
        self._zai_fallback_active = False
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
            f"\n  {col(YELLOW, '!!')} Rate limit -> switching to "
            f"{col(MAGENTA, self.active_model)}"
        )
        return True

    def reset_model(self):
        self._model_index = 0
        print(
            f"  {col(GREEN, 'OK')} Reset to primary: {col(MAGENTA, self.active_model)}\n"
        )

    # ── MCP (override: filter tools to CORE_PM_TOOLS allowlist) ──────────────

    async def connect_mcp(self):
        await super().connect_mcp()
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

    # ── Tool call (override: auto-retry on entity-not-found with name variants)

    async def call_mcp_tool(self, name, args):
        raw = await super().call_mcp_tool(name, args)
        if not _is_entity_not_found(raw):
            return raw
        # Try & <-> and name variants before returning the error to the model
        for param in _ENTITY_PARAMS:
            original = args.get(param)
            if not original:
                continue
            for variant in _name_variants(original):
                retry_args = {**args, param: variant}
                retry = await super().call_mcp_tool(name, retry_args)
                if not _is_entity_not_found(retry):
                    print(
                        f"\n  {col(YELLOW, '!!')} Name auto-corrected: "
                        f"{col(DIM, repr(original))} → {col(GREEN, repr(variant))}"
                    )
                    return retry
        return raw  # all variants failed — return original error to model

    def _activate_zai_fallback(self):
        if self._zai_fallback_active:
            return False
        if not OPENROUTER_ENABLE_ZAI_FALLBACK:
            return False
        if not ZAI_API_KEY:
            print(
                f"\n  {col(RED, 'ERROR')} OpenRouter exhausted and ZAI fallback unavailable "
                f"(missing ZAI_API_KEY)."
            )
            return False

        self.llm = AsyncOpenAI(api_key=ZAI_API_KEY, base_url=ZAI_BASE_URL)
        self.model_chain = list(self.zai_fallback_chain)
        self._model_index = 0
        self.provider_name = "zai-fallback"
        self._zai_fallback_active = True
        print(
            f"\n  {col(YELLOW, '!!')} OpenRouter quota/rate limit detected. "
            f"Switching temporarily to {col(MAGENTA, 'Z.AI fallback')} "
            f"{col(DIM, f'({self.active_model})')}."
        )
        return True

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
                if self._activate_zai_fallback():
                    continue
                raise RuntimeError(
                    "OpenRouter quota/rate limit reached and fallback is unavailable."
                ) from exc


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
    print(
        f"  {col(GREEN, 'OK')} Z.AI fallback     : "
        f"{col(DIM, 'enabled' if OPENROUTER_ENABLE_ZAI_FALLBACK else 'disabled')}"
    )

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
                print("\033[0m", end="")
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
