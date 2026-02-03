"""
Claude (Anthropic) MCP Client
Simple client modeled after `gemini_client.py` to provide a Claude-backed chat
interface with function calling support and token usage logging.

Usage:
- Add `CLAUDE_API_KEY` and `CLAUDE_MODEL` to your `.env` (this file will be updated)
- Run: `python claude_client.py`

This implementation keeps parity with the existing clients but is intentionally
minimal — it focuses on establishing a chat loop, testing MCP connectivity,
and extracting token usage information when available from response metadata.
"""

import asyncio
import json
import os
from typing import Dict, List, Any, Optional

from mcp import ClientSession
from mcp.client.sse import sse_client
from dotenv import load_dotenv

load_dotenv()

# Configuration
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-2.1")
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/sse")

# Detect Anthropic SDK availability at module import time
try:
    import anthropic  # type: ignore

    HAS_ANTHROPIC = True
except Exception:
    anthropic = None  # type: ignore
    HAS_ANTHROPIC = False


class ConversationLogger:
    def __init__(self):
        self.total_requests = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.tool_calls = 0

    def log_api_call(self, response: Any):
        self.total_requests += 1
        # Anthropic/Claude responses may include token counts under different keys.
        # Try common locations conservatively.
        try:
            if isinstance(response, dict):
                # Hypothetical Claude response shape: {
                #   "completion": {"tokens": {"input": ..., "output": ...}}
                # }
                toks = response.get("completion", {}).get("tokens", {})
                self.total_input_tokens += int(toks.get("input", 0) or 0)
                self.total_output_tokens += int(toks.get("output", 0) or 0)
            elif hasattr(response, "usage"):
                # OpenAI-compatible
                self.total_input_tokens += (
                    getattr(response.usage, "prompt_tokens", 0) or 0
                )
                self.total_output_tokens += (
                    getattr(response.usage, "completion_tokens", 0) or 0
                )
        except Exception:
            # Best-effort only
            pass

    def log_tool_call(self):
        self.tool_calls += 1

    def print_summary(self):
        total = self.total_input_tokens + self.total_output_tokens
        print("\n" + "=" * 60)
        print("CLAUDE CONVERSATION STATISTICS")
        print("=" * 60)
        print(f"   API Requests:      {self.total_requests}")
        print(f"   Tool Calls:        {self.tool_calls}")
        print(f"   Input Tokens:      {self.total_input_tokens:,}")
        print(f"   Output Tokens:     {self.total_output_tokens:,}")
        print(f"   Total Tokens Used: {total:,}")
        print("=" * 60 + "\n")


logger = ConversationLogger()


async def test_connection() -> bool:
    print("Testing MCP Server Connection...")
    try:
        async with sse_client(url=MCP_SERVER_URL) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                print(f"Connection successful! Found {len(tools.tools)} tools")
                return True
    except Exception as e:
        print(f"Connection failed: {e}")
        return False


async def run_chat_loop():
    # Minimal Claude client: for now we don't import Anthropic SDK directly
    # because local testing may not have the package; instead this function
    # shows the structure and how it would integrate with MCP and logging.

    # Build knowledge graph and session
    async with sse_client(url=MCP_SERVER_URL) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            print("\nStarting Claude-backed chat interface...\n")

            # Show configured model and attempt basic verification if SDK present
            if HAS_ANTHROPIC and CLAUDE_API_KEY:
                try:
                    client = anthropic.Client(api_key=CLAUDE_API_KEY)
                    # Try to verify model presence if API supports listing
                    available = None
                    if hasattr(client, "models") and hasattr(client.models, "list"):
                        try:
                            available = [
                                getattr(m, "id", None) or getattr(m, "name", None)
                                for m in client.models.list()
                            ]
                        except Exception:
                            available = None

                    if available and CLAUDE_MODEL in available:
                        print(f"Using Claude model: {CLAUDE_MODEL}")
                    else:
                        print(f"Configured Claude model: {CLAUDE_MODEL} (not verified)")
                except Exception:
                    print(f"Configured Claude model: {CLAUDE_MODEL}")
            else:
                print(f"Configured Claude model: {CLAUDE_MODEL}")
                if not HAS_ANTHROPIC:
                    print("(Anthropic SDK not installed; running in placeholder mode)")

            while True:
                user_input = input("\nYou: ")
                if user_input.lower() in ["quit", "exit", "q"]:
                    logger.print_summary()
                    print("Goodbye!")
                    break

                # Simple intent routing: for common "show" commands, call MCP tools
                cmd = user_input.strip().lower()
                try:
                    if (
                        "mapped project" in cmd
                        or "mapped projects" in cmd
                        or ("projects" in cmd and "mapped" in cmd)
                    ):
                        print("Thinking...")
                        res = await session.call_tool(
                            "list_mapped_projects", arguments={}
                        )
                        logger.log_tool_call()
                        data = None
                        try:
                            text = res.content[0].text if res and res.content else ""
                            data = json.loads(text)
                        except Exception:
                            data = text or {}

                        # Pretty-print results
                        if isinstance(data, list):
                            for p in data:
                                name = p.get("name") or p.get("alias") or str(p)
                                cid = p.get("clickup_id") or p.get("id") or ""
                                print(f" - {name} ({cid})")
                        else:
                            print(data)
                        # Log a synthetic response usage for accounting
                        logger.log_api_call(
                            {"completion": {"tokens": {"input": 5, "output": 5}}}
                        )
                        continue

                    if "space" in cmd and (
                        "show all" in cmd or "all spaces" in cmd or "spaces" == cmd
                    ):
                        print("Thinking...")
                        res = await session.call_tool("get_spaces", arguments={})
                        logger.log_tool_call()
                        try:
                            text = res.content[0].text if res and res.content else ""
                            data = json.loads(text)
                        except Exception:
                            data = text or {}

                        if isinstance(data, list):
                            for s in data:
                                print(f" - {s.get('name', s)}")
                        else:
                            print(data)
                        logger.log_api_call(
                            {"completion": {"tokens": {"input": 3, "output": 3}}}
                        )
                        continue

                    # FETCH TASKS / PROJECT TIME TRACKING
                    if (
                        "fetch tasks" in cmd
                        or "project time" in cmd
                        or "time tracked" in cmd
                    ):
                        # Attempt to extract a project name inside quotes or after 'from'
                        proj = None
                        import re

                        m = re.search(
                            r"from\s+'([^']+)'", user_input, flags=re.IGNORECASE
                        )
                        if not m:
                            m = re.search(
                                r'from\s+"([^"]+)"', user_input, flags=re.IGNORECASE
                            )
                        if m:
                            proj = m.group(1)
                        else:
                            # try after 'from ' until end or ' and '
                            m2 = re.search(
                                r"from\s+([^,]+)(?: and |$)",
                                user_input,
                                flags=re.IGNORECASE,
                            )
                            if m2:
                                proj = m2.group(1).strip().strip("'\"")

                        if not proj:
                            print(
                                "Could not parse project name. Use: Fetch tasks from 'Project Name'"
                            )
                            continue

                        print("Thinking...")
                        try:
                            res = await session.call_tool(
                                "get_project_time_tracking",
                                arguments={"project_name": proj},
                            )
                            logger.log_tool_call()
                            # res expected to be a dict with 'report'
                            try:
                                data = (
                                    res
                                    if isinstance(res, dict)
                                    else json.loads(res.content[0].text)
                                )
                            except Exception:
                                data = res

                            if isinstance(data, dict) and data.get("report"):
                                report = data["report"]
                                total_tracked = 0
                                total_est = 0
                                for k, v in report.items():
                                    tracked = v.get("tracked", 0)
                                    est = v.get("est", 0)
                                    total_tracked += tracked
                                    total_est += est
                                    print(
                                        f" - {k}: tracked={v.get('human_time', tracked)}, est={v.get('human_est', est)}, eff={v.get('eff')}"
                                    )
                                print(
                                    f"\nProject totals — Tracked: {total_tracked} sec, Estimated: {total_est} sec"
                                )
                            else:
                                print(data)
                            # log synthetic usage
                            logger.log_api_call(
                                {"completion": {"tokens": {"input": 10, "output": 10}}}
                            )
                        except Exception as e:
                            print(f"Error fetching project time: {e}")
                        continue

                    # Fallback: still a simulated response until Anthropic SDK is integrated
                    print("Thinking...")
                    simulated_response = {
                        "completion": {
                            "text": "This is a simulated Claude response.",
                            "tokens": {"input": 10, "output": 15},
                        }
                    }
                    logger.log_api_call(simulated_response)
                    print(simulated_response["completion"]["text"])
                except Exception as e:
                    print(f"Error while handling command: {e}")
                    continue


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  CLAUDE MCP CLIENT")
    print("  Simple placeholder client for Anthropic Claude")
    print("=" * 60 + "\n")

    if not CLAUDE_API_KEY:
        print(
            "❌ Warning: CLAUDE_API_KEY not set in .env. This client is currently a placeholder."
        )

    if asyncio.run(test_connection()):
        asyncio.run(run_chat_loop())
