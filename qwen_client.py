"""
ClickUp MCP Server - Qwen 2.5-7B-Instruct Client (IMPROVED v2.0)
=================================================================
Enhanced client with:
- Fixed conversation flow (no delayed responses)
- Smart name-to-ID resolution
- 4 dedicated time entry report tools
- Token-optimized prompts (50K limit)
- Better error recovery
- Multi-step task execution

Author: ClickUp MCP Team
Version: 2.0 (Production-Ready)
"""

from mcp import ClientSession
from mcp.client.sse import sse_client
from dotenv import load_dotenv
import asyncio
import json
import os
import warnings
import traceback
import requests
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta, timezone
from collections import defaultdict

load_dotenv()

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

# ============================================================================
# CONFIGURATION
# ============================================================================

LM_STUDIO_BASE_URL = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
LM_STUDIO_MODEL = os.getenv("LM_STUDIO_MODEL", "qwen2.5-7b-instruct")

CLICKUP_API_TOKEN = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID")
CLICKUP_BASE_URL = "https://api.clickup.com/api/v2"

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/sse")

# ============================================================================
# CLICKUP API HELPERS
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


# ============================================================================
# TIME ENTRY REPORT GENERATORS
# ============================================================================


def generate_space_wise_time_report(
    start_date: Optional[int] = None, end_date: Optional[int] = None
) -> Dict:
    """
    Report 1: Space-wise time entry report
    Groups all time entries by Space
    If dates not provided, fetches last 90 days
    """
    # Default to last 90 days if not provided
    if end_date is None:
        end_date = int(datetime.now(timezone.utc).timestamp() * 1000)
    if start_date is None:
        start_date = int(
            (datetime.now(timezone.utc) - timedelta(days=90)).timestamp() * 1000
        )

    team_id = get_team_id()
    if not team_id:
        return {"error": "No team ID found"}

    # Get workspace structure for mapping
    spaces_data, error = _api_call("GET", f"/team/{team_id}/space")
    if error:
        return {"error": error}

    # Build space ID -> name mapping
    space_map = {s["id"]: s["name"] for s in spaces_data.get("spaces", [])}

    # Get all time entries
    params = {"start_date": start_date, "end_date": end_date}
    time_data, error = _api_call("GET", f"/team/{team_id}/time_entries", params=params)

    if error:
        return {"error": error}

    time_entries = time_data.get("data", [])

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
        task_data, _ = _api_call("GET", f"/task/{task_id}")
        if not task_data:
            continue

        space_id = task_data.get("space", {}).get("id")
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
    start_date: Optional[int] = None,
    end_date: Optional[int] = None,
    space_name: Optional[str] = None,
) -> Dict:
    """
    Report 2: Space > Folder > Team member wise time entry report
    Hierarchical breakdown: Space -> Folder -> User
    If dates not provided, fetches last 90 days
    """
    # Default to last 90 days if not provided
    if end_date is None:
        end_date = int(datetime.now(timezone.utc).timestamp() * 1000)
    if start_date is None:
        start_date = int(
            (datetime.now(timezone.utc) - timedelta(days=90)).timestamp() * 1000
        )

    team_id = get_team_id()
    if not team_id:
        return {"error": "No team ID found"}

    # Get time entries
    params = {"start_date": start_date, "end_date": end_date}
    time_data, error = _api_call("GET", f"/team/{team_id}/time_entries", params=params)

    if error:
        return {"error": error}

    time_entries = time_data.get("data", [])

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

        task_data, _ = _api_call("GET", f"/task/{task_id}")
        if not task_data:
            continue

        space = task_data.get("space", {}).get("name", "Unknown Space")
        folder = task_data.get("folder", {}).get("name", "No Folder")
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
    start_date: Optional[int] = None,
    end_date: Optional[int] = None,
    member_name: Optional[str] = None,
) -> Dict:
    """
    Report 3: Team member wise time entry report
    Shows time breakdown per user across all spaces/tasks
    If dates not provided, fetches last 90 days
    """
    # Default to last 90 days if not provided
    if end_date is None:
        end_date = int(datetime.now(timezone.utc).timestamp() * 1000)
    if start_date is None:
        start_date = int(
            (datetime.now(timezone.utc) - timedelta(days=90)).timestamp() * 1000
        )

    team_id = get_team_id()
    if not team_id:
        return {"error": "No team ID found"}

    # Get time entries
    params = {"start_date": start_date, "end_date": end_date}
    time_data, error = _api_call("GET", f"/team/{team_id}/time_entries", params=params)

    if error:
        return {"error": error}

    time_entries = time_data.get("data", [])

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
        task_data, _ = _api_call("GET", f"/task/{task_id}") if task_id else (None, None)
        space_name = (
            task_data.get("space", {}).get("name", "Unknown")
            if task_data
            else "Unknown"
        )

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
# LM STUDIO CLIENT (Qwen Integration with Improved Flow)
# ============================================================================


class QwenLMStudioClient:
    """Client for Qwen 2.5-7B-Instruct via LM Studio"""

    def __init__(self):
        self.base_url = LM_STUDIO_BASE_URL
        self.model = LM_STUDIO_MODEL
        self.conversation_history = []
        self.system_prompt = self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        """Build optimized system prompt (token-efficient)"""
        return """You are a ClickUp assistant with 58 tools (54 MCP + 4 time reports).

**🚫 ANTI-HALLUCINATION RULES (CRITICAL):**
1. ONLY use function names that exist in the tool schema
2. NEVER invent or make up function names (e.g., NO "get_listless_lists")
3. If unsure of exact function name, search for SIMILAR words:
   - Want "folderless"? Look for "folderless" NOT "listless"
   - Want "spaces"? Use "get_spaces" NOT "list_spaces"
4. If no exact match, use the CLOSEST matching function from schema
5. When tool fails, READ the error and try EXISTING alternatives

**FUNCTION NAME MATCHING:**
- User says "lists not in folders" → Use "get_folderless_lists" (exists)
- User says "fetch projects" → Check if "get_projects" exists, else use "get_spaces"
- ALWAYS verify function name exists before calling

**TIME REPORTS (DATES NOW OPTIONAL):**
1. generate_space_wise_time_report(start_date_ms?, end_date_ms?)
2. generate_space_folder_member_report(start_date_ms?, end_date_ms?, space_name?)
3. generate_team_member_report(start_date_ms?, end_date_ms?, member_name?)
4. generate_weekly_report(report_type, weeks_back, **filters)

**DATE HANDLING:**
- If user says "team member report for X" without dates → Call with NO date params
- If user says "report for January" → Calculate dates and pass them
- Default fetches last 90 days if no dates provided

**NAME vs ID:**
- Most tools accept names OR IDs (e.g., workspace_id="Avinashi" works)
- Space is NOT a workspace; workspace contains spaces
- To get folders/lists from space, use space_id (numeric)

**COMMON PATTERNS:**
1. "List workspaces" → get_workspaces()
2. "Spaces in Avinashi" → get_spaces(workspace_id="Avinashi")
3. "Folders in X space" → get_folders(space_id=SPACE_ID)
4. "Lists not in folders" → get_folderless_lists(space_id=SPACE_ID)
5. "Time report for project" → generate_team_member_report() [no dates needed]

**ERROR RECOVERY:**
If tool fails:
1. Read error message carefully
2. Check if using wrong parameter (name vs ID)
3. Try related function from schema (e.g., get_folderless_lists not get_listless_lists)
4. Explain issue to user

Be concise, accurate, and NEVER hallucinate function names."""

    def _prepare_messages(self, user_message: str) -> List[Dict]:
        """Prepare messages for API call"""
        messages = [{"role": "system", "content": self.system_prompt}]

        # Add conversation history (last 10 exchanges to manage tokens)
        history_limit = 10
        recent_history = (
            self.conversation_history[-history_limit:]
            if len(self.conversation_history) > history_limit
            else self.conversation_history
        )
        messages.extend(recent_history)

        # Add current user message
        messages.append({"role": "user", "content": user_message})

        return messages

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
                "temperature": 0.1,
                "max_tokens": 8192,
                "stream": False,
            }

            # Add tools if provided
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            response = requests.post(
                f"{self.base_url}/chat/completions", json=payload, timeout=120
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
            return True
        except Exception as e:
            print(f"❌ MCP connection failed: {e}")
            traceback.print_exc()
            return False

    async def cleanup(self):
        """Properly cleanup MCP connection"""
        try:
            if self.session:
                await self.session.__aexit__(None, None, None)
            if self.sse_connection:
                await self.sse_connection.__aexit__(None, None, None)
        except Exception as e:
            print(f"⚠️  Error during cleanup: {e}")

    async def call_tool(self, tool_name: str, arguments: Dict) -> Any:
        """Execute MCP tool call"""
        try:
            if not self.session:
                return {"error": "Not connected to MCP server"}

            print(f"🔧 Calling tool: {tool_name}")
            print(f"   Arguments: {json.dumps(arguments, indent=2)[:200]}...")

            result = await self.session.call_tool(tool_name, arguments)

            # Extract content from result
            if hasattr(result, "content"):
                if isinstance(result.content, list):
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
# MAIN ORCHESTRATOR (IMPROVED)
# ============================================================================


class QwenClickUpAssistant:
    """Main assistant combining Qwen, MCP tools, and custom reports"""

    def __init__(self):
        self.qwen = QwenLMStudioClient()
        self.mcp = MCPToolManager()
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_tokens = 0

    async def initialize(self):
        """Initialize MCP connection"""
        return await self.mcp.connect()

    async def process_message(self, user_message: str) -> str:
        """
        Process user message with IMPROVED conversation flow
        """
        # Prepare messages
        messages = self.qwen._prepare_messages(user_message)

        # Get all available tools (MCP + custom)
        all_tools = self._get_all_tools()

        # Send to Qwen
        response = await self.qwen.send_message(messages, all_tools)

        if "error" in response:
            return f"❌ Error: {response['error']}"

        # Track tokens
        usage = response.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", 0)

        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        self.total_tokens += total_tokens

        # Process response
        choice = response.get("choices", [{}])[0]
        message = choice.get("message", {})

        # Update conversation history with user message
        self.qwen.conversation_history.append({"role": "user", "content": user_message})

        # Check for tool calls
        tool_calls = message.get("tool_calls", [])

        if tool_calls:
            print(f"\n🔍 Detected {len(tool_calls)} tool call(s)")

            # Execute all tool calls
            tool_results = []
            for tc in tool_calls:
                function_name = tc["function"]["name"]
                arguments = json.loads(tc["function"]["arguments"])

                print(f"\n📞 Calling: {function_name}")

                # Check if it's a custom report function
                if function_name == "generate_space_wise_time_report":
                    result = generate_space_wise_time_report(
                        arguments.get("start_date_ms"), arguments.get("end_date_ms")
                    )
                elif function_name == "generate_space_folder_member_report":
                    result = generate_space_folder_member_report(
                        arguments.get("start_date_ms"),
                        arguments.get("end_date_ms"),
                        arguments.get("space_name"),
                    )
                elif function_name == "generate_team_member_report":
                    result = generate_team_member_report(
                        arguments.get("start_date_ms"),
                        arguments.get("end_date_ms"),
                        arguments.get("member_name"),
                    )
                elif function_name == "generate_weekly_report":
                    result = generate_weekly_report(
                        arguments["report_type"],
                        arguments["weeks_back"],
                        **arguments.get("filters", {}),
                    )
                else:
                    # MCP tool call
                    result = await self.mcp.call_tool(function_name, arguments)

                tool_results.append(
                    {"id": tc["id"], "name": function_name, "result": result}
                )

            # Add tool calls to history
            self.qwen.conversation_history.append(
                {"role": "assistant", "tool_calls": tool_calls}
            )

            # Add tool results to history
            for tr in tool_results:
                self.qwen.conversation_history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tr["id"],
                        "content": json.dumps(tr["result"], default=str),
                    }
                )

            # Get final response from Qwen with tool results
            final_messages = [{"role": "system", "content": self.qwen.system_prompt}]
            final_messages.extend(self.qwen.conversation_history)

            final_response = await self.qwen.send_message(final_messages, all_tools)

            if "error" in final_response:
                return self._format_tool_results(tool_results)

            final_choice = final_response.get("choices", [{}])[0]
            final_message = final_choice.get("message", {})
            final_text = final_message.get("content", "")

            # Track tokens for final response
            final_usage = final_response.get("usage", {})
            final_prompt_tokens = final_usage.get("prompt_tokens", 0)
            final_completion_tokens = final_usage.get("completion_tokens", 0)
            final_total_tokens = final_usage.get("total_tokens", 0)

            self.total_prompt_tokens += final_prompt_tokens
            self.total_completion_tokens += final_completion_tokens
            self.total_tokens += final_total_tokens

            # Add final response to history
            self.qwen.conversation_history.append(
                {"role": "assistant", "content": final_text}
            )

            # Display tokens
            token_display = f"\n{'─' * 70}\n📊 Tokens: {final_total_tokens} (Prompt: {final_prompt_tokens}, Completion: {final_completion_tokens})\n📈 Session Total: {self.total_tokens} tokens\n{'=' * 70}"

            return (
                final_text or self._format_tool_results(tool_results)
            ) + token_display

        else:
            # No tool calls - direct response
            response_text = message.get("content", "⚠ Received empty response")

            # Add to history
            self.qwen.conversation_history.append(
                {"role": "assistant", "content": response_text}
            )

            return response_text

    def _get_all_tools(self) -> List[Dict]:
        """Get all tools (MCP + custom reports)"""
        tools = self.mcp.get_tools_schema()

        # Add custom report tools
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
                                "description": "Start date in milliseconds (optional, defaults to 90 days ago)",
                            },
                            "end_date_ms": {
                                "type": "integer",
                                "description": "End date in milliseconds (optional, defaults to now)",
                            },
                        },
                        "required": [],
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
                            "start_date_ms": {
                                "type": "integer",
                                "description": "Start date in milliseconds (optional, defaults to 90 days ago)",
                            },
                            "end_date_ms": {
                                "type": "integer",
                                "description": "End date in milliseconds (optional, defaults to now)",
                            },
                            "space_name": {
                                "type": "string",
                                "description": "Optional space filter",
                            },
                        },
                        "required": [],
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
                            "start_date_ms": {
                                "type": "integer",
                                "description": "Start date in milliseconds (optional, defaults to 90 days ago)",
                            },
                            "end_date_ms": {
                                "type": "integer",
                                "description": "End date in milliseconds (optional, defaults to now)",
                            },
                            "member_name": {
                                "type": "string",
                                "description": "Optional member filter",
                            },
                        },
                        "required": [],
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
                            "filters": {
                                "type": "object",
                                "description": "Additional filters (space_name, member_name)",
                            },
                        },
                        "required": ["report_type", "weeks_back"],
                    },
                },
            },
        ]

        tools.extend(custom_tools)
        return tools

    def _format_tool_results(self, results: List[Dict]) -> str:
        """Format tool results for display"""
        output = "\n📊 **Results:**\n\n"
        for item in results:
            output += f"**{item['name']}:**\n"
            output += f"```json\n{json.dumps(item['result'], indent=2, default=str)[:1000]}...\n```\n\n"
        return output


# ============================================================================
# INTERACTIVE CLI
# ============================================================================


async def main():
    """Main interactive loop"""
    print("\n" + "=" * 70)
    print("🤖 QWEN 2.5-7B CLICKUP ASSISTANT (IMPROVED v2.0)".center(70))
    print("=" * 70)
    print("\n📦 FEATURES:")
    print("  ✅ 54 MCP Tools + 4 Time Reports")
    print("  ✅ Smart name resolution")
    print("  ✅ Fixed conversation flow")
    print("  ✅ Better error recovery")
    print("  ✅ Token-optimized (50K limit)")
    print("\n" + "=" * 70)

    # Initialize assistant
    assistant = QwenClickUpAssistant()

    print("\n🔄 INITIALIZING...")
    if not await assistant.initialize():
        print("\n❌ Failed to connect to MCP server.")
        return

    print("   ✅ All systems ready!\n")

    print("=" * 70)
    print("💡 EXAMPLE QUERIES:")
    print("=" * 70)
    print("\n📋 Workspace:")
    print("  • 'List all workspaces'")
    print("  • 'Show spaces in Avinashi'")
    print("\n⏰ Time Reports:")
    print("  • 'Space-wise time report for January 2026'")
    print("  • 'Team member report for Alice last week'")
    print("  • 'Weekly breakdown for past 4 weeks'")
    print("\n" + "=" * 70)
    print("Type 'quit' to exit")
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
                print(f"Total Tokens Used: {assistant.total_tokens}")
                print(f"  • Prompt Tokens: {assistant.total_prompt_tokens}")
                print(f"  • Completion Tokens: {assistant.total_completion_tokens}")
                print("=" * 70)
                print("👋 Thank you for using Qwen ClickUp Assistant!")
                print("=" * 70)
                print("\n🧹 Cleaning up connections...")
                await assistant.mcp.cleanup()
                print("✅ Cleanup complete!\n")
                break

            print(f"\n{'─' * 70}")
            response = await assistant.process_message(user_input)
            print(f"\n🤖 Assistant:\n{response}")
            print(f"{'─' * 70}\n")

        except KeyboardInterrupt:
            print("\n\n👋 Session interrupted.")
            await assistant.mcp.cleanup()
            break
        except Exception as e:
            print(f"\n❌ ERROR: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
