"""
SmolLM3-3B + ClickUp MCP Integration
Intent-Based Routing for Zero Hallucination

Architecture:
- Intent detection via keyword matching (no LLM for tool selection)
- Direct tool calls (bypasses unreliable function calling)
- LLM only for response summarization (optional)
- 4 time entry reports with real data
"""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Optional, Dict, List, Any, Tuple

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

# Token budget - minimal for summarization only
TOKEN_BUDGET = {
    "system": 100,
    "response": 150,
    "total": 500,
}

# ============================================================================
# INTENT PATTERNS - Maps user queries to tools
# ============================================================================

INTENT_PATTERNS = {
    # Workspace commands
    "get_workspaces": [
        r"(fetch|get|list|show|display)\s*(all\s*)?(workspace|team)s?",
        r"workspace",
        r"what.*workspace",
        r"which.*workspace",
    ],
    # Space commands
    "get_spaces": [
        r"(fetch|get|list|show|display)\s*(all\s*)?spaces?(?!.*workspace)",
        r"spaces?\s*(in|for|of|inside)",
        r"what.*spaces?(?!.*workspace)",
        r"all\s*spaces?(?!.*workspace)",
    ],
    # Folder commands
    "get_folders": [
        r"(fetch|get|list|show|display)\s*(all\s*)?folders?",
        r"folders?\s*(in|for|of)",
    ],
    # List commands
    "get_folderless_lists": [
        r"(fetch|get|list|show|display)\s*(all\s*)?lists?",
        r"lists?\s*(in|for|of)",
    ],
    # Task commands
    "get_tasks": [
        r"(fetch|get|list|show|display)\s*(all\s*)?tasks?",
        r"tasks?\s*(in|for|of)",
    ],
    # Time entry reports
    "time_report_space": [
        r"(time|hour).*report.*space",
        r"space.*time.*report",
        r"time\s*entr(y|ies).*space",
    ],
    "time_report_member": [
        r"(time|hour).*report.*member",
        r"member.*time.*report",
        r"team.*member.*report",
        r"who.*work",
        r"employee.*time",
    ],
    "time_report_folder": [
        r"(time|hour).*report.*(folder|project)",
        r"folder.*time.*report",
        r"project.*time",
    ],
    "time_report_weekly": [
        r"weekly.*report",
        r"this.*week",
        r"last.*week",
        r"week.*report",
    ],
    # Time entries
    "get_time_entries": [
        r"time\s*entr(y|ies)",
        r"(fetch|get|list|show)\s*time",
        r"how.*much.*time",
        r"hours?\s*logged",
    ],
    # Debug command
    "debug_mcp": [
        r"debug\s*(mcp|tools?)",
        r"test\s*(mcp|tools?)",
        r"raw\s*(mcp|tools?)",
    ],
}

# ============================================================================
# CLICKUP API HELPERS
# ============================================================================


def _clickup_headers() -> Dict[str, str]:
    return {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}


def _clickup_api(
    method: str,
    endpoint: str,
    params: Optional[Dict] = None,
    data: Optional[Dict] = None,
):
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
        return None, f"API Error {response.status_code}: {response.text[:200]}"
    except Exception as e:
        return None, f"Exception: {str(e)}"


def get_team_id() -> str:
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
    """Report 1: Time entries grouped by Space"""
    if end_date_ms is None:
        end_date_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if start_date_ms is None:
        start_date_ms = int(
            (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
        )

    team_id = get_team_id()
    if not team_id:
        return {"error": "No team ID found"}

    print(f"  🏢 Using team ID: {team_id}")

    # Get spaces
    spaces_data, error = _clickup_api("GET", f"/team/{team_id}/space")
    if error:
        return {"error": f"Failed to get spaces: {error}"}

    print(f"  📁 Spaces API response: {json.dumps(spaces_data, default=str)[:300]}...")

    # Build space mapping
    space_map = {}
    if isinstance(spaces_data, dict) and "spaces" in spaces_data:
        for s in spaces_data["spaces"]:
            space_map[s.get("id")] = s.get("name", "Unknown Space")

    print(f"  🗺️ Space mapping: {space_map}")

    # Get time entries
    params = {"start_date": start_date_ms, "end_date": end_date_ms}
    time_data, error = _clickup_api(
        "GET", f"/team/{team_id}/time_entries", params=params
    )
    if error:
        return {"error": f"Failed to get time entries: {error}"}

    entries = time_data.get("data", []) if isinstance(time_data, dict) else []
    print(f"  ⏱️ Time entries found: {len(entries)}")

    if not entries:
        return {
            "report_type": "Space-wise Time Report",
            "period": f"{datetime.fromtimestamp(start_date_ms / 1000).strftime('%Y-%m-%d')} to {datetime.fromtimestamp(end_date_ms / 1000).strftime('%Y-%m-%d')}",
            "message": "No time entries found in the specified period",
            "total_entries": 0,
            "total_hours": 0,
            "spaces": [],
        }

    # Group by space
    space_report = defaultdict(lambda: {"total_ms": 0, "count": 0, "members": set()})

    for entry in entries:
        # Debug first few entries
        if space_report.__len__() < 3:
            print(f"    📝 Entry sample: {json.dumps(entry, default=str)[:200]}...")

        task = entry.get("task", {})
        if not task:
            continue

        # Handle different space ID formats
        space_id = None
        if isinstance(task, dict):
            space_info = task.get("space")
            if isinstance(space_info, dict):
                space_id = space_info.get("id")
            elif isinstance(space_info, str):
                space_id = space_info

        if space_id:
            space_name = space_map.get(space_id, f"Space {space_id}")
            duration = int(entry.get("duration", 0))
            space_report[space_name]["total_ms"] += duration
            space_report[space_name]["count"] += 1

            user = entry.get("user", {})
            if user:
                username = user.get("username") or user.get("email") or "Unknown"
                space_report[space_name]["members"].add(username)

    # Format result
    result = {
        "report_type": "Space-wise Time Report",
        "period": f"{datetime.fromtimestamp(start_date_ms / 1000).strftime('%Y-%m-%d')} to {datetime.fromtimestamp(end_date_ms / 1000).strftime('%Y-%m-%d')}",
        "total_entries": len(entries),
        "spaces": [],
    }

    for space_name, data in sorted(
        space_report.items(), key=lambda x: x[1]["total_ms"], reverse=True
    ):
        hours = round(data["total_ms"] / 3600000, 2)
        result["spaces"].append(
            {
                "name": space_name,
                "hours": hours,
                "entries": data["count"],
                "members": len(data["members"]),
            }
        )

    result["total_hours"] = round(sum(s["hours"] for s in result["spaces"]), 2)
    return result


def generate_team_member_report(
    start_date_ms: Optional[int] = None,
    end_date_ms: Optional[int] = None,
    member_name: Optional[str] = None,
) -> Dict:
    """Report 2: Time entries grouped by Team Member"""
    if end_date_ms is None:
        end_date_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if start_date_ms is None:
        start_date_ms = int(
            (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
        )

    team_id = get_team_id()
    if not team_id:
        return {"error": "No team ID found"}

    params = {"start_date": start_date_ms, "end_date": end_date_ms}
    time_data, error = _clickup_api(
        "GET", f"/team/{team_id}/time_entries", params=params
    )
    if error:
        return {"error": error}

    # Group by member
    member_report = defaultdict(lambda: {"total_ms": 0, "count": 0, "tasks": set()})

    for entry in time_data.get("data", []):
        user = entry.get("user", {})
        username = user.get("username", "Unknown")

        if member_name and member_name.lower() not in username.lower():
            continue

        member_report[username]["total_ms"] += int(entry.get("duration", 0))
        member_report[username]["count"] += 1
        if entry.get("task"):
            member_report[username]["tasks"].add(entry["task"].get("name", "Unknown"))

    result = {
        "report_type": "Team Member Time Report",
        "period": f"{datetime.fromtimestamp(start_date_ms / 1000).strftime('%Y-%m-%d')} to {datetime.fromtimestamp(end_date_ms / 1000).strftime('%Y-%m-%d')}",
        "members": [],
    }

    for member, data in sorted(
        member_report.items(), key=lambda x: x[1]["total_ms"], reverse=True
    ):
        hours = round(data["total_ms"] / 3600000, 2)
        result["members"].append(
            {
                "name": member,
                "hours": hours,
                "entries": data["count"],
                "tasks": len(data["tasks"]),
            }
        )

    result["total_hours"] = round(sum(m["hours"] for m in result["members"]), 2)
    return result


def generate_folder_wise_report(
    start_date_ms: Optional[int] = None,
    end_date_ms: Optional[int] = None,
    space_name: Optional[str] = None,
) -> Dict:
    """Report 3: Time entries grouped by Folder/Project"""
    if end_date_ms is None:
        end_date_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if start_date_ms is None:
        start_date_ms = int(
            (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
        )

    team_id = get_team_id()
    if not team_id:
        return {"error": "No team ID found"}

    params = {"start_date": start_date_ms, "end_date": end_date_ms}
    time_data, error = _clickup_api(
        "GET", f"/team/{team_id}/time_entries", params=params
    )
    if error:
        return {"error": error}

    # Group by folder
    folder_report = defaultdict(lambda: {"total_ms": 0, "count": 0, "members": set()})

    for entry in time_data.get("data", []):
        task = entry.get("task", {})
        folder = task.get("folder", {})
        folder_name = folder.get("name", "No Folder") if folder else "No Folder"

        folder_report[folder_name]["total_ms"] += int(entry.get("duration", 0))
        folder_report[folder_name]["count"] += 1
        if entry.get("user"):
            folder_report[folder_name]["members"].add(
                entry["user"].get("username", "Unknown")
            )

    result = {
        "report_type": "Folder/Project Time Report",
        "period": f"{datetime.fromtimestamp(start_date_ms / 1000).strftime('%Y-%m-%d')} to {datetime.fromtimestamp(end_date_ms / 1000).strftime('%Y-%m-%d')}",
        "folders": [],
    }

    for folder_name, data in sorted(
        folder_report.items(), key=lambda x: x[1]["total_ms"], reverse=True
    ):
        hours = round(data["total_ms"] / 3600000, 2)
        result["folders"].append(
            {
                "name": folder_name,
                "hours": hours,
                "entries": data["count"],
                "members": len(data["members"]),
            }
        )

    result["total_hours"] = round(sum(f["hours"] for f in result["folders"]), 2)
    return result


def generate_weekly_report(weeks_back: int = 0) -> Dict:
    """Report 4: Weekly time summary"""
    now = datetime.now(timezone.utc)

    # Calculate week boundaries
    start_of_week = now - timedelta(days=now.weekday() + (weeks_back * 7))
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_week = start_of_week + timedelta(days=6, hours=23, minutes=59, seconds=59)

    start_ms = int(start_of_week.timestamp() * 1000)
    end_ms = int(end_of_week.timestamp() * 1000)

    team_id = get_team_id()
    if not team_id:
        return {"error": "No team ID found"}

    params = {"start_date": start_ms, "end_date": end_ms}
    time_data, error = _clickup_api(
        "GET", f"/team/{team_id}/time_entries", params=params
    )
    if error:
        return {"error": error}

    # Group by day
    daily_report = defaultdict(lambda: {"total_ms": 0, "count": 0})
    member_totals = defaultdict(int)

    for entry in time_data.get("data", []):
        start = int(entry.get("start", 0))
        day = datetime.fromtimestamp(start / 1000, timezone.utc).strftime("%A")
        duration = int(entry.get("duration", 0))

        daily_report[day]["total_ms"] += duration
        daily_report[day]["count"] += 1

        if entry.get("user"):
            member_totals[entry["user"].get("username", "Unknown")] += duration

    result = {
        "report_type": "Weekly Time Report",
        "week": f"{start_of_week.strftime('%Y-%m-%d')} to {end_of_week.strftime('%Y-%m-%d')}",
        "days": [],
        "top_contributors": [],
    }

    days_order = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]
    for day in days_order:
        if day in daily_report:
            hours = round(daily_report[day]["total_ms"] / 3600000, 2)
            result["days"].append(
                {"day": day, "hours": hours, "entries": daily_report[day]["count"]}
            )

    for member, total_ms in sorted(
        member_totals.items(), key=lambda x: x[1], reverse=True
    )[:5]:
        result["top_contributors"].append(
            {"name": member, "hours": round(total_ms / 3600000, 2)}
        )

    result["total_hours"] = round(sum(d["hours"] for d in result["days"]), 2)
    return result


# ============================================================================
# INTENT DETECTOR - Human-like query understanding
# ============================================================================


class IntentDetector:
    """Detects user intent from natural language queries"""

    @staticmethod
    def detect(query: str) -> Tuple[str, Dict[str, Any]]:
        """
        Returns (intent_name, extracted_params)
        """
        query_lower = query.lower().strip()

        # Check each intent pattern
        for intent, patterns in INTENT_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, query_lower):
                    params = IntentDetector._extract_params(query_lower, intent)
                    return intent, params

        return "unknown", {}

    @staticmethod
    def _extract_params(query: str, intent: str) -> Dict[str, Any]:
        """Extract parameters from query based on intent"""
        params = {}

        # Extract workspace/space/folder names (quoted or after "in/for/of")
        name_match = re.search(
            r'(?:in|for|of|named?)\s+["\']?([a-zA-Z0-9_\-\s]+)["\']?', query
        )
        if name_match:
            params["name"] = name_match.group(1).strip()

        # Extract time periods
        if "last week" in query:
            params["weeks_back"] = 1
        elif "this week" in query:
            params["weeks_back"] = 0
        elif "last month" in query:
            params["days_back"] = 30
        elif "last 7 days" in query:
            params["days_back"] = 7

        return params


# ============================================================================
# MCP TOOL MANAGER
# ============================================================================


class MCPToolManager:
    """Manages MCP server connection and tool calls"""

    def __init__(self):
        self.session = None
        self.tools = []
        self.read_stream = None
        self.write_stream = None
        self.sse_connection = None
        self._workspace_cache = None
        self._space_cache = {}

    async def connect(self):
        try:
            self.sse_connection = sse_client(MCP_SERVER_URL)
            self.read_stream, self.write_stream = await self.sse_connection.__aenter__()
            self.session = ClientSession(self.read_stream, self.write_stream)
            await self.session.__aenter__()
            await self.session.initialize()
            tools_response = await self.session.list_tools()
            self.tools = tools_response.tools
            return True
        except Exception as e:
            print(f"❌ MCP connection failed: {e}")
            return False

    async def disconnect(self):
        try:
            if self.session:
                await self.session.__aexit__(None, None, None)
            if self.sse_connection:
                await self.sse_connection.__aexit__(None, None, None)
        except Exception:
            pass

    async def call_tool(self, tool_name: str, arguments: Dict = None) -> Any:
        if not self.session:
            return {"error": "Not connected to MCP server"}

        try:
            print(f"  🔧 Calling: {tool_name}")
            if arguments:
                print(f"    📝 Args: {arguments}")

            result = await self.session.call_tool(tool_name, arguments or {})

            # Debug: Show what we got back
            print(f"    📤 Result type: {type(result)}")

            if hasattr(result, "content"):
                if isinstance(result.content, list):
                    content = []
                    for item in result.content:
                        if hasattr(item, "text"):
                            try:
                                parsed = json.loads(item.text)
                                content.append(parsed)
                                print("    📋 Parsed JSON content")
                            except Exception:
                                content.append(item.text)
                                print(f"    📋 Raw text content: {item.text[:100]}...")
                        else:
                            content.append(item)

                    final_result = content[0] if len(content) == 1 else content
                    print(
                        f"    ✅ Final result keys: {list(final_result.keys()) if isinstance(final_result, dict) else 'non-dict'}"
                    )
                    return final_result
                else:
                    print(f"    📋 Direct content: {str(result.content)[:100]}...")
                    return result.content
            else:
                print("    📋 No content attribute, returning raw")
                return result
        except Exception as e:
            error_msg = f"Tool call failed: {str(e)}"
            print(f"    ❌ {error_msg}")
            return {"error": error_msg}
        result = await self.call_tool("get_workspaces")
        if isinstance(result, dict) and "error" not in result:
            self._workspace_cache = result
        return result

    async def get_spaces(self, workspace_id: str) -> List[Dict]:
        """Get spaces in a workspace with caching"""
        if workspace_id in self._space_cache:
            return self._space_cache[workspace_id]

        result = await self.call_tool("get_spaces", {"workspace_id": workspace_id})
        if isinstance(result, list) or (
            isinstance(result, dict) and "error" not in result
        ):
            self._space_cache[workspace_id] = result
        return result


# ============================================================================
# RESPONSE FORMATTER
# ============================================================================


class ResponseFormatter:
    """Formats tool results into human-readable responses"""

    @staticmethod
    def format(intent: str, result: Any) -> str:
        """Format result based on intent type"""
        if isinstance(result, dict) and "error" in result:
            return f"❌ Error: {result['error']}"

        if intent == "get_workspaces":
            return ResponseFormatter._format_workspaces(result)
        elif intent == "get_spaces":
            return ResponseFormatter._format_spaces(result)
        elif intent == "get_folders":
            return ResponseFormatter._format_folders(result)
        elif intent == "get_folderless_lists":
            return ResponseFormatter._format_lists(result)
        elif intent == "get_tasks":
            return ResponseFormatter._format_tasks(result)
        elif intent.startswith("time_report"):
            return ResponseFormatter._format_time_report(result)
        elif intent == "get_time_entries":
            return ResponseFormatter._format_time_entries(result)
        elif intent == "debug_mcp":
            return ResponseFormatter._format_debug(result)
        else:
            return json.dumps(result, indent=2, default=str)

    @staticmethod
    def _format_workspaces(result: Any) -> str:
        # Debug: Show raw result to understand structure
        print(f"  🔍 Raw workspace data: {json.dumps(result, default=str)[:200]}...")

        # Handle different possible response structures
        workspaces = []

        if isinstance(result, dict):
            # Check various possible keys
            if "teams" in result:
                workspaces = result["teams"]
            elif "workspaces" in result:
                workspaces = result["workspaces"]
            elif "data" in result:
                workspaces = result["data"]
            else:
                # Single workspace object
                workspaces = [result]
        elif isinstance(result, list):
            workspaces = result
        else:
            return f"Unexpected workspace data format: {type(result)}"

        if not workspaces:
            return "No workspaces found."

        lines = [f"📁 Found {len(workspaces)} workspace(s):\n"]
        for ws in workspaces:
            # Ensure ws is a dict
            if not isinstance(ws, dict):
                lines.append(f"  • Invalid workspace data: {ws}")
                continue

            name = ws.get("name", "Unknown")
            # Try different ID field names
            id_ = ws.get("id") or ws.get("team_id") or ws.get("workspace_id") or "N/A"

            # Try different member count fields
            members = ws.get("members", [])
            if isinstance(members, list):
                member_count = len(members)
            else:
                member_count = ws.get("member_count") or ws.get("members_count") or 0

            # Additional info
            color = ws.get("color", "")
            if color:
                lines.append(
                    f"  • {name} (ID: {id_}) - {member_count} members [{color}]"
                )
            else:
                lines.append(f"  • {name} (ID: {id_}) - {member_count} members")

    @staticmethod
    def _format_spaces(result: Any) -> str:
        if isinstance(result, dict) and "spaces" in result:
            spaces = result["spaces"]
        elif isinstance(result, list):
            spaces = result
        else:
            return f"Result: {result}"

        if not spaces:
            return "No spaces found."

        lines = [f"📂 Found {len(spaces)} space(s):\n"]
        for s in spaces:
            name = s.get("name", "Unknown")
            id_ = s.get("id", "N/A")
            lines.append(f"  • {name} (ID: {id_})")
        return "\n".join(lines)

    @staticmethod
    def _format_folders(result: Any) -> str:
        if isinstance(result, dict) and "folders" in result:
            folders = result["folders"]
        elif isinstance(result, list):
            folders = result
        else:
            return f"Result: {result}"

        if not folders:
            return "No folders found."

        lines = [f"📁 Found {len(folders)} folder(s):\n"]
        for f in folders:
            name = f.get("name", "Unknown")
            id_ = f.get("id", "N/A")
            lines.append(f"  • {name} (ID: {id_})")
        return "\n".join(lines)

    @staticmethod
    def _format_lists(result: Any) -> str:
        if isinstance(result, dict) and "lists" in result:
            lists = result["lists"]
        elif isinstance(result, list):
            lists = result
        else:
            return f"Result: {result}"

        if not lists:
            return "No lists found."

        lines = [f"📋 Found {len(lists)} list(s):\n"]
        for list_item in lists:
            name = list_item.get("name", "Unknown")
            id_ = list_item.get("id", "N/A")
            lines.append(f"  • {name} (ID: {id_})")
        return "\n".join(lines)

    @staticmethod
    def _format_tasks(result: Any) -> str:
        if isinstance(result, dict) and "tasks" in result:
            tasks = result["tasks"]
        elif isinstance(result, list):
            tasks = result
        else:
            return f"Result: {result}"

        if not tasks:
            return "No tasks found."

        lines = [f"✅ Found {len(tasks)} task(s):\n"]
        for t in tasks[:10]:  # Limit to 10
            name = t.get("name", "Unknown")
            status = t.get("status", {}).get("status", "Unknown")
            lines.append(f"  • {name} [{status}]")

        if len(tasks) > 10:
            lines.append(f"\n  ... and {len(tasks) - 10} more tasks")
        return "\n".join(lines)

    @staticmethod
    def _format_time_report(result: Dict) -> str:
        if "error" in result:
            return f"❌ Error: {result['error']}"

        report_type = result.get("report_type", "Time Report")
        period = result.get("period", result.get("week", ""))
        total = result.get("total_hours", 0)

        lines = [f"📊 {report_type}", f"📅 {period}", f"⏱️  Total: {total} hours\n"]

        # Format based on report type
        if "spaces" in result:
            for item in result["spaces"][:10]:
                lines.append(
                    f"  • {item['name']}: {item['hours']}h ({item['entries']} entries)"
                )
        elif "members" in result:
            for item in result["members"][:10]:
                lines.append(
                    f"  • {item['name']}: {item['hours']}h ({item['tasks']} tasks)"
                )
        elif "folders" in result:
            for item in result["folders"][:10]:
                lines.append(
                    f"  • {item['name']}: {item['hours']}h ({item['entries']} entries)"
                )
        elif "days" in result:
            for item in result["days"]:
                lines.append(f"  • {item['day']}: {item['hours']}h")
            if result.get("top_contributors"):
                lines.append("\n👥 Top contributors:")
                for c in result["top_contributors"][:5]:
                    lines.append(f"  • {c['name']}: {c['hours']}h")

        return "\n".join(lines)

    @staticmethod
    def _format_time_entries(result: Any) -> str:
        if isinstance(result, dict) and "data" in result:
            entries = result["data"]
        elif isinstance(result, list):
            entries = result
        else:
            return f"Result: {result}"

        if not entries:
            return "No time entries found."

        total_ms = sum(int(e.get("duration", 0)) for e in entries)
        total_hours = round(total_ms / 3600000, 2)

        lines = [f"⏱️  Found {len(entries)} time entries ({total_hours} hours total):\n"]

        for e in entries[:5]:
            task = e.get("task", {})
            task_name = task.get("name", "No task") if task else "No task"
            duration_ms = int(e.get("duration", 0))
            hours = round(duration_ms / 3600000, 2)
            user = e.get("user", {}).get("username", "Unknown")
            lines.append(f"  • {task_name}: {hours}h by {user}")

        if len(entries) > 5:
            lines.append(f"\n  ... and {len(entries) - 5} more entries")
        return "\n".join(lines)

    @staticmethod
    def _format_debug(result: Dict) -> str:
        """Format debug information about MCP tools"""
        lines = ["🔧 MCP Debug Information:"]

        total_tools = result.get("available_tools", 0)
        lines.append(f"📦 Total tools available: {total_tools}")

        sample_tools = result.get("sample_tools", [])
        if sample_tools:
            lines.append("\n🛠️ Sample tools (first 10):")
            for tool in sample_tools:
                lines.append(f"  {tool}")

        ws_test = result.get("workspace_test")
        if ws_test:
            lines.append("\n🧪 Workspace test result:")
            if isinstance(ws_test, dict) and "error" in ws_test:
                lines.append(f"  ❌ {ws_test['error']}")
            else:
                lines.append(
                    f"  ✅ Success: {json.dumps(ws_test, default=str)[:100]}..."
                )

        return "\n".join(lines)


# ============================================================================
# MAIN ASSISTANT - Intent-Based Routing
# ============================================================================


class ClickUpAssistant:
    """Main assistant using intent-based routing (no LLM for tool selection)"""

    def __init__(self):
        self.mcp = MCPToolManager()
        self.detector = IntentDetector()
        self.formatter = ResponseFormatter()
        self.context = {}  # Stores workspace_id, space_id, etc.
        self.total_tokens = 0  # Track MCP communication tokens
        self.call_count = 0

    async def initialize(self):
        success = await self.mcp.connect()
        if success:
            print(f"✅ Connected to MCP server - {len(self.mcp.tools)} tools loaded")
        return success

    async def process(self, query: str) -> str:
        """Process user query using intent-based routing"""

        self.call_count += 1
        start_time = datetime.now()

        # Detect intent
        intent, params = self.detector.detect(query)

        if intent == "unknown":
            return self._handle_unknown(query)

        print(f"  🎯 Intent: {intent}")

        # Route to appropriate handler
        result = await self._execute_intent(intent, params)

        # Debug: Show raw result structure
        if isinstance(result, dict) and len(str(result)) < 500:
            print(f"  🔍 Debug: {result}")

        # Format response
        formatted = self.formatter.format(intent, result)

        # Calculate and show token usage estimate
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        # Estimate tokens (rough calculation)
        estimated_tokens = (
            len(query) // 4 + len(str(result)) // 4 + 50
        )  # Rough token estimate
        self.total_tokens += estimated_tokens

        token_info = f"\n\n📊 Estimated tokens: {estimated_tokens} | Session total: {self.total_tokens} | Calls: {self.call_count}"
        return formatted + token_info

    async def _execute_intent(self, intent: str, params: Dict) -> Any:
        """Execute the detected intent"""
        if intent == "get_workspaces":
            result = await self.mcp.call_tool("get_workspaces")
            # Cache first workspace - handle both list and dict responses
            if isinstance(result, list) and result:
                # Result is a list of workspaces
                self.context["workspace_id"] = result[0].get("workspace_id") or result[
                    0
                ].get("id")
            elif isinstance(result, dict) and result.get("teams"):
                # Result is dict with teams key
                self.context["workspace_id"] = result["teams"][0]["id"]
            return result

        elif intent == "get_spaces":
            workspace_id = params.get("name") or self.context.get("workspace_id")
            if not workspace_id:
                # Try to get from workspaces
                ws_result = await self.mcp.call_tool("get_workspaces")

                # Handle list response
                if isinstance(ws_result, list) and ws_result:
                    workspace_id = ws_result[0].get("workspace_id") or ws_result[0].get(
                        "id"
                    )
                    self.context["workspace_id"] = workspace_id
                # Handle dict response
                elif isinstance(ws_result, dict) and ws_result.get("teams"):
                    workspace_id = ws_result["teams"][0]["id"]
                    self.context["workspace_id"] = workspace_id

            if not workspace_id:
                return {"error": "No workspace ID. Run 'list workspaces' first."}

            result = await self.mcp.call_tool(
                "get_spaces", {"workspace_id": workspace_id}
            )
            # Cache first space
            if isinstance(result, list) and result:
                self.context["space_id"] = result[0].get("id")
            elif isinstance(result, dict) and result.get("spaces"):
                self.context["space_id"] = result["spaces"][0].get("id")
            return result

        elif intent == "get_folders":
            space_id = params.get("name") or self.context.get("space_id")
            if not space_id:
                return {"error": "No space ID. Run 'list spaces' first."}
            return await self.mcp.call_tool("get_folders", {"space_id": space_id})

        elif intent == "get_folderless_lists":
            space_id = params.get("name") or self.context.get("space_id")
            if not space_id:
                return {"error": "No space ID. Run 'list spaces' first."}
            return await self.mcp.call_tool(
                "get_folderless_lists", {"space_id": space_id}
            )

        elif intent == "get_tasks":
            list_id = params.get("name") or self.context.get("list_id")
            if not list_id:
                return {"error": "No list ID. Run 'list lists' first."}
            return await self.mcp.call_tool("get_tasks", {"list_id": list_id})

        elif intent == "time_report_space":
            return generate_space_wise_time_report()

        elif intent == "time_report_member":
            return generate_team_member_report(member_name=params.get("name"))

        elif intent == "time_report_folder":
            return generate_folder_wise_report()

        elif intent == "time_report_weekly":
            weeks_back = params.get("weeks_back", 0)
            return generate_weekly_report(weeks_back)

        elif intent == "get_time_entries":
            team_id = get_team_id()
            if not team_id:
                return {"error": "No team ID found"}

            now = datetime.now(timezone.utc)
            end_ms = int(now.timestamp() * 1000)
            start_ms = int((now - timedelta(days=7)).timestamp() * 1000)

            return await self.mcp.call_tool(
                "get_team_time_entries",
                {"team_id": team_id, "start_date": start_ms, "end_date": end_ms},
            )

        elif intent == "debug_mcp":
            # Show all available tools and test basic functionality
            tools_list = [
                f"{i + 1}. {tool.name}: {tool.description[:50]}..."
                for i, tool in enumerate(self.mcp.tools[:10])
            ]

            # Test basic workspace call
            ws_test = await self.mcp.call_tool("get_workspaces")

            return {
                "available_tools": len(self.mcp.tools),
                "sample_tools": tools_list,
                "workspace_test": ws_test,
            }

        return {"error": f"Unknown intent: {intent}"}

    def _handle_unknown(self, query: str) -> str:
        """Handle unknown queries with helpful suggestions"""
        return """🤔 I didn't understand that. Try one of these:

📁 Workspace & Structure:
  • "list workspaces" or "fetch workspace"
  • "show spaces"
  • "get folders"
  • "list tasks"

📊 Time Reports:
  • "space time report" - Time by space
  • "team member report" - Time by person
  • "folder time report" - Time by folder/project
  • "weekly report" - This week's summary
  • "last week report" - Previous week

⏱️  Time Entries:
  • "show time entries"
  • "get time entries"

🔧 Debug:
  • "debug mcp" - Test MCP tools and show available functions
"""

    async def close(self):
        await self.mcp.disconnect()


# ============================================================================
# CLI INTERFACE
# ============================================================================


async def main():
    print("=" * 70)
    print("🤖 SmolLM3-3B + ClickUp MCP".center(70))
    print("Intent-Based Routing (Zero Hallucination)".center(70))
    print("=" * 70)
    print()

    assistant = ClickUpAssistant()

    print("🔄 Initializing...")
    if not await assistant.initialize():
        print("❌ Failed to connect to MCP server")
        return

    print("✅ Ready!")
    print()
    print("=" * 70)
    print("💡 Commands: 'list workspaces', 'space time report', 'weekly report'")
    print("=" * 70)
    print()

    while True:
        try:
            user_input = input("💬 You: ").strip()

            if not user_input:
                continue

            if user_input.lower() in ["quit", "exit", "q"]:
                break

            print()
            print("-" * 70)

            response = await assistant.process(user_input)
            print(f"🤖 Assistant:\n{response}")

            print("-" * 70)
            print()

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"❌ Error: {e}")

    print("👋 Goodbye!")
    await assistant.close()


if __name__ == "__main__":
    asyncio.run(main())
