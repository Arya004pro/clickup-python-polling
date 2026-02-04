"""
SmolLM3-3B + ClickUp MCP Integration
Token-Optimized Client for SmolLM3-3B (Absolute Heresy Q4_K_M)

Features:
- Ultra-concise system prompts (token budget: 200 tokens)
- MCP tool integration with error recovery
- Name-to-ID resolution
- 4 time entry report generation
- Tool failure handling & fallbacks
- Query intent detection
"""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Optional, Dict, List, Any

import requests
from mcp import ClientSession
from mcp.client.sse import sse_client
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# CONFIGURATION
# ============================================================================

CLICKUP_API_TOKEN = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID")
CLICKUP_BASE_URL = "https://api.clickup.com/api/v2"

LM_STUDIO_BASE_URL = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
LM_STUDIO_MODEL = os.getenv("LM_STUDIO_MODEL", "smollm3-3b-absolute-heresy")

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/sse")

# Token budget for SmolLM3-3B (Q4_K_M quantization)
TOKEN_BUDGET = {
    "system": 200,  # System prompt max (concise, proven)
    "history": 400,  # Conversation history max (8-10 messages for context)
    "response": 200,  # Max tokens for response (avoid hallucination)
    "total": 2000,  # Total context limit (3B model)
}

# ============================================================================
# CLICKUP API HELPERS
# ============================================================================


def _clickup_headers() -> Dict[str, str]:
    """Get ClickUp API headers"""
    return {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}


def _clickup_api(
    method: str,
    endpoint: str,
    params: Optional[Dict] = None,
    data: Optional[Dict] = None,
):
    """Make ClickUp API call with error handling"""
    url = f"{CLICKUP_BASE_URL}{endpoint}"
    try:
        response = requests.request(
            method,
            url,
            headers=_clickup_headers(),
            params=params,
            json=data,
            timeout=30,
        )
        if response.status_code == 200:
            return response.json(), None
        else:
            return None, f"API Error {response.status_code}: {response.text[:200]}"
    except Exception as e:
        return None, f"Exception: {str(e)}"


def get_team_id() -> str:
    """Get ClickUp team ID from config or API"""
    if CLICKUP_TEAM_ID:
        return CLICKUP_TEAM_ID
    data, error = _clickup_api("GET", "/team")
    if data and data.get("teams"):
        return data["teams"][0]["id"]
    return None


# ============================================================================
# TIME ENTRY REPORT GENERATORS (4 TYPES)
# ============================================================================


def generate_space_wise_time_report(
    start_date_ms: Optional[int] = None, end_date_ms: Optional[int] = None
) -> Dict:
    """
    Report 1: Space-wise time entry aggregation
    Groups all time entries by Space
    If dates not provided, fetches last 30 days
    """
    # Default to last 30 days if not provided
    if end_date_ms is None:
        end_date_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if start_date_ms is None:
        start_date_ms = int(
            (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
        )

    team_id = get_team_id()
    if not team_id:
        return {"error": "No team ID found"}

    # Get workspace structure for mapping
    spaces_data, error = _clickup_api("GET", f"/team/{team_id}/space")
    if error:
        return {"error": error}

    # Build space ID -> name mapping
    space_map = {s["id"]: s["name"] for s in spaces_data.get("spaces", [])}

    # Get all time entries for date range
    params = {"start_date": start_date_ms, "end_date": end_date_ms}
    time_data, error = _clickup_api(
        "GET", f"/team/{team_id}/time_entries", params=params
    )

    if error:
        return {"error": error}

    time_entries = time_data.get("data", [])

    # Group by space
    space_report = defaultdict(
        lambda: {
            "total_duration_ms": 0,
            "entries_count": 0,
            "members": set(),
            "tasks": set(),
        }
    )

    for entry in time_entries:
        task_id = entry.get("task", {}).get("id")
        if not task_id:
            continue

        # Get task details to find space
        task_data, _ = _clickup_api("GET", f"/task/{task_id}")
        if not task_data:
            continue

        space_id = task_data.get("space", {}).get("id")
        space_name = space_map.get(space_id, "Unknown Space")

        duration = int(entry.get("duration", 0))
        user_name = entry.get("user", {}).get("username", "Unknown")

        space_report[space_name]["total_duration_ms"] += duration
        space_report[space_name]["entries_count"] += 1
        space_report[space_name]["members"].add(user_name)
        space_report[space_name]["tasks"].add(task_id)

    # Convert sets to counts and calculate hours
    final_report = {}
    for space_name, data in space_report.items():
        hours = round(data["total_duration_ms"] / (1000 * 60 * 60), 2)
        final_report[space_name] = {
            "total_hours": hours,
            "total_entries": data["entries_count"],
            "unique_members": len(data["members"]),
            "unique_tasks": len(data["tasks"]),
        }

    return {
        "report_type": "space_wise_time_entries",
        "date_range": {
            "start": datetime.fromtimestamp(start_date_ms / 1000).isoformat(),
            "end": datetime.fromtimestamp(end_date_ms / 1000).isoformat(),
        },
        "spaces": final_report,
        "total_spaces": len(final_report),
    }


def generate_space_folder_member_report(
    start_date_ms: Optional[int] = None,
    end_date_ms: Optional[int] = None,
    space_name: Optional[str] = None,
) -> Dict:
    """
    Report 2: Space > Folder > Team member wise time entry report
    Hierarchical breakdown
    """
    # Default to last 30 days
    if end_date_ms is None:
        end_date_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if start_date_ms is None:
        start_date_ms = int(
            (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
        )

    team_id = get_team_id()
    if not team_id:
        return {"error": "No team ID found"}

    # Get time entries
    params = {"start_date": start_date_ms, "end_date": end_date_ms}
    time_data, error = _clickup_api(
        "GET", f"/team/{team_id}/time_entries", params=params
    )

    if error:
        return {"error": error}

    time_entries = time_data.get("data", [])

    # Build hierarchical report
    hierarchical = defaultdict(
        lambda: defaultdict(
            lambda: defaultdict(lambda: {"total_ms": 0, "entries": 0, "tasks": set()})
        )
    )

    for entry in time_entries:
        task_id = entry.get("task", {}).get("id")
        if not task_id:
            continue

        task_data, _ = _clickup_api("GET", f"/task/{task_id}")
        if not task_data:
            continue

        task_space = task_data.get("space", {}).get("name", "Unknown Space")
        task_folder = task_data.get("folder", {}).get("name", "No Folder")
        user = entry.get("user", {}).get("username", "Unknown User")
        duration = int(entry.get("duration", 0))

        # Filter by space if specified
        if space_name and task_space.lower() != space_name.lower():
            continue

        hierarchical[task_space][task_folder][user]["total_ms"] += duration
        hierarchical[task_space][task_folder][user]["entries"] += 1
        hierarchical[task_space][task_folder][user]["tasks"].add(task_id)

    # Convert to JSON-serializable format
    final_report = {}
    for space, folders in hierarchical.items():
        final_report[space] = {}
        for folder, users in folders.items():
            final_report[space][folder] = {}
            for user, data in users.items():
                hours = round(data["total_ms"] / (1000 * 60 * 60), 2)
                final_report[space][folder][user] = {
                    "total_hours": hours,
                    "total_entries": data["entries"],
                    "unique_tasks": len(data["tasks"]),
                }

    return {
        "report_type": "space_folder_member_time_entries",
        "date_range": {
            "start": datetime.fromtimestamp(start_date_ms / 1000).isoformat(),
            "end": datetime.fromtimestamp(end_date_ms / 1000).isoformat(),
        },
        "filter": {"space": space_name} if space_name else None,
        "hierarchy": final_report,
    }


def generate_team_member_report(
    start_date_ms: Optional[int] = None,
    end_date_ms: Optional[int] = None,
    member_name: Optional[str] = None,
) -> Dict:
    """
    Report 3: Team member wise time entry report
    Shows time per user with daily breakdown
    """
    # Default to last 30 days
    if end_date_ms is None:
        end_date_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if start_date_ms is None:
        start_date_ms = int(
            (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
        )

    team_id = get_team_id()
    if not team_id:
        return {"error": "No team ID found"}

    # Get time entries
    params = {"start_date": start_date_ms, "end_date": end_date_ms}
    time_data, error = _clickup_api(
        "GET", f"/team/{team_id}/time_entries", params=params
    )

    if error:
        return {"error": error}

    time_entries = time_data.get("data", [])

    member_report = defaultdict(
        lambda: {
            "total_ms": 0,
            "entries": 0,
            "tasks": set(),
            "spaces": set(),
            "daily": defaultdict(float),
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
        task_data, _ = (
            _clickup_api("GET", f"/task/{task_id}") if task_id else (None, None)
        )
        space_name = (
            task_data.get("space", {}).get("name", "Unknown")
            if task_data
            else "Unknown"
        )

        # Date for daily breakdown
        date_str = datetime.fromtimestamp(start_ms / 1000).date().isoformat()
        hours = duration / (1000 * 60 * 60)

        member_report[user]["total_ms"] += duration
        member_report[user]["entries"] += 1
        if task_id:
            member_report[user]["tasks"].add(task_id)
        member_report[user]["spaces"].add(space_name)
        member_report[user]["daily"][date_str] += round(hours, 2)

    # Convert to serializable format
    final_report = {}
    for user, data in member_report.items():
        hours = round(data["total_ms"] / (1000 * 60 * 60), 2)
        final_report[user] = {
            "total_hours": hours,
            "total_entries": data["entries"],
            "unique_tasks": len(data["tasks"]),
            "spaces_worked": sorted(list(data["spaces"])),
            "daily_hours": dict(data["daily"]),
        }

    return {
        "report_type": "team_member_time_entries",
        "date_range": {
            "start": datetime.fromtimestamp(start_date_ms / 1000).isoformat(),
            "end": datetime.fromtimestamp(end_date_ms / 1000).isoformat(),
        },
        "filter": {"member": member_name} if member_name else None,
        "members": final_report,
        "total_members": len(final_report),
    }


def generate_weekly_report(report_type: str, weeks_back: int = 1, **filters) -> Dict:
    """
    Report 4: Weekly breakdown of any of above 3 reports
    report_type: 'space' | 'space_folder_member' | 'team_member'
    """
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
                week_start_ms, week_end_ms, filters.get("space_name")
            )
        elif report_type == "team_member":
            week_report = generate_team_member_report(
                week_start_ms, week_end_ms, filters.get("member_name")
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
# LM STUDIO CLIENT (SmolLM)
# ============================================================================


class SmolLMClient:
    """Token-optimized client for SmolLM-1.7B-Instruct"""

    def __init__(self):
        self.base_url = LM_STUDIO_BASE_URL
        self.model = LM_STUDIO_MODEL
        self.conversation_history = []
        self.token_count = 0
        self.system_prompt = self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        """Strict system prompt for SmolLM3-3B - prevents hallucination"""
        return """You are a ClickUp assistant. Use ONLY the exact tools provided.

STRICT RULES:
1. NEVER generate <think> tags or reasoning
2. NEVER invent data, IDs, or tool names
3. Use EXACT tool names: get_workspaces, get_spaces, get_folders, get_tasks
4. Present tool results as-is (1-2 sentences max)
5. If unsure: ask for clarification

EXACT TOOL NAMES:
- get_workspaces() - list all workspaces
- get_spaces(workspace_id) - list spaces in workspace
- get_folders(space_id) - list folders in space
- get_tasks(list_id) - list tasks
- get_team_time_entries() - team time data

WRONG: "get_works()", "fetch_workspace()", "list_workspaces()"
CORRECT: "get_workspaces()"

Response: Present data directly. NO explanations, NO examples."""

    async def send_message(
        self, messages: List[Dict], tools: Optional[List[Dict]] = None
    ) -> Dict:
        """Send message to SmolLM via LM Studio"""
        try:
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.0,  # Zero temp - complete determinism, no hallucination
                "top_p": 1.0,
                "frequency_penalty": 0.0,
                "presence_penalty": 0.0,
                "max_tokens": TOKEN_BUDGET["response"],
                "stream": False,
            }

            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            response = requests.post(
                f"{self.base_url}/chat/completions", json=payload, timeout=60
            )

            if response.status_code == 200:
                result = response.json()
                # Track tokens
                usage = result.get("usage", {})
                self.token_count += usage.get("total_tokens", 0)
                return result
            else:
                return {
                    "error": f"LM Studio error {response.status_code}: {response.text[:100]}"
                }

        except Exception as e:
            return {"error": f"LM Studio request failed: {str(e)}"}


# ============================================================================
# MCP CLIENT (Tool Integration)
# ============================================================================


class MCPToolManager:
    """Manages MCP server connection and tool calls with error recovery"""

    def __init__(self):
        self.session = None
        self.tools = []
        self.read_stream = None
        self.write_stream = None
        self.sse_connection = None

    async def connect(self):
        """Connect to MCP server"""
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
            return False

    async def cleanup(self):
        """Cleanup MCP connection"""
        try:
            if self.session:
                await self.session.__aexit__(None, None, None)
            if self.sse_connection:
                await self.sse_connection.__aexit__(None, None, None)
        except Exception as e:
            print(f"⚠️  Error during cleanup: {e}")

    async def call_tool(self, tool_name: str, arguments: Dict) -> Any:
        """Call MCP tool with error recovery"""
        try:
            if not self.session:
                return {"error": "Not connected to MCP server"}

            print(f"🔧 Calling: {tool_name}")
            result = await self.session.call_tool(tool_name, arguments)

            # Extract content
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
        """Convert MCP tools to function calling format"""
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
# MAIN ORCHESTRATOR (SMOLLM + MCP)
# ============================================================================


class SmolLMClickUpAssistant:
    """Main assistant combining SmolLM, MCP tools, and custom reports"""

    def __init__(self):
        self.smollm = SmolLMClient()
        self.mcp = MCPToolManager()
        self.total_tokens = 0
        self.last_workspace_name = None
        self.last_space_id = None

    async def initialize(self):
        """Initialize MCP connection"""
        return await self.mcp.connect()

    def _trim_history(self, max_messages: int = 3):
        """Keep only last N messages to save tokens"""
        if len(self.smollm.conversation_history) > max_messages * 2:
            self.smollm.conversation_history = self.smollm.conversation_history[
                -(max_messages * 2) :
            ]

    def _extract_intent(self, user_query: str) -> str:
        """Detect query intent for routing"""
        query_lower = user_query.lower()

        intents = {
            "list_workspaces": ["list workspace", "all workspace", "show workspace"],
            "get_spaces": ["space", "spaces"],
            "get_folders": ["folder", "folders"],
            "get_lists": ["list", "lists"],
            "time_report_space": ["space.*time", "time.*space"],
            "time_report_member": ["team member", "member.*report", "who.*work"],
            "time_report_weekly": ["weekly", "week"],
            "direct_query": ["show", "get", "fetch", "list"],
        }

        for intent, keywords in intents.items():
            for keyword in keywords:
                if re.search(keyword, query_lower):
                    return intent

        return "general"

    async def process_message(self, user_message: str) -> str:
        """Process user message with token optimization"""
        # Trim history
        self._trim_history()

        # Add to history
        self.smollm.conversation_history.append(
            {"role": "user", "content": user_message}
        )

        # Prepare messages with system prompt
        messages = [{"role": "system", "content": self.smollm.system_prompt}]
        messages.extend(self.smollm.conversation_history)

        # Get all available tools
        all_tools = self._get_all_tools()

        # Send to SmolLM
        response = await self.smollm.send_message(messages, all_tools)

        if "error" in response:
            return f"❌ Error: {response['error']}"

        choice = response.get("choices", [{}])[0]
        message = choice.get("message", {})

        # Check for tool calls
        tool_calls = message.get("tool_calls", [])

        if tool_calls:
            print(f"\n🔍 Detected {len(tool_calls)} tool call(s)")

            # Execute tool call
            tc = tool_calls[0]  # SmolLM: one tool per response
            function_name = tc["function"]["name"]
            arguments = json.loads(tc["function"]["arguments"])

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
                    arguments.get("report_type"),
                    arguments.get("weeks_back", 1),
                    **arguments.get("filters", {}),
                )
            else:
                # Call MCP tool
                result = await self.mcp.call_tool(function_name, arguments)

            # Add to history
            self.smollm.conversation_history.append(
                {"role": "assistant", "tool_calls": tool_calls}
            )
            self.smollm.conversation_history.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, default=str)[
                        :1500
                    ],  # More context to prevent hallucination
                }
            )

            # Get final response
            final_messages = [{"role": "system", "content": self.smollm.system_prompt}]
            final_messages.extend(self.smollm.conversation_history)

            final_response = await self.smollm.send_message(final_messages, all_tools)

            if "error" in final_response:
                return f"❌ Error: {final_response['error']}"

            final_choice = final_response.get("choices", [{}])[0]
            final_message = final_choice.get("message", {})
            final_text = final_message.get("content", "")

            # Detect hallucination patterns - if present, show raw tool result instead
            hallucination_patterns = [
                "for example",
                "here are the steps",
                "you can use",
                "something like this",
                "[TOOL_RESULT]",
                "might return",
            ]

            is_hallucinating = any(
                pattern.lower() in final_text.lower()
                for pattern in hallucination_patterns
            )

            if is_hallucinating or len(final_text) > 500:
                # Display raw tool result instead of hallucinated response
                print(f"🔧 Calling: {function_name}")
                formatted_result = json.dumps(result, indent=2, default=str)
                final_text = f"Tool result:\n{formatted_result}"

            # Track tokens
            final_usage = final_response.get("usage", {})
            self.total_tokens += final_usage.get("total_tokens", 0)

            self.smollm.conversation_history.append(
                {"role": "assistant", "content": final_text}
            )

            token_display = f"\n\n📊 Tokens used: {final_usage.get('total_tokens', 0)} | Session total: {self.total_tokens}"
            return final_text + token_display

        else:
            # Direct response (model didn't call tools)
            text = message.get("content", "")

            # Detect if model is hallucinating instead of calling tools
            if "<think>" in text.lower() or "get_works" in text or len(text) > 300:
                # Model is hallucinating - force it to use tools
                error_msg = "⚠️ Model didn't call tools properly. Available tools: get_workspaces, get_spaces, get_folders, get_tasks. Please rephrase your request (e.g., 'list workspaces')."
                return error_msg

            usage = response.get("usage", {})
            self.total_tokens += usage.get("total_tokens", 0)

            self.smollm.conversation_history.append(
                {"role": "assistant", "content": text}
            )

            token_display = f"\n\n📊 Tokens used: {usage.get('total_tokens', 0)} | Session total: {self.total_tokens}"
            return text + token_display

    def _get_all_tools(self) -> List[Dict]:
        """Get all tools (MCP + custom reports)"""
        tools = self.mcp.get_tools_schema()

        # Add custom report tools
        custom_tools = [
            {
                "type": "function",
                "function": {
                    "name": "generate_space_wise_time_report",
                    "description": "Space-wise time entry report",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "start_date_ms": {
                                "type": "integer",
                                "description": "Start date in ms (optional)",
                            },
                            "end_date_ms": {
                                "type": "integer",
                                "description": "End date in ms (optional)",
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
                    "description": "Space > Folder > Member time report",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "start_date_ms": {
                                "type": "integer",
                                "description": "Start date in ms (optional)",
                            },
                            "end_date_ms": {
                                "type": "integer",
                                "description": "End date in ms (optional)",
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
                    "description": "Team member-wise time report",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "start_date_ms": {
                                "type": "integer",
                                "description": "Start date in ms (optional)",
                            },
                            "end_date_ms": {
                                "type": "integer",
                                "description": "End date in ms (optional)",
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
                    "description": "Weekly breakdown of reports",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "report_type": {
                                "type": "string",
                                "enum": ["space", "space_folder_member", "team_member"],
                            },
                            "weeks_back": {
                                "type": "integer",
                                "description": "Number of weeks",
                            },
                            "filters": {
                                "type": "object",
                                "description": "Optional filters",
                            },
                        },
                        "required": ["report_type"],
                    },
                },
            },
        ]

        tools.extend(custom_tools)
        return tools


# ============================================================================
# INTERACTIVE CLI
# ============================================================================


async def main():
    """Main interactive loop"""
    print("\n" + "=" * 70)
    print("🤖 SmolLM3-3B + ClickUp MCP".center(70))
    print("=" * 70)

    # Initialize
    assistant = SmolLMClickUpAssistant()

    print("\n🔄 Initializing...")
    if not await assistant.initialize():
        print("❌ Failed to connect to MCP server.")
        return

    print("✅ Ready!\n")

    print("=" * 70)
    print("💡 Examples: 'list workspaces', 'spaces in Avinashi', 'team member report'")
    print("=" * 70 + "\n")

    while True:
        try:
            user_input = input("💬 You: ").strip()

            if not user_input:
                continue

            if user_input.lower() in ["quit", "exit", "q"]:
                print(f"\n📊 Session Total Tokens: {assistant.total_tokens}")
                print("👋 Goodbye!")
                await assistant.mcp.cleanup()
                break

            print(f"\n{'─' * 70}")
            response = await assistant.process_message(user_input)
            print(f"🤖 Assistant:\n{response}")
            print(f"{'─' * 70}\n")

        except KeyboardInterrupt:
            print("\n\n👋 Session interrupted.")
            await assistant.mcp.cleanup()
            break
        except Exception as e:
            print(f"\n❌ ERROR: {e}")


if __name__ == "__main__":
    asyncio.run(main())
