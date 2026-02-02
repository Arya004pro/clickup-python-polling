"""
OpenRouter Multi-Model MCP Client
Access to multiple FREE models with 1M+ context windows
Best immediate solution while Gemini quota resets
"""

import asyncio
import json
import os
from typing import Dict, List, Any
from openai import AsyncOpenAI
from mcp import ClientSession
from mcp.client.sse import sse_client
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# --- CONFIGURATION ---
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/sse")

# FREE models with large context windows (prioritized by quality)
FREE_MODELS = [
    {
        "name": "google/gemini-2.0-flash-thinking-exp:free",
        "context": 1_000_000,
        "description": "Gemini 2.0 Flash (FREE) - 1M context, best for complex reasoning",
    },
    {
        "name": "meta-llama/llama-3.3-70b-instruct:free",
        "context": 128_000,
        "description": "Llama 3.3 70B (FREE) - 128K context, excellent tool calling",
    },
    {
        "name": "qwen/qwen-2.5-72b-instruct:free",
        "context": 128_000,
        "description": "Qwen 2.5 72B (FREE) - 128K context, great reasoning",
    },
    {
        "name": "google/gemini-flash-1.5:free",
        "context": 1_000_000,
        "description": "Gemini 1.5 Flash (FREE) - 1M context, reliable",
    },
]

# Knowledge graph for faster lookups
SPACE_MAP = {}
FOLDER_MAP = {}
LIST_MAP = {}


async def build_knowledge_graph(session: ClientSession):
    """Build initial context maps for faster tool parameter resolution"""
    print("\n🧠 Building Knowledge Graph...")
    try:
        result = await session.call_tool("get_spaces", arguments={})
        data = json.loads(result.content[0].text)

        if isinstance(data, list):
            for space in data:
                SPACE_MAP[space["name"].lower()] = space["space_id"]
            print(f"   ✓ Mapped {len(SPACE_MAP)} Spaces")

        if SPACE_MAP:
            first_space_id = list(SPACE_MAP.values())[0]
            try:
                folder_result = await session.call_tool(
                    "get_folders", arguments={"space_id": first_space_id}
                )
                folder_data = json.loads(folder_result.content[0].text)
                if isinstance(folder_data, dict) and "folders" in folder_data:
                    for folder in folder_data["folders"]:
                        FOLDER_MAP[folder["name"].lower()] = folder["id"]
                    print(f"   ✓ Mapped {len(FOLDER_MAP)} Folders")
            except:
                pass

    except Exception as e:
        print(f"   ⚠️  Auto-discovery warning: {e}")


def format_tools_for_openai(tools_list) -> List[Dict[str, Any]]:
    """Convert MCP tools to OpenAI function format"""
    functions = []

    for tool in tools_list.tools:
        properties = tool.inputSchema.get("properties", {})
        required = tool.inputSchema.get("required", [])

        functions.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }
        )

    return functions


async def try_model(client: AsyncOpenAI, model_name: str, test_message: str) -> bool:
    """Test if a model is available and working"""
    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": test_message}],
            max_tokens=50,
        )
        return True
    except Exception as e:
        # Print full exception details for diagnosis
        print("   ⚠️  Model test error:")
        try:
            import traceback

            traceback.print_exception(e, e, e.__traceback__)
        except Exception:
            print(repr(e))

        # Helpful short hints
        error_msg = str(e).lower()
        if "rate_limit" in error_msg or "429" in error_msg:
            print("   Hint: Rate limited or quota exceeded")
        elif "quota" in error_msg:
            print("   Hint: Quota exceeded")
        elif (
            "404" in error_msg or "no endpoint" in error_msg or "not found" in error_msg
        ):
            print("   Hint: Model name may be invalid for this OpenRouter deployment")
        return False


async def select_best_model(client: AsyncOpenAI) -> Dict[str, Any]:
    """Try models in order and return the first working one"""
    print("\n🔍 Finding best available FREE model...")
    # First, fetch and show available models from OpenRouter to guide selection
    try:
        available = await client.models.list()
        names = [
            m.id if hasattr(m, "id") else getattr(m, "name", str(m))
            for m in available.data
        ]
        print(
            f"\n🧾 OpenRouter reports {len(names)} available models (showing up to 20):"
        )
        for n in names[:20]:
            print(f"   - {n}")
    except Exception as e:
        print(f"\n⚠️  Could not list models: {e}")
        # continue to try the configured list

    # Prefer models that actually appear in the gateway's model list
    preferred_keywords = [
        "gemini",
        "gemini-3",
        "gemini-2",
        "gemini-flash",
        "llama-3",
        "llama",
        "qwen",
        "qwen-2",
        "qwen-2.5",
    ]

    found_candidates = []
    try:
        for kw in preferred_keywords:
            matches = [n for n in names if kw in n.lower()]
            if matches:
                found_candidates.extend(matches)
        # Deduplicate while preserving order
        seen = set()
        found_candidates = [
            x for x in found_candidates if not (x in seen or seen.add(x))
        ]
    except Exception:
        found_candidates = []

    # Try found candidates first
    for candidate in found_candidates:
        print(f"\n   Trying gateway-provided model: {candidate}")
        if await try_model(client, candidate, "Hi"):
            print(f"   ✅ Selected: {candidate}")
            return {
                "name": candidate,
                "context": 1_000_000,
                "description": "Gateway-provided model",
            }

    # Fall back to configured FREE_MODELS list
    for model_info in FREE_MODELS:
        model_name = model_info["name"]
        print(f"\n   Testing: {model_name.split('/')[-1]}")
        print(f"      Context: {model_info['context']:,} tokens")

        if await try_model(client, model_name, "Hi"):
            print(f"   ✅ Selected: {model_name}")
            return model_info

    print("\n❌ All models unavailable. Please try again later.")
    return None


async def run_chat_loop():
    print(f"🔌 Connecting to MCP Server: {MCP_SERVER_URL}")

    if not OPENROUTER_API_KEY:
        print("\n❌ OPENROUTER_API_KEY not found in .env file")
        print("\n📝 To get started:")
        print("   1. Go to: https://openrouter.ai/")
        print("   2. Sign up (FREE, no credit card)")
        print("   3. Get API key from: https://openrouter.ai/keys")
        print("   4. Add to .env: OPENROUTER_API_KEY=your_key_here")
        return

    # Initialize OpenRouter client
    client = AsyncOpenAI(
        api_key=OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1"
    )

    async with sse_client(url=MCP_SERVER_URL) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            await build_knowledge_graph(session)

            tools_list = await session.list_tools()
            print(f"✅ Connected! Loaded {len(tools_list.tools)} MCP tools")

            # Select best available model
            model_info = await select_best_model(client)
            if not model_info:
                return

            model_name = model_info["name"]
            context_window = model_info["context"]

            # Convert tools to OpenAI format
            openai_tools = format_tools_for_openai(tools_list)

            # System message
            system_message = {
                "role": "system",
                "content": f"""You are an expert ClickUp Project Analytics Assistant with access to {len(tools_list.tools)} specialized tools.

    CONTEXT SUMMARY:
    - Number of known Spaces: {len(SPACE_MAP)}
    - Number of known Folders mapped: {len(FOLDER_MAP)}
    - Do NOT assume any IDs — call the MCP tools to resolve names to IDs when needed.

    CAPABILITIES (short):
    - Workspace: list/get spaces, folders, lists
    - Tasks: query/filter/analyze tasks by status/assignee/date/tags
    - Analytics: produce distribution, time summaries, bottleneck detection

    SHORT GUIDELINES:
    1. Do not expect full environment dumps; call tools to fetch details.
    2. For large datasets, request the assistant to paginate or aggregate.
    3. Keep assistant outputs concise; use tools for raw data.

    IMPORTANT:
    - Call tools one at a time and wait for their results.
    - If you need full lists (spaces/folders/lists), call `get_spaces`, `get_folders`, or `get_lists` first.
    """,
            }

            messages = [system_message]

            print("\n" + "━" * 60)
            print(f"🚀 {model_name.split('/')[-1]} Ready!")
            print(f"📊 Context: {context_window:,} tokens")
            print("💡 Try: 'Show me all tasks in progress'")
            print("━" * 60)

            while True:
                user_input = input("\n📊 You: ")
                if user_input.lower() in ["quit", "exit", "q"]:
                    print("\n👋 Goodbye!")
                    break

                messages.append({"role": "user", "content": user_input})
                print("\n🤔 Analyzing...")

                max_iterations = 15
                iteration = 0

                try:
                    while iteration < max_iterations:
                        iteration += 1

                        # Call model
                        try:
                            response = await client.chat.completions.create(
                                model=model_name,
                                messages=messages,
                                tools=openai_tools,
                                tool_choice="auto",
                                temperature=0.1,
                                max_tokens=1024,
                            )
                        except Exception as ex:
                            # Detect prompt/credit errors and try message compression
                            msg = str(ex).lower()
                            if (
                                "402" in msg
                                or "prompt tokens" in msg
                                or "requires more credits" in msg
                            ):
                                print(
                                    "\n⚠️  Received 402 / prompt tokens error. Compressing conversation and retrying with smaller max_tokens..."
                                )
                                # Keep only system + last user message to reduce input size
                                messages = (
                                    [messages[0], messages[-1]]
                                    if len(messages) > 1
                                    else messages
                                )
                                try:
                                    response = await client.chat.completions.create(
                                        model=model_name,
                                        messages=messages,
                                        tools=openai_tools,
                                        tool_choice="auto",
                                        temperature=0.1,
                                        max_tokens=512,
                                    )
                                except Exception as ex2:
                                    print("   ⚠️ Retry also failed:")
                                    import traceback

                                    traceback.print_exception(
                                        ex2, ex2, ex2.__traceback__
                                    )
                                    # Attempt fallback to a smaller gateway model
                                    # Use 'names' from outer scope if available
                                    smaller = None
                                    try:
                                        for n in names:
                                            if (
                                                "step" in n.lower()
                                                or "3.5" in n.lower()
                                                or "mini" in n.lower()
                                            ):
                                                smaller = n
                                                break
                                    except NameError:
                                        # 'names' not in scope, skip fallback attempt
                                        pass

                                    if smaller:
                                        print(
                                            f"\n🔁 Falling back to smaller model: {smaller}"
                                        )
                                        model_name = smaller
                                        messages = [messages[0]]
                                        continue
                                    raise ex2
                            else:
                                raise

                        assistant_message = response.choices[0].message
                        messages.append(assistant_message)

                        # Check if model wants to call tools
                        if assistant_message.tool_calls:
                            for tool_call in assistant_message.tool_calls:
                                tool_name = tool_call.function.name
                                tool_args = json.loads(tool_call.function.arguments)

                                print(
                                    f"   🔧 Tool: {tool_name}({json.dumps(tool_args, indent=2)[:100]}...)"
                                )

                                try:
                                    # Execute MCP tool
                                    result = await session.call_tool(
                                        tool_name, arguments=tool_args
                                    )
                                    tool_output = (
                                        result.content[0].text
                                        if result.content
                                        else "{}"
                                    )

                                    # Truncate if needed
                                    if len(tool_output) > 50000:
                                        try:
                                            parsed = json.loads(tool_output)
                                            if (
                                                isinstance(parsed, list)
                                                and len(parsed) > 50
                                            ):
                                                parsed = parsed[:50]
                                                parsed.append(
                                                    {
                                                        "_note": f"Truncated to first 50 of {len(parsed)} items"
                                                    }
                                                )
                                            tool_output = json.dumps(parsed, indent=2)
                                        except:
                                            tool_output = (
                                                tool_output[:10000] + "\n...[TRUNCATED]"
                                            )

                                    print(f"      ✓ Success ({len(tool_output)} chars)")

                                    # Add tool result to messages
                                    messages.append(
                                        {
                                            "role": "tool",
                                            "tool_call_id": tool_call.id,
                                            "content": tool_output,
                                        }
                                    )

                                except Exception as e:
                                    print(f"      ❌ Error: {e}")
                                    messages.append(
                                        {
                                            "role": "tool",
                                            "tool_call_id": tool_call.id,
                                            "content": json.dumps({"error": str(e)}),
                                        }
                                    )

                        else:
                            # Model has final answer
                            answer = assistant_message.content
                            if answer:
                                print(f"\n🤖 Assistant:\n{answer}\n")
                            break

                    if iteration >= max_iterations:
                        print(
                            "\n⚠️  Maximum iterations reached. Resetting conversation."
                        )
                        messages = [system_message]

                except Exception as e:
                    print(f"\n❌ Error: {e}")
                    print("   Tip: Try rephrasing or breaking into smaller questions")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  OPENROUTER MULTI-MODEL MCP CLIENT")
    print("  Access Multiple FREE Models with Large Context")
    print("=" * 60 + "\n")

    asyncio.run(run_chat_loop())
