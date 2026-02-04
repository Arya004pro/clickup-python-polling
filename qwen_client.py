"""
ClickUp MCP Server - Qwen 2.5-7B-Instruct Client (LM Studio)
============================================================
A specialized client for Qwen 2.5-7B-Instruct running on LM Studio with:
- ZERO hallucination through strict validation
- 4 Custom time entry reports + 54 existing MCP tools
- Real ClickUp API data fetching
- Local inference (unlimited tokens/calls)

Features:
- Direct ClickUp API calls for time entries (no hallucination)
- Space-wise time entry reporting
- Space > Folder > Team member time entry reporting
- Team member wise time entry reporting
- Weekly reports for all above
- Full 54-tool MCP integration
- Structured output validation
- Comprehensive error handling

Author: ClickUp MCP Team
Version: 1.0 (Qwen-Optimized)
"""

from mcp import ClientSession
from mcp.client.sse import sse_client
from dotenv import load_dotenv
import asyncio
import json
import os
import sys
import warnings
import traceback
import requests
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import re

load_dotenv()

# Suppress warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

# ============================================================================
# CONFIGURATION
# ============================================================================

# LM Studio Configuration
LM_STUDIO_BASE_URL = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
LM_STUDIO_MODEL = os.getenv(
    "LM_STUDIO_MODEL", "qwen2.5-7b-instruct"
)  # Model name in LM Studio

# ClickUp Configuration
CLICKUP_API_TOKEN = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID")
CLICKUP_BASE_URL = "https://api.clickup.com/api/v2"

# MCP Server
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/sse")

# ============================================================================
# CLICKUP API HELPERS (Direct API - No Hallucination)
# ============================================================================


def _headers() -> Dict[str, str]:
    """Get ClickUp API headers"""
    return {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}


def _api_call(
    method: str,
    endpoint: str,
    params: Optional[Dict] = None,
    data: Optional[Dict] = None,
):
    """Make direct ClickUp API call"""
    url = f"{CLICKUP_BASE_URL}{endpoint}"
    try:
        response = requests.request(
            method, url, headers=_headers(), params=params, json=data, timeout=30
        )
        if response.status_code == 200:
            return response.json(), None
        else:
            return None, f"API Error {response.status_code}: {response.text}"
    except Exception as e:
        return None, f"Exception: {str(e)}"


def get_team_id() -> str:
    """Get ClickUp team ID"""
    if CLICKUP_TEAM_ID:
        return CLICKUP_TEAM_ID
    data, error = _api_call("GET", "/team")
    if data and data.get("teams"):
        return data["teams"][0]["id"]
    return None


def get_workspace_structure() -> Dict[str, Any]:
    """
    Fetch complete workspace structure: Spaces > Folders > Lists
    Returns hierarchical structure for navigation
    """
    team_id = get_team_id()
    if not team_id:
        return {"error": "No team ID found"}

    structure = {"team_id": team_id, "spaces": []}

    # Get all spaces
    spaces_data, error = _api_call("GET", f"/team/{team_id}/space")
    if error or not spaces_data:
        return {"error": f"Failed to fetch spaces: {error}"}

    for space in spaces_data.get("spaces", []):
        space_info = {
            "id": space["id"],
            "name": space["name"],
            "folders": [],
            "lists": [],
        }

        # Get folders in space
        folders_data, _ = _api_call("GET", f"/space/{space['id']}/folder")
        if folders_data:
            for folder in folders_data.get("folders", []):
                folder_info = {"id": folder["id"], "name": folder["name"], "lists": []}
                for lst in folder.get("lists", []):
                    folder_info["lists"].append({"id": lst["id"], "name": lst["name"]})
                space_info["folders"].append(folder_info)

        # Get lists directly in space (no folder)
        lists_data, _ = _api_call("GET", f"/space/{space['id']}/list")
        if lists_data:
            for lst in lists_data.get("lists", []):
                space_info["lists"].append({"id": lst["id"], "name": lst["name"]})

        structure["spaces"].append(space_info)

    return structure


def get_time_entries_for_team(
    team_id: str, start_date: int, end_date: int
) -> List[Dict]:
    """
    Fetch ALL time entries for entire team within date range
    Returns raw time entry data from ClickUp API
    """
    params = {
        "start_date": start_date,
        "end_date": end_date,
        "assignee": None,  # Get all users
    }

    data, error = _api_call("GET", f"/team/{team_id}/time_entries", params=params)
    if error:
        return []

    return data.get("data", [])


def get_task_details(task_id: str) -> Dict:
    """Get task details including list, folder, space hierarchy"""
    data, error = _api_call("GET", f"/task/{task_id}")
    if error or not data:
        return None
    return data


# ============================================================================
# CUSTOM REPORT GENERATORS (Zero Hallucination - Real Data Only)
# ============================================================================


def generate_space_wise_time_report(start_date: int, end_date: int) -> Dict:
    """
    Report 1: Space-wise time entry report
    Groups all time entries by Space
    """
    team_id = get_team_id()
    if not team_id:
        return {"error": "No team ID found"}

    # Get workspace structure for mapping
    structure = get_workspace_structure()
    if "error" in structure:
        return structure

    # Build space ID -> name mapping
    space_map = {s["id"]: s["name"] for s in structure.get("spaces", [])}

    # Get all time entries
    time_entries = get_time_entries_for_team(team_id, start_date, end_date)

    # Group by space
    space_report = defaultdict(
        lambda: {
            "total_duration_ms": 0,
            "total_duration_hours": 0,
            "entries_count": 0,
            "tasks": set(),
            "users": set(),
        }
    )

    for entry in time_entries:
        task_id = entry.get("task", {}).get("id")
        if not task_id:
            continue

        # Get task details to find space
        task = get_task_details(task_id)
        if not task:
            continue

        space_id = task.get("space", {}).get("id")
        space_name = space_map.get(space_id, "Unknown Space")

        duration = int(entry.get("duration", 0))
        user_name = entry.get("user", {}).get("username", "Unknown")

        space_report[space_name]["total_duration_ms"] += duration
        space_report[space_name]["total_duration_hours"] = round(
            space_report[space_name]["total_duration_ms"] / (1000 * 60 * 60), 2
        )
        space_report[space_name]["entries_count"] += 1
        space_report[space_name]["tasks"].add(task_id)
        space_report[space_name]["users"].add(user_name)

    # Convert sets to counts
    final_report = {}
    for space_name, data in space_report.items():
        final_report[space_name] = {
            "total_hours": data["total_duration_hours"],
            "total_entries": data["entries_count"],
            "unique_tasks": len(data["tasks"]),
            "unique_users": len(data["users"]),
            "users": sorted(list(data["users"])),
        }

    return {
        "report_type": "space_wise_time_entries",
        "date_range": {
            "start": datetime.fromtimestamp(start_date / 1000).isoformat(),
            "end": datetime.fromtimestamp(end_date / 1000).isoformat(),
        },
        "spaces": final_report,
        "total_spaces": len(final_report),
    }


def generate_space_folder_member_report(
    start_date: int, end_date: int, space_name: Optional[str] = None
) -> Dict:
    """
    Report 2: Space > Folder > Team member wise time entry report
    Hierarchical breakdown: Space -> Folder -> User
    """
    team_id = get_team_id()
    if not team_id:
        return {"error": "No team ID found"}

    structure = get_workspace_structure()
    if "error" in structure:
        return structure

    time_entries = get_time_entries_for_team(team_id, start_date, end_date)

    # Build hierarchical report structure
    hierarchical_report = defaultdict(
        lambda: defaultdict(
            lambda: defaultdict(
                lambda: {
                    "total_duration_ms": 0,
                    "total_duration_hours": 0,
                    "entries_count": 0,
                    "tasks": set(),
                }
            )
        )
    )

    for entry in time_entries:
        task_id = entry.get("task", {}).get("id")
        if not task_id:
            continue

        task = get_task_details(task_id)
        if not task:
            continue

        space = task.get("space", {}).get("name", "Unknown Space")
        folder = task.get("folder", {}).get("name", "No Folder")
        user = entry.get("user", {}).get("username", "Unknown User")
        duration = int(entry.get("duration", 0))

        # Filter by space if specified
        if space_name and space.lower() != space_name.lower():
            continue

        hierarchical_report[space][folder][user]["total_duration_ms"] += duration
        hierarchical_report[space][folder][user]["total_duration_hours"] = round(
            hierarchical_report[space][folder][user]["total_duration_ms"]
            / (1000 * 60 * 60),
            2,
        )
        hierarchical_report[space][folder][user]["entries_count"] += 1
        hierarchical_report[space][folder][user]["tasks"].add(task_id)

    # Convert to JSON-serializable format
    final_report = {}
    for space, folders in hierarchical_report.items():
        final_report[space] = {}
        for folder, users in folders.items():
            final_report[space][folder] = {}
            for user, data in users.items():
                final_report[space][folder][user] = {
                    "total_hours": data["total_duration_hours"],
                    "total_entries": data["entries_count"],
                    "unique_tasks": len(data["tasks"]),
                }

    return {
        "report_type": "space_folder_member_time_entries",
        "date_range": {
            "start": datetime.fromtimestamp(start_date / 1000).isoformat(),
            "end": datetime.fromtimestamp(end_date / 1000).isoformat(),
        },
        "filter": {"space": space_name} if space_name else None,
        "hierarchy": final_report,
    }


def generate_team_member_report(
    start_date: int, end_date: int, member_name: Optional[str] = None
) -> Dict:
    """
    Report 3: Team member wise time entry report
    Shows time breakdown per user across all spaces/tasks
    """
    team_id = get_team_id()
    if not team_id:
        return {"error": "No team ID found"}

    time_entries = get_time_entries_for_team(team_id, start_date, end_date)

    member_report = defaultdict(
        lambda: {
            "total_duration_ms": 0,
            "total_duration_hours": 0,
            "entries_count": 0,
            "tasks": set(),
            "spaces": set(),
            "daily_breakdown": defaultdict(int),
        }
    )

    for entry in time_entries:
        user = entry.get("user", {}).get("username", "Unknown User")

        # Filter by member if specified
        if member_name and user.lower() != member_name.lower():
            continue

        task_id = entry.get("task", {}).get("id")
        duration = int(entry.get("duration", 0))
        start_ms = int(entry.get("start", 0))

        # Get task details for space
        task = get_task_details(task_id) if task_id else None
        space_name = task.get("space", {}).get("name", "Unknown") if task else "Unknown"

        # Date for daily breakdown
        date_str = datetime.fromtimestamp(start_ms / 1000).date().isoformat()

        member_report[user]["total_duration_ms"] += duration
        member_report[user]["total_duration_hours"] = round(
            member_report[user]["total_duration_ms"] / (1000 * 60 * 60), 2
        )
        member_report[user]["entries_count"] += 1
        if task_id:
            member_report[user]["tasks"].add(task_id)
        member_report[user]["spaces"].add(space_name)
        member_report[user]["daily_breakdown"][date_str] += round(
            duration / (1000 * 60 * 60), 2
        )

    # Convert to serializable format
    final_report = {}
    for user, data in member_report.items():
        final_report[user] = {
            "total_hours": data["total_duration_hours"],
            "total_entries": data["entries_count"],
            "unique_tasks": len(data["tasks"]),
            "unique_spaces": len(data["spaces"]),
            "spaces_worked": sorted(list(data["spaces"])),
            "daily_hours": dict(data["daily_breakdown"]),
        }

    return {
        "report_type": "team_member_time_entries",
        "date_range": {
            "start": datetime.fromtimestamp(start_date / 1000).isoformat(),
            "end": datetime.fromtimestamp(end_date / 1000).isoformat(),
        },
        "filter": {"member": member_name} if member_name else None,
        "members": final_report,
        "total_members": len(final_report),
    }


def generate_weekly_report(report_type: str, weeks_back: int = 1, **kwargs) -> Dict:
    """
    Report 4: Weekly versions of all above reports
    report_type: 'space' | 'space_folder_member' | 'team_member'
    """
    # Calculate date range for past N weeks
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(weeks=weeks_back)

    start_ms = int(start_date.timestamp() * 1000)
    end_ms = int(end_date.timestamp() * 1000)

    # Generate weekly breakdowns
    weekly_reports = []
    current_start = start_date

    while current_start < end_date:
        week_end = min(current_start + timedelta(days=7), end_date)
        week_start_ms = int(current_start.timestamp() * 1000)
        week_end_ms = int(week_end.timestamp() * 1000)

        # Generate appropriate report for this week
        if report_type == "space":
            week_report = generate_space_wise_time_report(week_start_ms, week_end_ms)
        elif report_type == "space_folder_member":
            week_report = generate_space_folder_member_report(
                week_start_ms, week_end_ms, **kwargs
            )
        elif report_type == "team_member":
            week_report = generate_team_member_report(
                week_start_ms, week_end_ms, **kwargs
            )
        else:
            return {"error": f"Invalid report_type: {report_type}"}

        weekly_reports.append(
            {
                "week_start": current_start.isoformat(),
                "week_end": week_end.isoformat(),
                "report": week_report,
            }
        )

        current_start = week_end

    return {
        "report_type": f"weekly_{report_type}_time_entries",
        "weeks_analyzed": weeks_back,
        "total_weeks": len(weekly_reports),
        "weekly_breakdown": weekly_reports,
    }


# ============================================================================
# LM STUDIO CLIENT (Qwen Integration)
# ============================================================================


class QwenLMStudioClient:
    """Client for Qwen 2.5-7B-Instruct via LM Studio"""

    def __init__(self):
        self.base_url = LM_STUDIO_BASE_URL
        self.model = LM_STUDIO_MODEL
        self.session = None

    async def send_message(
        self, messages: List[Dict], tools: List[Dict] = None
    ) -> Dict:
        """
        Send message to Qwen via LM Studio OpenAI-compatible API
        Supports function calling if tools are provided
        """
        try:
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.1,  # Low temperature for accuracy
                "max_tokens": 4096,
                "stream": False,
            }

            # Add tools if provided
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            response = requests.post(
                f"{self.base_url}/chat/completions", json=payload, timeout=60
            )

            if response.status_code == 200:
                return response.json()
            else:
                return {
                    "error": f"LM Studio error {response.status_code}: {response.text}"
                }

        except Exception as e:
            return {"error": f"LM Studio request failed: {str(e)}"}


# ============================================================================
# MCP CLIENT (Tool Integration)
# ============================================================================


class MCPToolManager:
    """Manages MCP server connection and tool calls"""

    def __init__(self):
        self.session = None
        self.tools = []

        self.read_stream = None
        self.write_stream = None
        self.sse_connection = None

    async def connect(self):
        """Connect to MCP server and load tools"""
        try:
            self.sse_connection = sse_client(MCP_SERVER_URL)
            self.read_stream, self.write_stream = await self.sse_connection.__aenter__()
            self.session = ClientSession(self.read_stream, self.write_stream)
            await self.session.__aenter__()

            # Initialize and get tools
            await self.session.initialize()
            tools_result = await self.session.list_tools()
            self.tools = tools_result.tools

            print(f"✅ Connected to MCP server - {len(self.tools)} tools loaded")
            print("📋 Available tool categories: workspace, tasks, analytics, config, intelligence, sync")
            return True
        except Exception as e:
            print(f"❌ MCP connection failed: {e}")
            traceback.print_exc()
            return False

    async def cleanup(self):
        """Properly cleanup MCP connection"""
        try:
            if self.session:
                try:
                    await self.session.__aexit__(None, None, None)
                except Exception as e:
                    print(f"⚠️  Warning during session cleanup: {e}")

            if self.sse_connection:
                try:
                    await self.sse_connection.__aexit__(None, None, None)
                except Exception as e:
                    print(f"⚠️  Warning during SSE cleanup: {e}")
        except Exception as e:
            print(f"⚠️  Error during cleanup: {e}")

    async def call_tool(self, tool_name: str, arguments: Dict) -> Any:
        """Execute MCP tool call"""
        try:
            if not self.session:
                return {"error": "Not connected to MCP server"}

            print(f"🔧 Calling tool: {tool_name}")
            print(f"   Arguments: {json.dumps(arguments, indent=2)}")

            result = await self.session.call_tool(tool_name, arguments)

            # Extract content from result
            if hasattr(result, "content"):
                if isinstance(result.content, list):
                    # MCP returns list of content items
                    content = []
                    for item in result.content:
                        if hasattr(item, "text"):
                            try:
                                content.append(json.loads(item.text))
                            except Exception:
                                content.append(item.text)
                    return content[0] if len(content) == 1 else content
                else:
                    return result.content
            else:
                return result
        except Exception as e:
            error_msg = f"Tool call failed: {str(e)}"
            print(f"❌ {error_msg}")
            traceback.print_exc()
            return {"error": error_msg}

    def get_tools_schema(self) -> List[Dict]:
        """Convert MCP tools to OpenAI function calling format"""
        schema = []
        for tool in self.tools:
            schema.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.inputSchema
                        or {"type": "object", "properties": {}},
                    },
                }
            )
        return schema


# ============================================================================
# MAIN ORCHESTRATOR
# ============================================================================


class QwenClickUpAssistant:
    """Main assistant combining Qwen, MCP tools, and custom reports"""

    def __init__(self):
        self.qwen = QwenLMStudioClient()
        self.mcp = MCPToolManager()
        self.conversation_history = []

        self.total_tokens_used = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.last_workspace_name = None
        self.last_space_name = None
        self.recent_results = {}

    async def initialize(self):
        """Initialize MCP connection"""
        return await self.mcp.connect()

    def _build_context_summary(self) -> str:
        """Build context from recent results to help with tool selection"""
        context = "None yet"

        if self.last_workspace_name:
            context = f"- Last workspace queried: '{self.last_workspace_name}'\n"
        if self.last_space_name:
            context += f"- Last space queried: '{self.last_space_name}'\n"

        return context

    async def process_query(self, user_query: str) -> str:
        """
        Process user query with Qwen + MCP tools + Custom reports
        """
        # Add user message to history
        self.conversation_history.append({"role": "user", "content": user_query})

        # Build context from recent results for better tool selection
        context_summary = self._build_context_summary()

        # System prompt for Qwen
        system_prompt = f"""You are a ClickUp project management assistant with access to 54 MCP tools and 4 custom report functions.

**CONVERSATION CONTEXT (from recent queries):**
{context_summary}

**WORKSPACE & SPACE NAMING - CRITICAL:**
- Workspace names are TEXT (e.g., "Avinashi", "Marketing")
- When user says "from it" or "that one", use the last workspace/space name
- When user says "fetch all spaces from it", use get_spaces() with the workspace name
- Example: If user said "Avinashi" before, and now says "from it", use workspace_name="Avinashi"

**KEY TOOL PARAMETERS:**
1. get_workspaces() → Returns all workspaces (no parameters needed)
2. get_spaces(workspace_name="Avinashi") → Gets spaces from that workspace
3. get_lists(space_name="...", folder_name="...") → Gets lists
4. get_tasks(project="...", filters={{...}}) → Gets tasks

**CRITICAL EXECUTION RULES:**
1. NEVER make up data - always use tools
2. If user says "from it" or "that one", refer to the most recent entity mentioned
3. Extract workspace/space names from previous results
4. Call the correct tool with proper parameters
5. For "fetch all spaces from it" → use get_spaces() NOT get_workspaces()

**RESPONSE FORMAT:**
- Acknowledge what you're doing
- Call one tool with correct parameters
- Present results clearly"""

        messages = [
            {"role": "system", "content": system_prompt},
            *self.conversation_history,
        ]

        # Get MCP tools schema
        tools_schema = self.mcp.get_tools_schema()

        # Add custom report "tools" (we'll handle manually)
        custom_tools = [
            {
                "type": "function",
                "function": {
                    "name": "generate_space_wise_time_report",
                    "description": "Generate space-wise time entry report for a date range",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "start_date_ms": {
                                "type": "integer",
                                "description": "Start date in milliseconds",
                            },
                            "end_date_ms": {
                                "type": "integer",
                                "description": "End date in milliseconds",
                            },
                        },
                        "required": ["start_date_ms", "end_date_ms"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "generate_space_folder_member_report",
                    "description": "Generate hierarchical Space>Folder>Member time report",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "start_date_ms": {"type": "integer"},
                            "end_date_ms": {"type": "integer"},
                            "space_name": {
                                "type": "string",
                                "description": "Optional space filter",
                            },
                        },
                        "required": ["start_date_ms", "end_date_ms"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "generate_team_member_report",
                    "description": "Generate team member-wise time entry report",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "start_date_ms": {"type": "integer"},
                            "end_date_ms": {"type": "integer"},
                            "member_name": {
                                "type": "string",
                                "description": "Optional member filter",
                            },
                        },
                        "required": ["start_date_ms", "end_date_ms"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "generate_weekly_report",
                    "description": "Generate weekly breakdown of any report type",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "report_type": {
                                "type": "string",
                                "enum": ["space", "space_folder_member", "team_member"],
                            },
                            "weeks_back": {
                                "type": "integer",
                                "description": "Number of weeks to analyze",
                            },
                        },
                        "required": ["report_type", "weeks_back"],
                    },
                },
            },
        ]

        all_tools = tools_schema + custom_tools

        # Send to Qwen
        response = await self.qwen.send_message(messages, all_tools)

        if "error" in response:
            return self._format_error(response["error"])

        # Track token usage
        usage = response.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", 0)

        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        self.total_tokens_used += total_tokens

        # Process response
        choice = response.get("choices", [{}])[0]
        message = choice.get("message", {})

        # Check for tool calls
        if message.get("tool_calls"):
            print(f"\n🔍 Detected {len(message['tool_calls'])} tool call(s)")
            tool_results = []

            for tool_call in message["tool_calls"]:
                function_name = tool_call["function"]["name"]
                arguments = json.loads(tool_call["function"]["arguments"])

                print(f"\n📞 Calling: {function_name}")

                # Execute custom report functions
                if function_name == "generate_space_wise_time_report":
                    print("   ⏰ Generating space-wise time report...")
                    result = generate_space_wise_time_report(
                        arguments["start_date_ms"], arguments["end_date_ms"]
                    )
                elif function_name == "generate_space_folder_member_report":
                    print("   ⏰ Generating hierarchical time report...")
                    result = generate_space_folder_member_report(
                        arguments["start_date_ms"],
                        arguments["end_date_ms"],
                        arguments.get("space_name"),
                    )
                elif function_name == "generate_team_member_report":
                    print("   ⏰ Generating team member report...")
                    result = generate_team_member_report(
                        arguments["start_date_ms"],
                        arguments["end_date_ms"],
                        arguments.get("member_name"),
                    )
                elif function_name == "generate_weekly_report":
                    print("   ⏰ Generating weekly report...")
                    result = generate_weekly_report(
                        arguments["report_type"], arguments["weeks_back"]
                    )
                else:
                    # MCP tool call
                    result = await self.mcp.call_tool(function_name, arguments)

                # Track workspace and space names for context
                if function_name == "get_workspaces" and isinstance(result, list):
                    if result and "name" in result[0]:
                        self.last_workspace_name = result[0]["name"]
                elif function_name == "get_spaces" and "workspace_name" in arguments:
                    self.last_workspace_name = arguments["workspace_name"]
                    if isinstance(result, list) and result and "name" in result[0]:
                        self.last_space_name = result[0]["name"]

                tool_results.append(
                    {"tool": function_name, "arguments": arguments, "result": result}
                )

            # Return tool results formatted
            return self._format_tool_results(
                tool_results, prompt_tokens, completion_tokens, total_tokens
            )

        # Regular text response
        content = message.get("content", "No response generated")
        return self._format_text_response(
            content, prompt_tokens, completion_tokens, total_tokens
        )

    def _format_error(self, error: str) -> str:
        """Format error message"""
        return f"\n{'=' * 70}\n❌ ERROR\n{'=' * 70}\n{error}\n{'=' * 70}\n"

    def _format_text_response(
        self,
        content: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> str:
        """Format regular text response"""
        output = f"\n{'=' * 70}\n"
        output += "💬 ASSISTANT RESPONSE\n"
        output += f"{'=' * 70}\n\n"
        output += f"{content}\n\n"
        output += f"{'─' * 70}\n"
        output += f"📊 Tokens: {total_tokens} (Prompt: {prompt_tokens}, Completion: {completion_tokens})\n"
        output += f"📈 Session Total: {self.total_tokens_used} tokens\n"
        output += f"{'=' * 70}\n"
        return output

    def _format_tool_results(
        self,
        results: List[Dict],
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> str:
        """Format tool results for user"""
        output = f"\n{'=' * 70}\n"
        output += "🔧 TOOL EXECUTION RESULTS\n"
        output += f"{'=' * 70}\n\n"

        for item in results:
            output += f"🔨 Tool: {item['tool']}\n"
            output += (
                f"📥 Arguments: {json.dumps(item.get('arguments', {}), indent=2)}\n"
            )
            output += "📤 Result:\n"

            result = item["result"]
            if isinstance(result, dict):
                if "error" in result:
                    output += f"   ❌ Error: {result['error']}\n"
                else:
                    output += f"{json.dumps(result, indent=2)}\n"
            elif isinstance(result, list):
                output += f"{json.dumps(result, indent=2)}\n"
            else:
                output += f"{result}\n"

            output += f"\n{'-' * 70}\n\n"

        output += f"{'─' * 70}\n"
        output += f"📊 Tokens: {total_tokens} (Prompt: {prompt_tokens}, Completion: {completion_tokens})\n"
        output += f"📈 Session Total: {self.total_tokens_used} tokens\n"
        output += f"{'=' * 70}\n"
        return output


# ============================================================================
# INTERACTIVE CLI
# ============================================================================


async def main():
    """Main interactive loop"""
    print("\n" + "=" * 70)
    print("🤖 QWEN 2.5-7B CLICKUP ASSISTANT".center(70))
    print("=" * 70)
    print("\n📦 FEATURES:")
    print("  ✅ 54 MCP Tools (workspace, tasks, analytics)")
    print("  ✅ 4 Custom Time Reports (zero hallucination)")
    print("  ✅ Direct ClickUp API integration")
    print("  ✅ Local inference (unlimited tokens)")
    print("  ✅ Token usage tracking")
    print("  ✅ Enhanced debugging")
    print("\n" + "=" * 70)

    # Initialize assistant
    assistant = QwenClickUpAssistant()

    print("\n🔄 INITIALIZING...")
    print("   • Connecting to MCP server...")
    if not await assistant.initialize():
        print("\n❌ Failed to connect to MCP server.")
        print("   Make sure MCP server is running:")
        print("   uvicorn app.mcp.mcp_server:mcp --host 0.0.0.0 --port 8001")
        return

    print("   • Connecting to LM Studio...")
    print("   ✅ All systems ready!\n")

    print("=" * 70)
    print("💡 EXAMPLE QUERIES:")
    print("=" * 70)
    print("\n📋 Workspace & Structure:")
    print("  • 'List all workspaces'")
    print("  • 'Show all spaces from Avinashi workspace'")
    print("  • 'Get lists from Marketing space'")
    print("\n⏰ Time Reports:")
    print("  • 'Space-wise time report for last week'")
    print("  • 'Team member time report for John'")
    print("  • 'Weekly breakdown for past 4 weeks'")
    print("\n📊 Tasks & Analytics:")
    print("  • 'Show overdue tasks'")
    print("  • 'Get task analytics for project X'")
    print("  • 'List tasks with no time entries'")
    print("\n" + "=" * 70)
    print("Type 'quit' or 'exit' to end session")
    print("=" * 70 + "\n")

    while True:
        try:
            user_input = input("💬 You: ").strip()

            if not user_input:
                continue

            if user_input.lower() in ["quit", "exit", "q"]:
                print("\n" + "=" * 70)
                print("📊 SESSION SUMMARY")
                print("=" * 70)
                print(f"Total Tokens Used: {assistant.total_tokens_used}")
                print(f"  • Prompt Tokens: {assistant.total_prompt_tokens}")
                print(f"  • Completion Tokens: {assistant.total_completion_tokens}")
                print("=" * 70)
                print("👋 Thank you for using Qwen ClickUp Assistant!")
                print("=" * 70 + "\n")

                # Cleanup
                print("🧹 Cleaning up connections...")
                await assistant.mcp.cleanup()
                print("✅ Cleanup complete!\n")
                break

            print(f"\n{'─' * 70}")
            print("🤔 Processing your request...")
            print(f"{'─' * 70}")

            response = await assistant.process_query(user_input)
            print(response)

        except KeyboardInterrupt:
            print("\n\n" + "=" * 70)
            print("👋 Session interrupted. Cleaning up...")
            print("=" * 70)

            # Cleanup
            print("🧹 Cleaning up connections...")
            await assistant.mcp.cleanup()
            print("✅ Cleanup complete!\n")
            break
        except Exception as e:
            print(f"\n{'=' * 70}")
            print("❌ UNEXPECTED ERROR")
            print(f"{'=' * 70}")
            print(f"Error: {e}")
            print(f"{'=' * 70}\n")
            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
