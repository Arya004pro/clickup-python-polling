"""
LM Studio MCP Client v2.4 - PRODUCTION EDITION
============================================
Updates:
- FIXED: "Chain Looping" (Model trying to fetch everything at once)
- ADDED: Tool Schema Injection (Model now sees actual tool definitions)
- ADDED: "Lazy Protocol" (Strict instructions to do minimum work)
- CHANGED: Max iterations reduced to 5 to prevent runaway loops
"""

import asyncio
import os
import json
import re
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from openai import OpenAI
from mcp import ClientSession
from mcp.client.sse import sse_client

load_dotenv()

LM_STUDIO_BASE_URL = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
LM_STUDIO_MODEL = os.getenv("LM_STUDIO_MODEL", "gemma-3-4b")
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/sse")
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

client = OpenAI(base_url=LM_STUDIO_BASE_URL, api_key="lm-studio")

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================
logging.basicConfig(level=logging.CRITICAL)
logger = logging.getLogger(__name__)
logger.disabled = True


# ============================================================================
# RESPONSE CACHE
# ============================================================================


class ResponseCache:
    """Cache tool results by query signature to avoid refetches"""

    def __init__(self, ttl_minutes=30):
        self.cache = {}
        self.ttl = ttl_minutes * 60

    def make_key(self, tool_name, args):
        args_json = json.dumps(args, sort_keys=True)
        return f"{tool_name}:{args_json}"

    def get(self, tool_name, args):
        key = self.make_key(tool_name, args)
        if key in self.cache:
            result, timestamp = self.cache[key]
            if (datetime.now() - timestamp).total_seconds() < self.ttl:
                return result
            else:
                del self.cache[key]
        return None

    def set(self, tool_name, args, result):
        key = self.make_key(tool_name, args)
        self.cache[key] = (result, datetime.now())

    def clear(self):
        self.cache.clear()


# ============================================================================
# WORKSPACE MEMORY
# ============================================================================


class WorkspaceMemory:
    """Persistent context with name-to-ID resolution"""

    def __init__(self):
        self.workspace_id = None
        self.workspace_name = None
        self.workspace_details = None
        self.spaces = {}
        self.folders = {}
        self.mapped_projects = {}
        self.hierarchy_loaded = False
        self.cache = ResponseCache()

    def get_summary(self):
        return {
            "workspace": f"{self.workspace_name} ({self.workspace_id})"
            if self.workspace_id
            else "Not initialized",
            "spaces_count": len(self.spaces),
            "mapped_projects_count": len(self.mapped_projects),
        }

    def get_id_lookup_table(self):
        """Generate a text table of Name -> ID for the prompt"""
        lines = ["=== ID LOOKUP TABLE (USE THESE IDs) ==="]
        lines.append("--- SPACES ---")
        if not self.spaces:
            lines.append("(No spaces cached - Call get_spaces)")
        else:
            for name, info in self.spaces.items():
                lines.append(f"• {info['name']} -> ID: {info['id']}")

        lines.append("\n--- MAPPED PROJECTS ---")
        if not self.mapped_projects:
            lines.append("(No projects mapped - Call list_mapped_projects)")
        else:
            count = 0
            for name, info in self.mapped_projects.items():
                lines.append(f"• {info['name']} -> ID: {info['id']}")
                count += 1
                if count >= 30:
                    lines.append(f"... (+{len(self.mapped_projects) - 30} more)")
                    break
        return "\n".join(lines)


# ============================================================================
# TOOL CALLING & PARSING
# ============================================================================


def parse_tool_calls(text):
    """
    Robust parsing of tool calls from mixed text.
    Handles Markdown blocks, smart quotes, and messy spacing.
    """
    clean_text = text.replace("“", '"').replace("”", '"')
    clean_text = clean_text.replace("```xml", "").replace("```", "")

    tool_calls = []
    pattern = r"<tool_call>\s*<name>(.*?)</name>\s*<arguments>(.*?)</arguments>\s*</tool_call>"
    matches = re.finditer(pattern, clean_text, re.DOTALL | re.IGNORECASE)

    for match in matches:
        tool_name = match.group(1).strip()
        args_str = match.group(2).strip()

        try:
            if not args_str:
                arguments = {}
            else:
                arguments = json.loads(args_str)
        except json.JSONDecodeError:
            try:
                fixed_str = args_str.replace("'", '"')
                arguments = json.loads(fixed_str)
            except Exception:
                arguments = {}

        tool_calls.append({"name": tool_name, "arguments": arguments})

    return tool_calls


def format_tools_for_prompt(tools):
    """Generates a simplified schema description for the model"""
    lines = ["=== AVAILABLE TOOLS (CHECK PARAMETERS CAREFULLY) ==="]

    # Priority tools first
    priority = [
        "get_workspaces",
        "get_spaces",
        "list_mapped_projects",
        "get_folders",
        "get_tasks",
    ]

    sorted_tools = sorted(tools, key=lambda t: (0 if t.name in priority else 1, t.name))

    for tool in sorted_tools:
        # Simple signature generation
        params = []
        if tool.inputSchema and "properties" in tool.inputSchema:
            for prop_name, prop_def in tool.inputSchema["properties"].items():
                req = (
                    " (required)"
                    if tool.inputSchema.get("required")
                    and prop_name in tool.inputSchema["required"]
                    else ""
                )
                params.append(f"{prop_name}{req}")

        param_str = ", ".join(params) if params else "No arguments"
        lines.append(f"- {tool.name}({param_str}): {tool.description or ''}")

    return "\n".join(lines)


# ============================================================================
# SYSTEM PROMPT
# ============================================================================


def create_system_prompt(memory: WorkspaceMemory, tools_schema_text=""):
    """System prompt with LAZY PROTOCOL"""

    summary = memory.get_summary()
    id_lookup = memory.get_id_lookup_table()

    return f"""You are a ClickUp Data Analysis Assistant with direct MCP tool integration.

=== WORKSPACE CONTEXT ===
Workspace: {summary["workspace"]}
Total Spaces: {summary["spaces_count"]}
Total Mapped Projects: {summary["mapped_projects_count"]}

{id_lookup}

{tools_schema_text}

YOUR GUIDING PRINCIPLE: "DO THE MINIMUM"
========================================
1. ANALYZE the user's specific request.
2. CALL the SINGLE tool that answers it.
3. STOP. Do not try to be "thorough" by fetching extra data.

STRICT RULES (ANTI-LOOPING):
----------------------------
1. **Fetch Workspace**: 
   - User says: "Fetch workspace"
   - You call: `get_workspaces()` 
   - STOP. Do not call `get_folders` or `discover_projects`.

2. **Fetch Spaces**:
   - User says: "Get spaces"
   - You call: `get_spaces(workspace_id=...)`
   - STOP.

3. **Discovery Protocol**:
   - NEVER call `discover_projects` unless the user specifically asks to "scan hierarchy" or "find missing projects".
   - It is an EXPENSIVE operation. Do not use it for simple retrieval.

4. **Argument Safety**:
   - Check "AVAILABLE TOOLS" above.
   - `get_workspaces` takes NO arguments. Do not send `{{"workspace": "Name"}}`.
   - `get_folders` requires `space_id`.

TOOL CALL TEMPLATE:
<tool_call>
<name>tool_name</name>
<arguments>{{"param": "value"}}</arguments>
</tool_call>
"""


# ============================================================================
# SESSION TRACKER
# ============================================================================


class SessionTracker:
    def __init__(self):
        self.api_calls = 0
        self.tool_calls = 0
        self.failed_tools = []

    def log_api(self, response):
        self.api_calls += 1

    def log_tool(self, name, success):
        self.tool_calls += 1
        if not success:
            self.failed_tools.append(name)

    def summary(self):
        print("\n" + "=" * 70)
        print("SESSION SUMMARY")
        print(f"API Calls: {self.api_calls} | Tool Calls: {self.tool_calls}")
        print("=" * 70)


# ============================================================================
# MAIN CLIENT
# ============================================================================


async def initialize_workspace(session, memory):
    try:
        print("\n🔄 Initializing workspace context...")

        # 1. Get workspaces
        result = await session.call_tool("get_workspaces", {})
        workspaces = json.loads(result.content[0].text)

        if not workspaces:
            print("❌ No workspaces found")
            return False

        ws = workspaces[0]
        memory.workspace_id = ws.get("workspace_id", ws.get("id"))
        memory.workspace_name = ws.get("name")
        print(f"✓ Workspace: {memory.workspace_name} ({memory.workspace_id})")

        # 2. Get spaces
        result = await session.call_tool(
            "get_spaces", {"workspace_id": memory.workspace_id}
        )
        spaces = json.loads(result.content[0].text)

        for space in spaces:
            memory.spaces[space.get("name").lower()] = {
                "id": space.get("space_id", space.get("id")),
                "name": space.get("name"),
            }
        print(f"✓ Loaded {len(memory.spaces)} spaces")

        # 3. Get mapped projects
        try:
            result = await session.call_tool("list_mapped_projects", {})
            projects = json.loads(result.content[0].text)
            if isinstance(projects, dict) and "projects" in projects:
                projects = projects["projects"]

            for proj in projects:
                p_name = proj.get("name", proj.get("alias", "Unknown"))
                memory.mapped_projects[p_name.lower()] = {
                    "id": proj.get("clickup_id", proj.get("id")),
                    "name": p_name,
                }
            print(f"✓ Loaded {len(memory.mapped_projects)} mapped projects")
        except Exception:
            pass

        print("✅ Workspace context initialized!")
        return True

    except Exception as e:
        print(f"❌ Initialization failed: {e}")
        return False


async def run_client():
    async with sse_client(MCP_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Fetch tools schema dynamically
            tools_result = await session.list_tools()
            tools_list = tools_result.tools
            available_tool_names = {t.name for t in tools_list}
            tools_schema_text = format_tools_for_prompt(tools_list)

            print("\n" + "=" * 70)
            print("ClickUp MCP Client - Production Edition")
            print("=" * 70)
            print(f"✓ Connected to MCP server: {len(available_tool_names)} tools")
            print(f"✓ LM Studio model: {LM_STUDIO_MODEL}")
            print("=" * 70)

            memory = WorkspaceMemory()
            tracker = SessionTracker()

            await initialize_workspace(session, memory)

            system_prompt = create_system_prompt(memory, tools_schema_text)
            conversation_history = [{"role": "system", "content": system_prompt}]

            print("\n💡 Ready! Ask me anything about your ClickUp data.")
            print("Type 'quit' to exit, 'refresh' to reload data\n")

            while True:
                try:
                    user_input = input("You: ").strip()
                    if not user_input:
                        continue

                    if user_input.lower() in ["quit", "exit"]:
                        tracker.summary()
                        print("\n👋 Goodbye!\n")
                        break

                    if user_input.lower() == "refresh":
                        memory = WorkspaceMemory()
                        await initialize_workspace(session, memory)
                        system_prompt = create_system_prompt(memory, tools_schema_text)
                        conversation_history = [
                            {"role": "system", "content": system_prompt}
                        ]
                        continue

                    system_prompt = create_system_prompt(memory, tools_schema_text)
                    conversation_history[0] = {
                        "role": "system",
                        "content": system_prompt,
                    }
                    conversation_history.append({"role": "user", "content": user_input})

                    # LOWERED MAX ITERATIONS TO PREVENT LOOPS
                    max_iterations = 5
                    iteration = 0

                    tools_called_this_turn = set()

                    while iteration < max_iterations:
                        iteration += 1

                        response = client.chat.completions.create(
                            model=LM_STUDIO_MODEL,
                            messages=conversation_history,
                            temperature=0.1,
                            max_tokens=10000,
                        )

                        if iteration == 1:
                            tracker.log_api(response)

                        assistant_response = response.choices[0].message.content or ""
                        tool_calls = parse_tool_calls(assistant_response)

                        if not tool_calls:
                            print(f"\n🤖 Assistant:\n{assistant_response}\n")
                            conversation_history.append(
                                {"role": "assistant", "content": assistant_response}
                            )
                            break

                        print(f"\n🔧 Processing {len(tool_calls)} tool call(s)...")

                        tool_results = []
                        for tc in tool_calls:
                            name = tc["name"]
                            args = tc["arguments"]

                            # Duplicate Check
                            call_signature = (
                                f"{name}:{json.dumps(args, sort_keys=True)}"
                            )
                            if call_signature in tools_called_this_turn:
                                print(f"   🛑 Blocked duplicate call: {name}")
                                tool_results.append(
                                    {
                                        "tool": name,
                                        "result": "SYSTEM: Duplicate call blocked. Stop and answer.",
                                        "success": False,
                                    }
                                )
                                continue
                            tools_called_this_turn.add(call_signature)

                            if name not in available_tool_names:
                                print(f"   ✗ Tool '{name}' not found")
                                tool_results.append(
                                    {
                                        "tool": name,
                                        "result": "Error: Tool not found.",
                                        "success": False,
                                    }
                                )
                                continue

                            cached = memory.cache.get(name, args)
                            if cached:
                                print(f"   → {name} (cached)")
                                tool_results.append(
                                    {"tool": name, "result": cached, "success": True}
                                )
                                continue

                            print(f"   → {name}({json.dumps(args)})")

                            try:
                                result = await session.call_tool(name, args)

                                if isinstance(result.content, list) and result.content:
                                    raw = (
                                        result.content[0].text
                                        if hasattr(result.content[0], "text")
                                        else str(result.content)
                                    )
                                else:
                                    raw = str(result.content)

                                try:
                                    parsed = json.loads(raw)
                                except Exception:
                                    parsed = raw

                                memory.cache.set(name, args, parsed)
                                tool_results.append(
                                    {"tool": name, "result": parsed, "success": True}
                                )
                                print("      ✓ Success")

                            except Exception as e:
                                print(f"      ✗ Error: {e}")
                                tool_results.append(
                                    {
                                        "tool": name,
                                        "result": f"API Error: {str(e)}",
                                        "success": False,
                                    }
                                )

                        conversation_history.append(
                            {"role": "assistant", "content": assistant_response}
                        )

                        results_msg = "TOOL RESULTS:\n"
                        for tr in tool_results:
                            status = "✓" if tr["success"] else "✗"
                            results_msg += f"{status} {tr['tool']}:\n{json.dumps(tr['result'], indent=2)}\n"

                        conversation_history.append(
                            {"role": "user", "content": results_msg}
                        )

                except KeyboardInterrupt:
                    tracker.summary()
                    print("\n👋 Goodbye!\n")
                    break
                except Exception as e:
                    print(f"\n❌ Error: {e}\n")


if __name__ == "__main__":
    print("\n🚀 Starting ClickUp MCP Client...")
    try:
        asyncio.run(run_client())
    except KeyboardInterrupt:
        print("\n👋 Goodbye!\n")
