"""
LM Studio MCP Client — Qwen v2-Compatible Edition (Hallucination-Controlled)
===========================================================================
IMPORTANT:
- Recreates lmstudio_client_v2 behaviour for Qwen models
- Strict tool-first execution (prevents hallucinated answers)
- Proper tool-call parsing for Qwen XML/JSON mixed formats
- Uses LM Studio token usage directly (response['usage'])
- Prevents raw <tool_call> output to user
- Keeps caching + iterative agent loop from v2

.env:
LM_STUDIO_BASE_URL=http://localhost:1234/v1
LM_STUDIO_MODEL=qwen3-4b-instruct-2507
MCP_SERVER_URL=http://127.0.0.1:8001/sse
"""

import asyncio
import os
import json
import re
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from mcp import ClientSession
from mcp.client.sse import sse_client

load_dotenv()

LM_STUDIO_BASE_URL = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
LM_STUDIO_MODEL = os.getenv("LM_STUDIO_MODEL", "qwen3-4b-instruct-2507")
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/sse")

client = OpenAI(base_url=LM_STUDIO_BASE_URL, api_key="lm-studio")

# ============================================================
# TOKEN LOGGER (REAL TOKENS FROM LM STUDIO)
# ============================================================


class TokenLogger:
    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0
        self.api_calls = 0
        self.tool_calls = 0
        self.successful_tools = []
        self.failed_tools = []

    def add_usage(self, usage):
        """Track tokens from API response (CompletionUsage object)"""
        if not usage:
            return
        self.api_calls += 1
        # Use getattr for Pydantic model object
        self.input_tokens += getattr(usage, "prompt_tokens", 0)
        self.output_tokens += getattr(usage, "completion_tokens", 0)

    def log_tool(self, name, success=True):
        """Log tool execution"""
        self.tool_calls += 1
        if success:
            self.successful_tools.append(name)
        else:
            self.failed_tools.append(name)

    def report(self):
        """Print clean summary table"""
        # Count unique tools
        tool_counts = {}
        for tool in self.successful_tools:
            tool_counts[tool] = tool_counts.get(tool, 0) + 1

        total_tokens = self.input_tokens + self.output_tokens

        print("\n" + "=" * 60)
        print("SESSION SUMMARY")
        print("=" * 60)
        print(f"{'Metric':<25} {'Count':<15} {'Details'}")
        print("-" * 60)
        print(f"{'API Calls':<25} {self.api_calls:<15}")
        print(
            f"{'Tool Calls':<25} {self.tool_calls:<15} {len(self.successful_tools)} success, {len(self.failed_tools)} failed"
        )
        print(f"{'Input Tokens':<25} {self.input_tokens:<15,}")
        print(f"{'Output Tokens':<25} {self.output_tokens:<15,}")
        print(f"{'Total Tokens':<25} {total_tokens:<15,}")

        if self.successful_tools:
            tools_used = ", ".join(
                [f"{tool}({count})" for tool, count in sorted(tool_counts.items())]
            )
            print(f"{'Tools Used':<25} {len(tool_counts):<15} {tools_used}")

        print("=" * 60 + "\n")


# ============================================================
# PROJECT CONTEXT MANAGER
# ============================================================


class ProjectContext:
    """Manages mapped project information for better query accuracy"""

    def __init__(self):
        self.projects = []
        self.project_map = {}

    def load_projects(self, projects_data):
        """Load mapped projects from MCP server"""
        if isinstance(projects_data, dict) and "error" not in projects_data:
            self.projects = projects_data if isinstance(projects_data, list) else []

            # Create a lookup map
            for proj in self.projects:
                name = proj.get("alias", "").replace("_", " ")
                self.project_map[name.lower()] = {
                    "alias": proj.get("alias"),
                    "id": proj.get("clickup_id"),
                    "type": proj.get("type", "Unknown"),
                }

    def get_project_guidance(self):
        """Generate guidance text for the system prompt"""
        if not self.projects:
            return ""

        lines = ["\n=== MAPPED PROJECTS (Use these exact names) ==="]
        for proj in self.projects[:30]:  # Limit to avoid token overflow
            alias = proj.get("alias", "Unknown")
            proj_type = proj.get("type", "?")
            # Convert alias to readable name
            readable_name = alias.replace("_", " ")
            lines.append(f'- "{readable_name}" (type: {proj_type})')

        if len(self.projects) > 30:
            lines.append(f"... and {len(self.projects) - 30} more")

        lines.append(
            "\nIMPORTANT: Use exact project names from this list when calling project-related tools."
        )
        lines.append(
            "If a user mentions a project, verify it matches one of these names.\n"
        )

        return "\n".join(lines)

    def suggest_project(self, query):
        """Suggest the best matching project for a query"""
        query_lower = query.lower().strip()

        # Direct match
        if query_lower in self.project_map:
            return self.project_map[query_lower]

        # Partial match
        matches = []
        for name, info in self.project_map.items():
            if query_lower in name or name in query_lower:
                matches.append((name, info))

        if matches:
            return matches[0][1]  # Return first match

        return None


# ============================================================
# RESPONSE CACHE (FROM V2)
# ============================================================


class ResponseCache:
    def __init__(self, ttl_minutes=30):
        self.cache = {}
        self.ttl = ttl_minutes * 60

    def key(self, tool, args):
        return f"{tool}:{json.dumps(args, sort_keys=True)}"

    def get(self, tool, args):
        k = self.key(tool, args)
        if k in self.cache:
            result, ts = self.cache[k]
            if (datetime.now() - ts).total_seconds() < self.ttl:
                return result
            del self.cache[k]
        return None

    def set(self, tool, args, result):
        self.cache[self.key(tool, args)] = (result, datetime.now())


# ============================================================
# TOOL SCHEMA FORMATTER
# ============================================================


def format_tools_for_prompt(tools):
    """Generates a simplified schema description for the model"""
    lines = ["=== AVAILABLE TOOLS ==="]

    # Sort by name
    sorted_tools = sorted(tools, key=lambda t: t.name)

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


# ============================================================
# ROBUST TOOL PARSER (QWEN SAFE)
# ============================================================


def parse_tool_calls(text):
    text = text.replace("```xml", "").replace("```", "")

    calls = []

    # JSON-style tool call
    json_pattern = r'<tool_call>\s*\{.*?"name"\s*:\s*"(.*?)".*?"arguments"\s*:\s*(\{.*?\})\s*\}\s*</tool_call>'
    for m in re.finditer(json_pattern, text, re.DOTALL):
        try:
            args = json.loads(m.group(2))
        except Exception:
            args = {}
        calls.append({"name": m.group(1), "arguments": args})

    # XML structured
    xml_pattern = r"<tool_call>\s*<name>(.*?)</name>\s*<arguments>(.*?)</arguments>\s*</tool_call>"
    for m in re.finditer(xml_pattern, text, re.DOTALL):
        try:
            args = json.loads(m.group(2) or "{}")
        except Exception:
            args = {}
        calls.append({"name": m.group(1), "arguments": args})

    return calls


# ============================================================
# STRICT SYSTEM PROMPT (ANTI HALLUCINATION)
# ============================================================


def build_system_prompt(tools_schema_text="", project_guidance_text=""):
    return f"""
You are a ClickUp MCP assistant.

{tools_schema_text}

{project_guidance_text}

MANDATORY RULES:
- ONLY call tools that exist in the AVAILABLE TOOLS list above.
- Never hallucinate tool names.
- Never make up tools that don't exist.
- Always call MCP tools before answering factual queries.
- Never display tool_call XML to user.
- Wait for tool results before answering.
- Follow instructions exactly.
- Do not assume workspace/space IDs.

CLICKUP HIERARCHY (CRITICAL):
- WORKSPACE: Top-level container (use get_workspaces to fetch)
- SPACE: Inside a workspace (use get_spaces or list_spaces to fetch)
- FOLDER: Inside a space
- LIST: Inside a folder or space
- When user asks for "spaces", use get_spaces or list_spaces, NOT get_workspaces
- When user asks for "workspaces", use get_workspaces, NOT get_spaces

PROJECT NAME RULES (CRITICAL FOR ACCURACY):
- When user mentions a project name, use the EXACT name from MAPPED PROJECTS list
- Project names are case-sensitive and must match exactly
- Examples of correct usage:
  * "3D Jewellery Website" (exact match from list)
  * "3D Configurator" (exact match from list)  
  * "3D Team" (this is a SPACE containing multiple lists)
- If asking for time tracking of "3D Jewellery Website", use project_name="3D Jewellery Website" exactly
- DO NOT use "3D Team" when user specifically means "3D Jewellery Website"

When tool is needed, ONLY output:
<tool_call>
{{ "name": "tool_name", "arguments": {{"param": "value"}} }}
</tool_call>

If a tool is needed but not in the AVAILABLE TOOLS list, tell the user it's not available.
"""


# ============================================================
# MAIN CLIENT LOOP (V2 STRUCTURE)
# ============================================================


async def run_client():
    token_logger = TokenLogger()
    cache = ResponseCache()
    project_context = ProjectContext()

    async with sse_client(MCP_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Fetch tools schema dynamically
            tools_result = await session.list_tools()
            tools_list = tools_result.tools
            available_tool_names = {t.name for t in tools_list}
            tools_schema_text = format_tools_for_prompt(tools_list)

            print(f"\n✓ Connected to {len(available_tool_names)} tools")

            # Load mapped projects
            try:
                print("⏳ Loading mapped projects...")
                proj_result = await session.call_tool("list_mapped_projects", {})
                if proj_result and proj_result.content:
                    raw_data = proj_result.content[0].text
                    projects_data = (
                        json.loads(raw_data) if isinstance(raw_data, str) else raw_data
                    )
                    project_context.load_projects(projects_data)
                    print(f"✓ Loaded {len(project_context.projects)} mapped projects")

                    # Show first few projects to user
                    if project_context.projects:
                        print("\n📊 Available Projects (first 10):")
                        for i, proj in enumerate(project_context.projects[:10]):
                            name = proj.get("alias", "").replace("_", " ")
                            proj_type = proj.get("type", "?")
                            print(f"  {i + 1}. {name} ({proj_type})")
                        if len(project_context.projects) > 10:
                            print(
                                f"  ... and {len(project_context.projects) - 10} more"
                            )
            except Exception as e:
                print(f"⚠ Warning: Could not load mapped projects: {e}")

            print("\nQwen MCP client ready. Type 'quit' to exit.\n")

            # Build system prompt with tools and project guidance
            project_guidance_text = project_context.get_project_guidance()
            system_prompt = build_system_prompt(
                tools_schema_text, project_guidance_text
            )
            conversation = [{"role": "system", "content": system_prompt}]

            while True:
                user_input = input("You: ").strip()

                if user_input.lower() in ["quit", "exit"]:
                    token_logger.report()
                    break

                conversation.append({"role": "user", "content": user_input})

                max_iter = 5
                iteration = 0

                while iteration < max_iter:
                    iteration += 1

                    response = client.chat.completions.create(
                        model=LM_STUDIO_MODEL,
                        messages=conversation,
                        temperature=0.1,
                    )

                    token_logger.add_usage(response.usage)

                    assistant_text = response.choices[0].message.content or ""
                    tool_calls = parse_tool_calls(assistant_text)

                    if not tool_calls:
                        # Final answer - add visual separator
                        print("\nAssistant:", assistant_text)
                        print("─" * 60)
                        conversation.append(
                            {"role": "assistant", "content": assistant_text}
                        )
                        break

                    # Tool execution in progress (don't show to user)
                    # print(f"Executing {len(tool_calls)} tool(s)...")

                    for tc in tool_calls:
                        name, args = tc["name"], tc["arguments"]

                        # Check if tool exists
                        if name not in available_tool_names:
                            # Only show error, not tool result
                            # print(f"\n❌ Tool '{name}' not found in available tools")
                            result = f"Error: Tool '{name}' does not exist. Check AVAILABLE TOOLS list."
                            token_logger.log_tool(name, success=False)
                        else:
                            # Validation for project-related tools
                            if "project" in name and "project_name" in args:
                                project_name = args["project_name"]
                                suggestion = project_context.suggest_project(
                                    project_name
                                )
                                if suggestion and suggestion.get("alias"):
                                    suggested_name = suggestion["alias"].replace(
                                        "_", " "
                                    )
                                    if suggested_name.lower() != project_name.lower():
                                        # Hide hints from user
                                        pass
                                        # print(
                                        #     f"  💡 Hint: Using '{suggested_name}' (matched from '{project_name}')"
                                        # )

                            cached = cache.get(name, args)
                            if cached:
                                result = cached
                                # print(f"✓ {name} (cached)")
                            else:
                                try:
                                    # Hide tool execution details from user
                                    # print(
                                    #     f"  Calling: {name}({json.dumps(args)[:100]})"
                                    # )
                                    res = await session.call_tool(name, args)
                                    raw = res.content[0].text
                                    try:
                                        result = json.loads(raw)
                                    except Exception:
                                        result = raw
                                    cache.set(name, args, result)
                                    token_logger.log_tool(name, success=True)
                                    # print(f"✓ {name}")
                                except Exception as e:
                                    result = f"Tool error: {e}"
                                    token_logger.log_tool(name, success=False)
                                    # print(f"✗ {name}: {e}")

                        # Hide tool results from user - only show final answer
                        # print(
                        #     f"\nTool Result ({name}):\n{json.dumps(result, indent=2)[:4000]}\n"
                        # )

                        conversation.append(
                            {"role": "assistant", "content": assistant_text}
                        )
                        conversation.append(
                            {
                                "role": "user",
                                "content": f"TOOL RESULT:\n{json.dumps(result)[:4000]}",
                            }
                        )


if __name__ == "__main__":
    print("Starting Qwen MCP Client...")
    asyncio.run(run_client())
