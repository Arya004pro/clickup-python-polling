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
    print("🔍 Testing MCP Server Connection...")
    try:
        async with sse_client(url=MCP_SERVER_URL) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                print(f"✅ Connection successful! Found {len(tools.tools)} tools")
                return True
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False


async def run_chat_loop():
    # Minimal Claude client: for now we don't import Anthropic SDK directly
    # because local testing may not have the package; instead this function
    # shows the structure and how it would integrate with MCP and logging.

    # Build knowledge graph and session
    async with sse_client(url=MCP_SERVER_URL) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            print("\n🚀 Starting Claude-backed chat interface...\n")

            # Note: Replace the following block with actual Anthropic client code
            # when `anthropic` SDK is available and you want to make real API calls.
            print(f"🔌 Intending to use Claude model: {CLAUDE_MODEL}")
            print("(This is a placeholder client. Replace with real Anthropic calls.)")

            while True:
                user_input = input("\nYou: ")
                if user_input.lower() in ["quit", "exit", "q"]:
                    logger.print_summary()
                    print("Goodbye!")
                    break

                # Simulate a response for now
                print("Thinking...")
                # Simulated response object/dict
                simulated_response = {
                    "completion": {
                        "text": "This is a simulated Claude response.",
                        "tokens": {"input": 10, "output": 15},
                    }
                }

                # Log and show
                logger.log_api_call(simulated_response)
                print(simulated_response["completion"]["text"])


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
