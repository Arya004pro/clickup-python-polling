"""
Qwen MCP Client v2.0 - PRODUCTION EDITION WITH SMART HIERARCHY
================================================================
Features:
- Smart Hierarchy Discovery: Auto-discovers folderless lists and complex structures
- Name-to-ID Resolution: Searches hierarchy by keywords when direct lookup fails
- Workspace Memory: Caches discovered structure for fast lookups
- Safety Confirmations: Asks before create/update/delete operations
- Duplicate Detection: Prevents redundant tool calls
- Session Tracking: Token and API usage statistics

Updates from v1:
- ADDED: discover_hierarchy integration for mapping failures
- ADDED: Keyword-based search through workspace structure
- ADDED: Safety confirmation for destructive operations
- ADDED: Folderless list support (lists directly under spaces)
- IMPROVED: Error recovery with automatic hierarchy discovery
- IMPROVED: Context awareness with persistent memory
"""

import asyncio
import os
import json
import re
import logging
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from mcp import ClientSession
from mcp.client.sse import sse_client

load_dotenv()

LM_STUDIO_BASE_URL = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
LM_STUDIO_MODEL = os.getenv("LM_STUDIO_MODEL", "qwen3-4b-instruct-2507")
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/sse")
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

client = OpenAI(base_url=LM_STUDIO_BASE_URL, api_key="lm-studio")

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)
logger.disabled = not DEBUG_MODE

# Suppress httpx logging
logging.getLogger("httpx").setLevel(logging.WARNING)


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
# WORKSPACE MEMORY WITH HIERARCHY
# ============================================================================


class WorkspaceMemory:
    """Persistent context with hierarchical structure and smart name resolution"""

    def __init__(self):
        self.workspace_id = None
        self.workspace_name = None
        self.spaces = {}
        self.folders = {}
        self.lists = {}  # Includes both folder-based and folderless
        self.mapped_projects = {}
        self.hierarchy = None  # Full hierarchy from discover_hierarchy
        self.hierarchy_loaded = False
        self.cache = ResponseCache()

    def add_space(self, space_id, space_name):
        """Add space to memory"""
        self.spaces[space_name.lower()] = {"id": space_id, "name": space_name}

    def add_folder(self, folder_id, folder_name, space_id):
        """Add folder to memory with parent space"""
        self.folders[folder_name.lower()] = {
            "id": folder_id,
            "name": folder_name,
            "space_id": space_id,
        }

    def add_list(self, list_id, list_name, parent_type, parent_id):
        """Add list to memory (can be under folder or space)"""
        self.lists[list_name.lower()] = {
            "id": list_id,
            "name": list_name,
            "parent_type": parent_type,  # 'folder' or 'space'
            "parent_id": parent_id,
        }

    def search_hierarchy(self, keyword):
        """
        Search hierarchy for entities matching keyword.
        Returns list of matches with their full paths.
        """
        if not self.hierarchy:
            return []

        keyword_lower = keyword.lower()
        matches = []

        def search_recursive(node, path=[]):
            # Check current node
            node_name = node.get("name", "")
            if keyword_lower in node_name.lower():
                matches.append(
                    {
                        "id": node.get("id"),
                        "name": node_name,
                        "type": node.get("type"),
                        "path": " → ".join(path + [node_name]),
                    }
                )

            # Search children
            if "children" in node:
                for child in node["children"]:
                    search_recursive(child, path + [node_name])

            # Search folders
            if "folders" in node:
                for folder in node["folders"]:
                    search_recursive(folder, path + [node_name])

            # Search folderless_lists
            if "folderless_lists" in node:
                for lst in node["folderless_lists"]:
                    search_recursive(lst, path + [node_name])

            # Search lists
            if "lists" in node:
                for lst in node["lists"]:
                    search_recursive(lst, path + [node_name])

        # Start search from hierarchy root
        if isinstance(self.hierarchy, dict) and "hierarchy" in self.hierarchy:
            for space in self.hierarchy["hierarchy"]:
                search_recursive(space, [])
        elif isinstance(self.hierarchy, list):
            for space in self.hierarchy:
                search_recursive(space, [])

        return matches

    def get_summary(self):
        return {
            "workspace": f"{self.workspace_name} ({self.workspace_id})"
            if self.workspace_id
            else "Not initialized",
            "spaces_count": len(self.spaces),
            "folders_count": len(self.folders),
            "lists_count": len(self.lists),
            "mapped_projects_count": len(self.mapped_projects),
            "hierarchy_loaded": self.hierarchy_loaded,
        }

    def get_id_lookup_table(self):
        """Generate a text table of Name → ID for the prompt"""
        lines = ["=== ID LOOKUP TABLE (USE THESE IDs) ==="]

        lines.append("--- SPACES ---")
        if not self.spaces:
            lines.append("(No spaces cached - Call get_spaces)")
        else:
            for name, info in list(self.spaces.items())[:10]:
                lines.append(f"• {info['name']} → ID: {info['id']}")
            if len(self.spaces) > 10:
                lines.append(f"... (+{len(self.spaces) - 10} more)")

        lines.append("\n--- FOLDERS ---")
        if not self.folders:
            lines.append("(No folders cached)")
        else:
            for name, info in list(self.folders.items())[:10]:
                lines.append(f"• {info['name']} → ID: {info['id']}")
            if len(self.folders) > 10:
                lines.append(f"... (+{len(self.folders) - 10} more)")

        lines.append("\n--- LISTS ---")
        if not self.lists:
            lines.append("(No lists cached)")
        else:
            for name, info in list(self.lists.items())[:10]:
                parent_type = info.get("parent_type", "unknown")
                lines.append(
                    f"• {info['name']} → ID: {info['id']} (under {parent_type})"
                )
            if len(self.lists) > 10:
                lines.append(f"... (+{len(self.lists) - 10} more)")

        lines.append("\n--- MAPPED PROJECTS ---")
        if not self.mapped_projects:
            lines.append("(No projects mapped - Call list_mapped_projects)")
        else:
            for name, info in list(self.mapped_projects.items())[:10]:
                lines.append(
                    f"• {info['name']} → ID: {info['id']} ({info.get('type', 'unknown')})"
                )
            if len(self.mapped_projects) > 10:
                lines.append(f"... (+{len(self.mapped_projects) - 10} more)")

        return "\n".join(lines)


# ============================================================================
# TOOL CALLING & PARSING
# ============================================================================


def parse_tool_calls(text):
    """
    Robust parsing of tool calls from mixed text.
    Handles Markdown blocks, smart quotes, and messy spacing.
    Supports both <n> and <name> tags for compatibility.
    Also handles simple format: <tool_call>function_name(args)</tool_call>
    """
    clean_text = text.replace(""", '"').replace(""", '"')
    clean_text = clean_text.replace("```xml", "").replace("```", "")

    tool_calls = []

    # Try <n> format first (Gemma style)
    pattern1 = (
        r"<tool_call>\s*<n>(.*?)</n>\s*<arguments>(.*?)</arguments>\s*</tool_call>"
    )
    matches = list(re.finditer(pattern1, clean_text, re.DOTALL | re.IGNORECASE))

    # Try <name> format (alternative style)
    if not matches:
        pattern2 = r"<tool_call>\s*<name>(.*?)</name>\s*<arguments>(.*?)</arguments>\s*</tool_call>"
        matches = list(re.finditer(pattern2, clean_text, re.DOTALL | re.IGNORECASE))

    # Try simple function call format: <tool_call>function_name(...)</tool_call>
    if not matches:
        pattern3 = (
            r"<tool_call>\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\(([^)]*)\)\s*</tool_call>"
        )
        simple_matches = list(
            re.finditer(pattern3, clean_text, re.DOTALL | re.IGNORECASE)
        )
        for match in simple_matches:
            tool_name = match.group(1).strip()
            args_str = match.group(2).strip()

            # Parse arguments from function call format
            arguments = {}
            if args_str:
                # Try to parse as JSON object or extract key=value pairs
                try:
                    if args_str.startswith("{"):
                        arguments = json.loads(args_str)
                    else:
                        # Handle key=value format like: workspace_id="123", name="test"
                        for arg_pair in args_str.split(","):
                            if "=" in arg_pair:
                                key, val = arg_pair.split("=", 1)
                                key = key.strip().strip("\"'")
                                val = val.strip().strip("\"'")
                                arguments[key] = val
                except Exception:
                    logger.warning(f"Could not parse simple format args: {args_str}")

            tool_calls.append({"name": tool_name, "arguments": arguments})
        if tool_calls:
            return tool_calls

    # Try JSON-style tool call: <tool_call>{"name": "...", "arguments": {...}}</tool_call>
    if not matches:
        json_pattern = r'<tool_call>\s*\{.*?"name"\s*:\s*"(.*?)".*?"arguments"\s*:\s*(\{.*?\})\s*\}\s*</tool_call>'
        for m in re.finditer(json_pattern, clean_text, re.DOTALL):
            try:
                args = json.loads(m.group(2))
            except Exception:
                args = {}
            tool_calls.append({"name": m.group(1), "arguments": args})
        if tool_calls:
            return tool_calls

    for match in matches:
        tool_name = match.group(1).strip()
        args_str = match.group(2).strip()

        try:
            if not args_str or args_str == "{}":
                arguments = {}
            else:
                arguments = json.loads(args_str)
        except json.JSONDecodeError:
            try:
                # Try fixing common JSON issues
                fixed_str = args_str.replace("'", '"')
                arguments = json.loads(fixed_str)
            except Exception:
                logger.warning(f"Failed to parse arguments for {tool_name}: {args_str}")
                arguments = {}

        tool_calls.append({"name": tool_name, "arguments": arguments})

    return tool_calls


def format_tools_for_prompt(tools):
    """Generates a simplified schema description for the model"""
    lines = ["=== AVAILABLE TOOLS (CHECK PARAMETERS CAREFULLY) ==="]

    # Priority tools first for better visibility
    priority = [
        "get_workspaces",
        "get_spaces",
        "discover_hierarchy",
        "list_mapped_projects",
        "map_project",
        "get_folders",
        "get_lists",
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
                prop_type = prop_def.get("type", "any")
                params.append(f"{prop_name}: {prop_type}{req}")

        param_str = ", ".join(params) if params else "No arguments"
        desc = (tool.description or "")[:200]  # Truncate long descriptions
        lines.append(f"- {tool.name}({param_str})")
        if desc:
            lines.append(f"  {desc}")

    return "\n".join(lines)


# ============================================================================
# SAFETY CHECKS
# ============================================================================


DESTRUCTIVE_TOOLS = {
    "create_task",
    "update_task",
    "delete_task",
    "create_list",
    "update_list",
    "delete_list",
    "create_folder",
    "update_folder",
    "delete_folder",
}


def needs_confirmation(tool_name):
    """Check if tool requires user confirmation"""
    return tool_name in DESTRUCTIVE_TOOLS


def get_user_confirmation(tool_name, args):
    """Ask user for confirmation before destructive operation"""
    print(f"\n⚠️  SAFETY CHECK: {tool_name}")
    print(f"   Arguments: {json.dumps(args, indent=2)}")
    print("   This will MODIFY data in your ClickUp workspace!")

    response = input("\n   Proceed? (yes/no): ").strip().lower()
    return response in ["yes", "y"]


# ============================================================================
# SYSTEM PROMPT WITH SMART HIERARCHY GUIDANCE
# ============================================================================


def create_system_prompt(memory: WorkspaceMemory, tools_schema_text=""):
    """System prompt with smart hierarchy discovery guidance"""

    summary = memory.get_summary()
    id_lookup = memory.get_id_lookup_table()

    return f"""You are a ClickUp Data Analysis Assistant with direct MCP tool integration and SMART HIERARCHY DISCOVERY.

=== WORKSPACE CONTEXT ===
Workspace: {summary["workspace"]}
Total Spaces: {summary["spaces_count"]}
Total Folders: {summary["folders_count"]}
Total Lists: {summary["lists_count"]}
Total Mapped Projects: {summary["mapped_projects_count"]}
Hierarchy Loaded: {summary["hierarchy_loaded"]}

{id_lookup}

{tools_schema_text}

=== YOUR INTELLIGENT WORKFLOW ===

1. **UNDERSTAND THE REQUEST**
   - What is the user asking for?
   - What specific data do they need?
   - Do I have the necessary IDs in my lookup table?

2. **CHECK LOOKUP TABLE FIRST**
   - If you have the ID → use it directly
   - If you DON'T have the ID → use discover_hierarchy

3. **SMART HIERARCHY DISCOVERY**
   When mapping fails or you can't find an entity:
   
   a) Call discover_hierarchy(workspace_id="{memory.workspace_id}")
   b) Search the returned structure for keywords
   c) Extract the correct ID from the path
   d) Proceed with the original task using the discovered ID
   
   Example:
   User: "Map 3D Jewellery Website project"
   You: 
   - discover_hierarchy returns structure showing:
     Space "3D Team" → folderless_lists → "3D Jewellery Website" (ID: 901613173367)
   - Now you know: map_project(id="901613173367", type="list", alias="3d-jewellery")

4. **HANDLE FOLDERLESS LISTS**
   Lists can exist in TWO places:
   - Inside folders: Space → Folder → List
   - Directly under space: Space → List (folderless)
   
   If get_lists(folder_id=...) fails, the list might be folderless.
   Use discover_hierarchy to see the full structure.

5. **SAFETY PROTOCOL**
   Before calling create_task, update_task, or any destructive operation:
   - The system will automatically ask the user for confirmation
   - DO NOT hesitate to call these tools - safety is handled automatically
   - If user says "no", stop and ask what they'd like to do instead

=== CLICKUP HIERARCHY UNDERSTANDING (CRITICAL) ===

**WORKSPACE vs SPACE - NEVER CONFUSE THESE:**
- WORKSPACE = Top-level team container (use get_workspaces to fetch workspaces)
- SPACE = Second-level container INSIDE a workspace (use get_spaces or list_spaces to fetch spaces)
- FOLDER = Inside a space (use get_folders with space_id)
- LIST = Inside a folder OR directly under space as "folderless list"

**When user says:**
- "fetch workspaces" → use get_workspaces
- "fetch spaces" → use get_spaces or list_spaces (with workspace_id)
- "show me spaces in workspace X" → use get_spaces(workspace_id="X")

**CRITICAL: When mapping projects from a space:**
- Only map entities that are WITHIN that specific space
- Do NOT look at other spaces or suggest folders from different spaces
- Use discover_hierarchy to see the FULL structure first
- Filter results to only show items within the requested space
- Example: "Map 3D Team" should ONLY map items inside the "3D Team" space,
  NOT folders from "AI" space or any other space

=== STRICT RULES ===

1. **Automatic Tool Execution - DO NOT WAIT FOR USER**
   - Execute tools automatically without user confirmation
   - Process tool results immediately in the same turn
   - Continue the workflow until you have a final answer
   - NEVER output tool_call XML as if it's the final answer
   - Tool calls are PROCESSED SILENTLY - users only see your final answer

2. **Multiple Tools for Complex Tasks**
   - For complex requests, execute ALL necessary tools automatically
   - Use discover_hierarchy FIRST when you need to find IDs
   - Then use the discovered IDs to complete the task
   - Continue iterating until task is complete (up to 5 iterations)

3. **Use discover_hierarchy When Stuck**
   - Can't find a space/folder/list by name? → discover_hierarchy
   - Mapping fails? → discover_hierarchy
   - Need to explore structure? → discover_hierarchy

4. **Always Use IDs, Never Names**
   - get_folders requires space_id (not space_name)
   - get_lists requires folder_id (not folder_name)
   - get_tasks requires list_id (not list_name)
   
5. **Error Recovery**
   If a tool fails with "not found" or "invalid ID":
   - Call discover_hierarchy automatically
   - Search for the entity name in the hierarchy
   - Extract the correct ID from the path
   - Retry the original operation - all in the same conversation turn

6. **No Assumptions**
   - Don't assume folder structure
   - Don't assume list locations
   - Always verify with discover_hierarchy when uncertain

=== TOOL CALL FORMAT (IMPORTANT) ===
Use EXACTLY this format - the parser requires it:

<tool_call>
<n>tool_name</n>
<arguments>{{"param": "value"}}</arguments>
</tool_call>

When you need to call a tool:
- Output ONLY the tool_call XML
- NO explanatory text before or after
- NO function call format like function_name()
- The system will process it silently and give you results
- You will then see the results and continue automatically

=== TIME TRACKING REPORT STAGES (FOLLOW THESE STEPS) ===

When user asks for time tracking / time entry reports, follow these stages:

**STAGE 1 — Identify Report Type:**
| User says                                              | Report Type        | Tool to use                                             |
|--------------------------------------------------------|--------------------|---------------------------------------------------------|
| "space wise time report"                               | Space-level        | get_time_tracking_report for each space or mapped project|
| "team member time report" or "member wise report"      | Team-member        | get_time_tracking_report(project=..., group_by="assignee")|
| "folder wise report" / "Luminique report"              | Folder/Project     | get_time_tracking_report(project="Luminique")            |
| "time tracking for <project>"                          | Project-level      | get_project_time_tracking(project_name="...")             |
| "weekly report"                                        | Weekly variant     | get_project_weekly_digest(project_name="...")             |

**STAGE 2 — Resolve Scope:**
- If a project name is given → use it directly as `project=...` parameter  
- The MCP server resolves mapped project names (from project_map.json) automatically
- For folder-level projects (e.g., "Luminique"), pass the name directly — the server will resolve to all lists in that folder
- For space-level projects (e.g., "JewelleryOS", "3D Team"), same — pass the name
- Do NOT try to manually find list IDs — the tools handle resolution internally

**STAGE 3 — Execute:**
- Call the appropriate tool with resolved parameters
- If tool returns error with "not found", try:
  a) list_mapped_projects to check exact alias
  b) Use the exact alias from mapped projects
  c) If still failing, use discover_hierarchy to find IDs

**STAGE 4 — Present Results:**
- Format as a clean readable table
- Include member names, hours tracked, hours estimated
- Include efficiency % where available
- Mention the project scope and period

**IMPORTANT for Folder-Level Projects:**
When a project is mapped as a FOLDER (e.g., "Luminique" is mapped as folder 90167907863):
- It contains MULTIPLE lists (Store Front, Research, Backlog, etc.)
- The time tracking tools automatically aggregate across ALL lists in the folder  
- Do NOT try to query individual lists manually
- Just pass the project/folder name to the tool

Remember: Tool execution is AUTOMATIC and INVISIBLE to users. They only see your final answer after all tools complete!
"""


# ============================================================================
# SESSION TRACKER
# ============================================================================


class SessionTracker:
    def __init__(self):
        self.api_calls = 0
        self.tool_calls = 0
        self.successful_tools = []
        self.failed_tools = []

        # Token tracking
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_tokens = 0

        # Tool-specific tracking
        self.tool_call_history = []

    def log_api(self, response, iteration=None):
        """Log API call with token usage details"""
        self.api_calls += 1

        # Extract token usage from response
        if hasattr(response, "usage") and response.usage:
            input_tokens = getattr(response.usage, "prompt_tokens", 0)
            output_tokens = getattr(response.usage, "completion_tokens", 0)
            total_tokens = getattr(response.usage, "total_tokens", 0)

            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            self.total_tokens += total_tokens

            if DEBUG_MODE and iteration:
                print(
                    f"   [Iteration {iteration}] Tokens: {input_tokens} in, {output_tokens} out"
                )

    def log_tool(self, name, args, success, result_summary=""):
        """Log tool call with details"""
        self.tool_calls += 1

        self.tool_call_history.append(
            {
                "timestamp": datetime.now().isoformat(),
                "name": name,
                "args": args,
                "success": success,
                "result_summary": result_summary[:200],
            }
        )

        if success:
            self.successful_tools.append(name)
        else:
            self.failed_tools.append(name)

    def get_stats(self):
        """Get current statistics as a dictionary"""
        return {
            "api_calls": self.api_calls,
            "tool_calls": self.tool_calls,
            "successful_tools": len(self.successful_tools),
            "failed_tools": len(self.failed_tools),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
        }

    def summary(self):
        """Print clean summary table"""
        # Count unique tools
        tool_counts = {}
        for tool in self.successful_tools:
            tool_counts[tool] = tool_counts.get(tool, 0) + 1

        print("\n" + "=" * 70)
        print("SESSION SUMMARY")
        print("=" * 70)
        print(f"{'Metric':<30} {'Count':<15} {'Details'}")
        print("-" * 70)
        print(f"{'API Calls':<30} {self.api_calls:<15}")
        print(
            f"{'Tool Calls':<30} {self.tool_calls:<15} {len(self.successful_tools)} success, {len(self.failed_tools)} failed"
        )
        print(f"{'Input Tokens':<30} {self.total_input_tokens:<15,}")
        print(f"{'Output Tokens':<30} {self.total_output_tokens:<15,}")
        print(f"{'Total Tokens':<30} {self.total_tokens:<15,}")

        if self.successful_tools:
            tools_used = ", ".join(
                [f"{tool}({count})" for tool, count in sorted(tool_counts.items())]
            )
            print(f"{'Tools Used':<30} {len(tool_counts):<15} {tools_used}")

        print("=" * 70 + "\n")


# ============================================================================
# INITIALIZATION WITH HIERARCHY DISCOVERY
# ============================================================================


async def initialize_workspace(session, memory):
    """Initialize workspace with full hierarchy discovery"""
    try:
        print("\n🔄 Initializing workspace context...")

        # 1. Get workspaces
        result = await session.call_tool("get_workspaces", {})
        workspaces_raw = result.content[0].text if result.content else "[]"
        workspaces = (
            json.loads(workspaces_raw)
            if isinstance(workspaces_raw, str)
            else workspaces_raw
        )

        if not workspaces:
            print("❌ No workspaces found")
            return False

        ws = workspaces[0] if isinstance(workspaces, list) else workspaces
        memory.workspace_id = ws.get("workspace_id", ws.get("id"))
        memory.workspace_name = ws.get("name")
        print(f"✓ Workspace: {memory.workspace_name} ({memory.workspace_id})")

        # 2. Discover full hierarchy
        print("🔍 Discovering workspace hierarchy...")
        try:
            result = await session.call_tool(
                "discover_hierarchy",
                {"workspace_id": memory.workspace_id, "show_archived": False},
            )
            hierarchy_raw = result.content[0].text if result.content else "{}"
            hierarchy_data = (
                json.loads(hierarchy_raw)
                if isinstance(hierarchy_raw, str)
                else hierarchy_raw
            )

            # Handle different response formats
            if isinstance(hierarchy_data, dict):
                if "data" in hierarchy_data:
                    memory.hierarchy = hierarchy_data["data"]
                elif "hierarchy" in hierarchy_data:
                    memory.hierarchy = hierarchy_data
                else:
                    memory.hierarchy = hierarchy_data
            else:
                memory.hierarchy = hierarchy_data

            memory.hierarchy_loaded = True

            # Extract entities from hierarchy
            if memory.hierarchy:
                hierarchy_items = memory.hierarchy.get("hierarchy", [])
                if isinstance(hierarchy_items, list):
                    for space in hierarchy_items:
                        space_id = space.get("id")
                        space_name = space.get("name")
                        if space_id and space_name:
                            memory.add_space(space_id, space_name)

                        # Extract folders
                        for folder in space.get("folders", []):
                            folder_id = folder.get("id")
                            folder_name = folder.get("name")
                            if folder_id and folder_name:
                                memory.add_folder(folder_id, folder_name, space_id)

                            # Extract lists in folders
                            for lst in folder.get("lists", []):
                                list_id = lst.get("id")
                                list_name = lst.get("name")
                                if list_id and list_name:
                                    memory.add_list(
                                        list_id, list_name, "folder", folder_id
                                    )

                        # Extract folderless lists
                        for lst in space.get("folderless_lists", []):
                            list_id = lst.get("id")
                            list_name = lst.get("name")
                            if list_id and list_name:
                                memory.add_list(list_id, list_name, "space", space_id)

            print(
                f"✓ Hierarchy discovered: {len(memory.spaces)} spaces, {len(memory.folders)} folders, {len(memory.lists)} lists"
            )

        except Exception as e:
            print(f"⚠️  Hierarchy discovery failed: {e}")
            print("   Continuing with basic space loading...")

            # Fallback to simple space loading
            result = await session.call_tool(
                "get_spaces", {"workspace_id": memory.workspace_id}
            )
            spaces_raw = result.content[0].text if result.content else "[]"
            spaces = (
                json.loads(spaces_raw) if isinstance(spaces_raw, str) else spaces_raw
            )

            # Handle different response formats
            if isinstance(spaces, dict) and "spaces" in spaces:
                spaces = spaces["spaces"]

            if isinstance(spaces, list):
                for space in spaces:
                    space_id = space.get("space_id", space.get("id"))
                    space_name = space.get("name")
                    if space_id and space_name:
                        memory.add_space(space_id, space_name)
                print(f"✓ Loaded {len(memory.spaces)} spaces")

        # 3. Get mapped projects
        try:
            result = await session.call_tool("list_mapped_projects", {})
            projects_raw = result.content[0].text if result.content else "[]"
            projects = (
                json.loads(projects_raw)
                if isinstance(projects_raw, str)
                else projects_raw
            )

            # Handle different response formats
            if isinstance(projects, dict):
                if "mapped_projects" in projects:
                    projects_dict = projects["mapped_projects"]
                    projects = (
                        list(projects_dict.values())
                        if isinstance(projects_dict, dict)
                        else []
                    )
                elif "projects" in projects:
                    projects = projects["projects"]
                else:
                    projects = []

            if isinstance(projects, list):
                for proj in projects:
                    if isinstance(proj, dict):
                        p_name = proj.get("alias", proj.get("name", "Unknown"))
                        p_id = proj.get("clickup_id", proj.get("id"))
                        p_type = proj.get("clickup_type", proj.get("type", "unknown"))
                        if p_name and p_id:
                            memory.mapped_projects[p_name.lower()] = {
                                "id": p_id,
                                "name": p_name,
                                "type": p_type,
                            }
                print(f"✓ Loaded {len(memory.mapped_projects)} mapped projects")
        except Exception as e:
            print(f"⚠️  Mapped projects loading failed: {e}")

        print("✅ Workspace context initialized!")
        return True

    except Exception as e:
        print(f"❌ Initialization failed: {e}")
        if DEBUG_MODE:
            import traceback

            traceback.print_exc()
        return False


# ============================================================================
# MAIN CLIENT WITH SMART HIERARCHY RECOVERY
# ============================================================================


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
            print("Qwen ClickUp MCP Client v2.0 - Production Edition")
            print("Features: Smart Hierarchy Discovery + Safety Confirmations")
            print("=" * 70)
            print(f"✓ Connected to MCP server: {len(available_tool_names)} tools")
            print(f"✓ Model: {LM_STUDIO_MODEL}")
            print("=" * 70)

            memory = WorkspaceMemory()
            tracker = SessionTracker()

            # Initialize with full hierarchy discovery
            await initialize_workspace(session, memory)

            system_prompt = create_system_prompt(memory, tools_schema_text)
            conversation_history = [{"role": "system", "content": system_prompt}]

            print("\n💡 Ready! Ask me anything about your ClickUp data.")
            print("Commands: 'quit' | 'refresh' | 'stats' | 'search <keyword>'\n")

            while True:
                try:
                    user_input = input("You: ").strip()
                    if not user_input:
                        continue

                    # Handle special commands
                    if user_input.lower() in ["quit", "exit"]:
                        tracker.summary()
                        print("\n👋 Goodbye!\n")
                        break

                    if user_input.lower() == "refresh":
                        print("\n🔄 Refreshing workspace context...")
                        memory = WorkspaceMemory()
                        await initialize_workspace(session, memory)
                        system_prompt = create_system_prompt(memory, tools_schema_text)
                        conversation_history = [
                            {"role": "system", "content": system_prompt}
                        ]
                        print("✅ Refresh complete!\n")
                        continue

                    if user_input.lower() == "stats":
                        stats = tracker.get_stats()
                        print("\n" + "=" * 70)
                        print(f"{'Metric':<30} {'Count'}")
                        print("-" * 70)
                        print(f"{'API Calls':<30} {stats['api_calls']}")
                        print(
                            f"{'Tool Calls':<30} {stats['tool_calls']} ({stats['successful_tools']} success, {stats['failed_tools']} failed)"
                        )
                        print(f"{'Input Tokens':<30} {stats['total_input_tokens']:,}")
                        print(f"{'Output Tokens':<30} {stats['total_output_tokens']:,}")
                        print(f"{'Total Tokens':<30} {stats['total_tokens']:,}")
                        print("=" * 70 + "\n")
                        continue

                    if user_input.lower().startswith("search "):
                        keyword = user_input[7:].strip()
                        if not keyword:
                            print("Usage: search <keyword>\n")
                            continue

                        print(f"\n🔍 Searching hierarchy for: {keyword}")
                        matches = memory.search_hierarchy(keyword)

                        if not matches:
                            print(f"❌ No matches found for '{keyword}'")
                            print(
                                "Tip: Try discover_hierarchy first to load full structure\n"
                            )
                        else:
                            print(f"✓ Found {len(matches)} match(es):\n")
                            for i, match in enumerate(matches, 1):
                                print(f"{i}. {match['name']} (ID: {match['id']})")
                                print(f"   Type: {match['type']}")
                                print(f"   Path: {match['path']}\n")
                        continue

                    # Update system prompt with latest context
                    system_prompt = create_system_prompt(memory, tools_schema_text)
                    conversation_history[0] = {
                        "role": "system",
                        "content": system_prompt,
                    }
                    conversation_history.append({"role": "user", "content": user_input})

                    # Process with max 8 iterations to prevent loops
                    max_iterations = 8
                    iteration = 0
                    tools_called_this_turn = set()
                    is_processing = True

                    # Show processing indicator
                    print("\n⏳ Processing...", end="", flush=True)

                    while iteration < max_iterations:
                        iteration += 1

                        response = client.chat.completions.create(
                            model=LM_STUDIO_MODEL,
                            messages=conversation_history,
                            temperature=0.1,
                            max_tokens=8192,
                        )

                        # Log API call with token usage
                        tracker.log_api(response, iteration=iteration)

                        assistant_response = response.choices[0].message.content or ""
                        tool_calls = parse_tool_calls(assistant_response)

                        # If no tool calls, we have the final answer
                        if not tool_calls:
                            # Strip any leaked <tool_call> XML from final output
                            clean_response = re.sub(
                                r"<tool_call>.*?</tool_call>",
                                "",
                                assistant_response,
                                flags=re.DOTALL,
                            ).strip()
                            # Also clean up any leftover XML tags
                            clean_response = re.sub(
                                r"</?tool_call>|</?n>|</?name>|</?arguments>",
                                "",
                                clean_response,
                            ).strip()

                            if not clean_response:
                                # Model gave empty response after stripping, ask it to summarize
                                conversation_history.append(
                                    {"role": "assistant", "content": assistant_response}
                                )
                                conversation_history.append(
                                    {
                                        "role": "user",
                                        "content": "SYSTEM: Please provide a clear, human-readable summary of all the tool results above. Do NOT output any tool_call XML.",
                                    }
                                )
                                continue

                            # Clear processing indicator and show answer
                            print(f"\r\n🤖 Assistant:\n{clean_response}")
                            print("─" * 70 + "\n")
                            conversation_history.append(
                                {"role": "assistant", "content": assistant_response}
                            )
                            is_processing = False
                            break

                        # Tool calls found - process silently
                        if DEBUG_MODE:
                            print(
                                f"\n   [Debug] Processing {len(tool_calls)} tool call(s)... (iteration {iteration})"
                            )

                        tool_results = []
                        for tc in tool_calls:
                            name = tc["name"]
                            args = tc["arguments"]

                            # Duplicate detection
                            call_signature = (
                                f"{name}:{json.dumps(args, sort_keys=True)}"
                            )
                            if call_signature in tools_called_this_turn:
                                if DEBUG_MODE:
                                    print(f"   [Debug] Blocked duplicate: {name}")
                                tool_results.append(
                                    {
                                        "tool": name,
                                        "result": "SYSTEM: Duplicate call blocked. Use cached result or try different approach.",
                                        "success": False,
                                    }
                                )
                                continue
                            tools_called_this_turn.add(call_signature)

                            # Check if tool exists
                            if name not in available_tool_names:
                                if DEBUG_MODE:
                                    print(f"   [Debug] Tool '{name}' not found")
                                tool_results.append(
                                    {
                                        "tool": name,
                                        "result": f"Error: Tool '{name}' does not exist. Check available tools.",
                                        "success": False,
                                    }
                                )
                                tracker.log_tool(
                                    name,
                                    args,
                                    success=False,
                                    result_summary="Tool not found",
                                )
                                continue

                            # Safety check for destructive operations
                            if needs_confirmation(name):
                                if not get_user_confirmation(name, args):
                                    print(f"   🛑 User cancelled operation: {name}")
                                    tool_results.append(
                                        {
                                            "tool": name,
                                            "result": "SYSTEM: User cancelled this operation. Ask what they'd like to do instead.",
                                            "success": False,
                                        }
                                    )
                                    tracker.log_tool(
                                        name,
                                        args,
                                        success=False,
                                        result_summary="User cancelled",
                                    )
                                    continue

                            # Check cache
                            cached = memory.cache.get(name, args)
                            if cached:
                                if DEBUG_MODE:
                                    print(f"   [Debug] {name} (cached)")
                                tool_results.append(
                                    {"tool": name, "result": cached, "success": True}
                                )
                                continue

                            # Execute tool (show dots for progress)
                            print(".", end="", flush=True)

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

                                # Cache successful result
                                memory.cache.set(name, args, parsed)

                                # Update memory if discover_hierarchy was called
                                if name == "discover_hierarchy" and isinstance(
                                    parsed, dict
                                ):
                                    if "data" in parsed:
                                        memory.hierarchy = parsed["data"]
                                    elif "hierarchy" in parsed:
                                        memory.hierarchy = parsed
                                    else:
                                        memory.hierarchy = parsed
                                    memory.hierarchy_loaded = True
                                    if DEBUG_MODE:
                                        print("\n      [Debug] Hierarchy cache updated")

                                tool_results.append(
                                    {"tool": name, "result": parsed, "success": True}
                                )

                                # Log successful tool call
                                result_summary = str(parsed)[:200] if parsed else ""
                                tracker.log_tool(
                                    name,
                                    args,
                                    success=True,
                                    result_summary=result_summary,
                                )

                            except Exception as e:
                                error_msg = str(e)
                                if DEBUG_MODE:
                                    print(f"\n      [Debug] Error: {error_msg}")

                                # Provide helpful error recovery guidance
                                recovery_hint = ""
                                if (
                                    "not found" in error_msg.lower()
                                    or "invalid" in error_msg.lower()
                                ):
                                    recovery_hint = "\nHINT: Try using discover_hierarchy to find the correct ID."

                                tool_results.append(
                                    {
                                        "tool": name,
                                        "result": f"API Error: {error_msg}{recovery_hint}",
                                        "success": False,
                                    }
                                )

                                # Log failed tool call
                                tracker.log_tool(
                                    name, args, success=False, result_summary=error_msg
                                )

                        # Add assistant response and tool results to conversation
                        conversation_history.append(
                            {"role": "assistant", "content": assistant_response}
                        )

                        results_msg = "TOOL RESULTS (process these and continue - DO NOT show raw tool_call XML to user):\n"
                        for tr in tool_results:
                            status = "✓" if tr["success"] else "✗"
                            result_str = (
                                json.dumps(tr["result"], indent=2)
                                if isinstance(tr["result"], (dict, list))
                                else str(tr["result"])
                            )
                            # Truncate very large results to prevent context overflow
                            if len(result_str) > 6000:
                                result_str = result_str[:6000] + "\n... (truncated)"
                            results_msg += f"{status} {tr['tool']}:\n{result_str}\n\n"

                        results_msg += "\nIMPORTANT: Now provide the final answer to the user. Do NOT output any <tool_call> XML. Present data in a clean, readable format."

                        conversation_history.append(
                            {"role": "user", "content": results_msg}
                        )

                    # If we hit max iterations without a final answer
                    if is_processing:
                        print(
                            "\r\n⚠️  Processing limit reached. Please try a more specific query."
                        )
                        print("─" * 70 + "\n")

                except KeyboardInterrupt:
                    tracker.summary()
                    print("\n👋 Goodbye!\n")
                    break
                except Exception as e:
                    print(f"\n❌ Error: {e}\n")
                    if DEBUG_MODE:
                        import traceback

                        traceback.print_exc()


# ============================================================================
# ENTRY POINT
# ============================================================================


if __name__ == "__main__":
    print("\n🚀 Starting Qwen ClickUp MCP Client v2.0...")
    print("   With Smart Hierarchy Discovery & Safety Features\n")
    try:
        asyncio.run(run_client())
    except KeyboardInterrupt:
        print("\n👋 Goodbye!\n")
    except Exception as e:
        print(f"\n❌ Fatal error: {e}\n")
        if DEBUG_MODE:
            import traceback

            traceback.print_exc()
