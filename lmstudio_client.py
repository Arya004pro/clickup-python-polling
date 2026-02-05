"""
LM Studio MCP Client - PRODUCTION VERSION
===============================================
Comprehensive tool understanding with intelligent context management
Optimized for gemma-3-4b with minimal hallucination and maximum tool coverage

Features:
- Automatic workspace initialization and caching
- Intelligent entity resolution (space/folder/list names to IDs)
- Natural language understanding for all 54 tools
- Context-aware report generation
- Persistent workspace state across queries
"""

import asyncio
import os
import json
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
from openai import OpenAI
from mcp import ClientSession
from mcp.client.sse import sse_client

load_dotenv()

# LM Studio Configuration
LM_STUDIO_BASE_URL = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
LM_STUDIO_MODEL = os.getenv("LM_STUDIO_MODEL", "gemma-3-4b")

# MCP Server Configuration
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/sse")

# Initialize LM Studio client
client = OpenAI(
    base_url=LM_STUDIO_BASE_URL,
    api_key="lm-studio",
)


# ============================================================================
# WORKSPACE CONTEXT MANAGER
# ============================================================================


class WorkspaceContext:
    """Maintains workspace state and provides intelligent entity resolution"""

    def __init__(self):
        self.workspace_id = None
        self.workspace_name = None
        self.spaces = {}  # {space_name: space_id}
        self.folders = {}  # {folder_name: (folder_id, space_id)}
        self.lists = {}  # {list_name: (list_id, parent_type, parent_id)}
        self.mapped_projects = {}  # {project_name: {type, id}}
        self.hierarchy_loaded = False

    def set_workspace(self, workspace_id, workspace_name):
        """Set current workspace"""
        self.workspace_id = workspace_id
        self.workspace_name = workspace_name
        print(f"✓ Workspace set: {workspace_name} (ID: {workspace_id})")

    def add_space(self, space_name, space_id):
        """Add space to context"""
        self.spaces[space_name.lower()] = space_id

    def add_folder(self, folder_name, folder_id, space_id):
        """Add folder to context"""
        self.folders[folder_name.lower()] = (folder_id, space_id)

    def add_list(self, list_name, list_id, parent_type, parent_id):
        """Add list to context"""
        self.lists[list_name.lower()] = (list_id, parent_type, parent_id)

    def add_mapped_project(self, project_name, entity_type, entity_id):
        """Add mapped project to context"""
        self.mapped_projects[project_name.lower()] = {
            "type": entity_type,
            "id": entity_id,
        }

    def resolve_entity(self, name):
        """
        Intelligently resolve entity name to ID and type
        Returns: (entity_type, entity_id) or (None, None)
        """
        name_lower = name.lower().strip()

        # Check mapped projects first (highest priority)
        if name_lower in self.mapped_projects:
            proj = self.mapped_projects[name_lower]
            return (proj["type"], proj["id"])

        # Check spaces
        if name_lower in self.spaces:
            return ("space", self.spaces[name_lower])

        # Check folders
        if name_lower in self.folders:
            folder_id, _ = self.folders[name_lower]
            return ("folder", folder_id)

        # Check lists
        if name_lower in self.lists:
            list_id, _, _ = self.lists[name_lower]
            return ("list", list_id)

        return (None, None)

    def get_summary(self):
        """Get context summary"""
        return {
            "workspace": self.workspace_name,
            "spaces_count": len(self.spaces),
            "folders_count": len(self.folders),
            "lists_count": len(self.lists),
            "mapped_projects_count": len(self.mapped_projects),
        }


# ============================================================================
# COMPREHENSIVE SYSTEM PROMPT
# ============================================================================


def create_comprehensive_system_prompt(workspace_context: WorkspaceContext):
    """Create detailed system prompt with all tool documentation"""

    context_info = ""
    if workspace_context.workspace_id:
        summary = workspace_context.get_summary()
        context_info = f"""
CURRENT WORKSPACE CONTEXT:
- Workspace: {summary["workspace"]}
- Workspace ID: {workspace_context.workspace_id}
- Cached Spaces: {summary["spaces_count"]}
- Cached Folders: {summary["folders_count"]}
- Cached Lists: {summary["lists_count"]}
- Mapped Projects: {summary["mapped_projects_count"]}

When user mentions entity names, use the cached context to resolve IDs automatically.
"""

    return f"""You are an expert ClickUp Data Analysis Assistant with access to 54 MCP tools.

{context_info}

CRITICAL OPERATING PRINCIPLES:
================================

1. NEVER INVENT DATA - Always use tools to get real information
2. ALWAYS provide complete, formatted answers - don't just echo tool names
3. Use cached workspace context to resolve entity names to IDs
4. Execute tool sequences step-by-step, waiting for each result
5. If a tool fails, report the error clearly and suggest alternatives

TOOL CALLING FORMAT:
====================

Use this EXACT XML format for tool calls:

<tool_call>
<name>exact_tool_name</name>
<arguments>{{"param1": "value1", "param2": "value2"}}</arguments>
</tool_call>

For tools with no parameters, use empty dict: {{"}}

REPORT TYPE RECOGNITION:
========================

When user asks for reports, recognize these patterns:

1. TIME REPORTS (Keywords: "time", "hours", "tracking", "worked", "spent")
   → Use: get_time_tracking_report or get_project_time_tracking
   → group_by options: "assignee", "status", "task"

2. TEAM REPORTS (Keywords: "team", "workload", "members", "assignees")
   → Use: get_workload or get_project_team_workload

3. WEEKLY DIGEST (Keywords: "weekly", "digest", "summary", "this week")
   → Use: get_project_weekly_digest

4. HEALTH SCORE (Keywords: "health", "score", "status", "overview")
   → Use: get_project_health_score or get_project_status

5. PROGRESS REPORTS (Keywords: "progress", "completed", "since", "done")
   → Use: get_progress_since

6. STANDUP (Keywords: "standup", "daily", "today", "yesterday")
   → Use: get_project_daily_standup

7. BLOCKERS (Keywords: "blocked", "blockers", "stuck", "waiting")
   → Use: get_project_blockers

8. AT-RISK (Keywords: "risk", "overdue", "late", "due")
   → Use: get_project_at_risk or get_overdue_tasks

ENTITY RESOLUTION LOGIC:
========================

When user provides an entity name (without explicit ID):
1. Check if it's a mapped project name
2. Check if it's a space name
3. Check if it's a folder name
4. Check if it's a list name
5. If not found, use discover_hierarchy to search

For project-based tools (get_project_time_tracking, etc.):
- First check list_mapped_projects
- If not mapped, ask user if they want to map it
- For space/folder: use map_project first, then use project tools

COMPLETE TOOL REFERENCE (54 Tools):
====================================

WORKSPACE & STRUCTURE (9 tools):
---------------------------------

1. get_workspaces - List all workspaces
   Parameters: None
   Usage: First call to initialize workspace context

2. get_spaces - List all spaces in workspace
   Parameters: workspace_id (string, required)
   Usage: Get all spaces after workspace initialization

3. get_space - Get specific space details
   Parameters: space_id (string, required)
   Returns: Space details with settings

4. get_folders - List folders in a space
   Parameters: space_id (string, required)
   Returns: All folders with IDs and names

5. get_folder - Get folder details with lists
   Parameters: folder_id (string, required)
   Returns: Folder info and contained lists

6. get_lists - List all lists in a folder
   Parameters: folder_id (string, required)
   Returns: Lists with task counts

7. get_folderless_lists - Get lists not in folders
   Parameters: space_id (string, required)
   Returns: Lists directly in space (not in folders)

8. get_list - Get specific list details
   Parameters: list_id (string, required)
   Returns: List metadata, statuses, task count

9. invalidate_cache - Clear cached data
   Parameters: type (optional) - 'all', 'workspaces', 'spaces', 'folders', 'lists', 'tasks'
   Usage: Use when data seems stale

TASK MANAGEMENT (9 tools):
---------------------------

10. get_tasks - List tasks with filters
    Parameters: list_id (required), include_closed, statuses, assignees, page
    Returns: Filtered task list

11. get_task - Get full task details
    Parameters: task_id (required)
    Returns: Complete task info including time tracking

12. create_task - Create new task
    Parameters: list_id (required), name (required), description, status, priority, assignees, due_date, tags
    Returns: Created task ID and URL

13. update_task - Update existing task
    Parameters: task_id (required), name, description, status, priority, due_date, add_assignees, remove_assignees
    Returns: Update confirmation

14. search_tasks - Search by name in project
    Parameters: project (required), query (required), include_closed
    Returns: Matching tasks

15. get_project_tasks - Get all tasks in project
    Parameters: project (required), include_closed, statuses
    Returns: All project tasks

16. get_list_progress - Progress summary for list
    Parameters: list_id (required)
    Returns: Completion rate, status breakdown, velocity

17. get_workload - Workload by assignee
    Parameters: list_id (required)
    Returns: Task distribution per team member

18. get_overdue_tasks - Find overdue tasks
    Parameters: list_id (required)
    Returns: Overdue tasks with days overdue

PM ANALYTICS (8 tools):
------------------------

19. get_progress_since - Tasks completed since date
    Parameters: project OR list_id, since_date (required), include_status_changes
    Returns: Completed tasks and status changes

20. get_time_tracking_report - Time vs estimate analysis
    Parameters: project OR list_id, group_by (optional: 'assignee', 'task', 'status')
    Returns: Time tracked vs estimated, grouped

21. get_inactive_assignees - Find inactive team members
    Parameters: project OR list_id, inactive_days (default: 3)
    Returns: Members with no recent activity

22. get_untracked_tasks - Tasks with no time logged
    Parameters: project OR list_id, status_filter (optional: 'all', 'in_progress', 'closed')
    Returns: Tasks missing time entries

23. get_stale_tasks - Tasks not updated recently
    Parameters: project OR list_id, stale_days (default: 7)
    Returns: Tasks with no recent updates

24. get_estimation_accuracy - Estimate vs actual analysis
    Parameters: project OR list_id
    Returns: Accuracy metrics and recommendations

25. get_at_risk_tasks - Overdue or at-risk tasks
    Parameters: project OR list_id, risk_days (default: 3)
    Returns: Tasks categorized by urgency

26. get_status_summary - Quick status rollup
    Parameters: project OR list_id
    Returns: Status breakdown for stakeholders

PROJECT CONFIGURATION (7 tools):
---------------------------------

27. discover_projects - Scan workspace for projects
    Parameters: workspace_id (required), project_level (optional: 'space', 'folder', 'list')
    Returns: Discovered entities that can be tracked

28. add_project - Add project to tracking
    Parameters: name (required), type (required), id (required), workspace_id (required)
    Returns: Project added confirmation

29. list_projects - List tracked projects
    Parameters: None
    Returns: All tracked projects with IDs

30. remove_project - Remove from tracking
    Parameters: project_name (required)
    Returns: Removal confirmation

31. refresh_projects - Refresh all project data
    Parameters: None
    Returns: Refresh summary

32. get_project_status - Comprehensive project status
    Parameters: project_name (required)
    Returns: Status with metrics and health

33. get_all_projects_status - Summary for all projects
    Parameters: None
    Returns: Status table for all projects

PROJECT INTELLIGENCE (7 tools):
--------------------------------

34. get_project_health_score - 0-100 health score
    Parameters: project_name (required)
    Returns: Score with component breakdown

35. get_project_daily_standup - Daily standup report
    Parameters: project_name (required)
    Returns: Yesterday's work, today's plan, blockers

36. get_project_time_tracking - Time report for project
    Parameters: project_name (required), group_by (optional: 'assignee', 'list', 'status')
    Returns: Time tracking grouped by dimension

37. get_project_blockers - Find blocked/stale tasks
    Parameters: project_name (required), stale_days (default: 5)
    Returns: Blockers categorized by type

38. get_project_at_risk - Overdue/due-soon tasks
    Parameters: project_name (required), risk_days (default: 3)
    Returns: At-risk tasks by urgency

39. get_project_weekly_digest - Weekly stakeholder digest
    Parameters: project_name (required)
    Returns: Executive summary with key metrics

40. get_project_team_workload - Team workload distribution
    Parameters: project_name (required)
    Returns: Workload per team member

SYNC & MAPPING (10 tools):
---------------------------

41. discover_hierarchy - Full workspace hierarchy
    Parameters: workspace_id (optional), show_archived (optional)
    Returns: Complete hierarchy tree

42. map_project - Map entity as project
    Parameters: id (required), type (required: 'space'/'folder'/'list'), alias (optional)
    Returns: Mapping confirmation with structure

43. list_mapped_projects - Show mapped projects
    Parameters: None
    Returns: All mapped projects with structure

44. get_project - Get mapped project details
    Parameters: alias (required)
    Returns: Project details and structure

45. refresh_project - Refresh project structure
    Parameters: alias (required)
    Returns: Before/after comparison

46. unmap_project - Remove project mapping
    Parameters: alias (required)
    Returns: Unmapped confirmation

47. get_sync_status - Sync and cache status
    Parameters: None
    Returns: Status summary

48. list_spaces - Spaces with mapping status
    Parameters: workspace_id (optional)
    Returns: Spaces showing which are mapped

49. clear_sync - Clear all mappings
    Parameters: confirm (required: must be true)
    Returns: Cleared confirmation

50. prune_cache - Remove expired cache
    Parameters: None
    Returns: Pruning summary

COMMON WORKFLOWS:
=================

WORKFLOW 1: Initialize Workspace (DO THIS FIRST)
-------------------------------------------------
1. Call get_workspaces to get workspace_id
2. Call get_spaces with workspace_id to cache all spaces
3. Optionally call discover_hierarchy for full structure

WORKFLOW 2: Time Report for Space
----------------------------------
User says: "Show me time report for AI space"
1. Resolve "AI" to space_id using cached context
2. Check if "AI" is already mapped: call list_mapped_projects
3. If not mapped: call map_project with space_id, type="space", alias="AI"
4. Call get_project_time_tracking with project_name="AI", group_by="assignee"
5. Format and present results

WORKFLOW 3: Team Member Report
-------------------------------
User says: "Show workload for John"
1. Call list_mapped_projects to get all projects
2. For each project, call get_project_team_workload
3. Filter results for "John" and aggregate
4. Present formatted summary

WORKFLOW 4: Weekly Digest
--------------------------
User says: "Give me weekly report for Luminique"
1. Resolve "Luminique" to entity type and ID
2. If not mapped, map it first
3. Call get_project_weekly_digest with project_name
4. Present digest in readable format

ANSWER FORMATTING RULES:
=========================

1. ALWAYS provide final answers in natural language
2. Include relevant metrics and numbers from tool results
3. Format tables nicely for readability
4. Highlight key insights and actionable items
5. If multiple steps are needed, explain what you're doing
6. Never just echo tool names - always interpret results

EXAMPLE GOOD RESPONSE:
User: "Show time report for AI space"

<tool_call>
<name>list_mapped_projects</name>
<arguments>{{}}</arguments>
</tool_call>

[After getting results]
I can see "AI" is already mapped as a space project. Let me get the time tracking report.

<tool_call>
<name>get_project_time_tracking</name>
<arguments>{{"project_name": "AI", "group_by": "assignee"}}</arguments>
</tool_call>

[After getting results]
Here's the time tracking report for AI space:

**Team Member Time Summary:**

1. John Doe:
   - Time Tracked: 24h 30m
   - Time Estimated: 20h 0m
   - Tasks: 15
   - Efficiency: 122% (over-estimated)

2. Jane Smith:
   - Time Tracked: 18h 15m
   - Time Estimated: 25h 0m
   - Tasks: 12
   - Efficiency: 73% (under-estimated)

**Key Insights:**
- Total team time: 42h 45m
- Overall efficiency: 94%
- Recommendation: Review estimation practices

ERROR HANDLING:
===============

If a tool fails:
1. Clearly state what went wrong
2. Suggest alternative approaches
3. Ask user for clarification if needed

Example:
"I couldn't find a project named 'Marketing'. Here are the available mapped projects:
- AI (space)
- Luminique (folder)
- linkutm (space)

Did you mean one of these, or would you like me to search for it using discover_hierarchy?"

REMEMBER:
=========
- Start EVERY session by getting workspace context
- Cache entity names and IDs for quick resolution
- Use mapped projects when available for better performance
- Always provide complete, formatted answers
- Be helpful and proactive in suggesting next steps
"""


# ============================================================================
# RESULT FORMATTING
# ============================================================================


def format_tool_result(tool_name, result_data, verbose=False):
    """Format tool results for display and LLM context"""

    try:
        if isinstance(result_data, str):
            try:
                data = json.loads(result_data)
            except Exception:
                data = result_data
        else:
            data = result_data

        # Format based on tool type
        if tool_name == "get_workspaces":
            if isinstance(data, list):
                formatted = "📋 WORKSPACES:\n"
                for ws in data:
                    formatted += f"  • {ws.get('name')} (ID: {ws.get('workspace_id', ws.get('id'))})\n"
                return formatted, data

        elif tool_name == "get_spaces":
            if isinstance(data, list):
                formatted = "🗂️  SPACES:\n"
                for space in data:
                    formatted += (
                        f"  • {space.get('name')} (ID: {space.get('space_id')})\n"
                    )
                return formatted, data

        elif tool_name == "get_folders":
            if isinstance(data, list):
                formatted = "📁 FOLDERS:\n"
                for folder in data:
                    list_count = folder.get("list_count", 0)
                    formatted += f"  • {folder.get('name')} ({list_count} lists, ID: {folder.get('folder_id')})\n"
                return formatted, data

        elif tool_name in ["get_lists", "get_folderless_lists"]:
            if isinstance(data, list):
                formatted = "📝 LISTS:\n"
                for lst in data:
                    task_count = lst.get("task_count", 0)
                    formatted += f"  • {lst.get('name')} ({task_count} tasks, ID: {lst.get('list_id')})\n"
                return formatted, data

        elif tool_name in ["get_project_time_tracking", "get_time_tracking_report"]:
            if isinstance(data, dict) and "report" in data:
                formatted = "⏱️  TIME TRACKING REPORT:\n\n"
                for member, stats in data["report"].items():
                    formatted += f"👤 {member}:\n"
                    formatted += f"   Tracked: {stats.get('human_tracked', stats.get('human_time', 'N/A'))}\n"
                    formatted += f"   Estimated: {stats.get('human_est', 'N/A')}\n"
                    formatted += f"   Tasks: {stats.get('tasks', 'N/A')}\n\n"
                return formatted, data

        elif tool_name == "get_project_weekly_digest":
            if isinstance(data, dict):
                formatted = "📊 WEEKLY DIGEST:\n\n"
                formatted += f"Summary: {data.get('summary', 'N/A')}\n\n"
                if "key_metrics" in data:
                    formatted += "Key Metrics:\n"
                    for k, v in data["key_metrics"].items():
                        formatted += f"  • {k}: {v}\n"
                return formatted, data

        elif tool_name == "list_mapped_projects" or tool_name == "list_projects":
            if isinstance(data, list):
                formatted = "🗺️  MAPPED PROJECTS:\n"
                for proj in data:
                    proj_name = proj.get("alias", proj.get("name", "Unknown"))
                    proj_type = proj.get("type", proj.get("clickup_type", "N/A"))
                    formatted += f"  • {proj_name} (Type: {proj_type})\n"
                return formatted, data

        elif tool_name == "discover_hierarchy":
            if isinstance(data, dict) and "hierarchy" in data.get("data", data):
                hierarchy = data.get("data", data).get("hierarchy", [])
                formatted = "🌳 WORKSPACE HIERARCHY:\n\n"
                for space in hierarchy:
                    formatted += f"📦 {space.get('name')}\n"
                    for folder in space.get("folders", []):
                        formatted += f"  📁 {folder.get('name')}\n"
                        for lst in folder.get("lists", []):
                            formatted += f"    📝 {lst.get('name')}\n"
                    for lst in space.get("folderless_lists", []):
                        formatted += f"  📝 {lst.get('name')} (no folder)\n"
                return formatted, data

        elif tool_name == "get_project_health_score":
            if isinstance(data, dict):
                formatted = f"💊 HEALTH SCORE: {data.get('score', 'N/A')}/100\n"
                formatted += f"Grade: {data.get('grade', 'N/A')}\n\n"
                if "breakdown" in data:
                    formatted += "Component Breakdown:\n"
                    for k, v in data["breakdown"].items():
                        formatted += f"  • {k}: {v}%\n"
                if "recommendations" in data:
                    formatted += "\nRecommendations:\n"
                    for rec in data["recommendations"]:
                        formatted += f"  • {rec}\n"
                return formatted, data

        # Default formatting
        if verbose:
            formatted = json.dumps(data, indent=2)
        else:
            formatted = json.dumps(data, indent=2)[:800] + "..."
        return formatted, data

    except Exception:
        return str(data)[:500], data


def parse_tool_calls(text):
    """Parse XML-style tool calls from model response"""
    tool_calls = []
    pattern = r"<tool_call>\s*<name>([^<]+)</name>\s*<arguments>(.*?)</arguments>\s*</tool_call>"
    matches = re.finditer(pattern, text, re.DOTALL | re.IGNORECASE)

    for match in matches:
        tool_name = match.group(1).strip()
        args_str = match.group(2).strip()

        try:
            if args_str and args_str not in ["{}", ""]:
                arguments = json.loads(args_str)
            else:
                arguments = {}
        except json.JSONDecodeError:
            print(f"⚠️  Warning: Failed to parse arguments for {tool_name}: {args_str}")
            arguments = {}

        tool_calls.append({"name": tool_name, "arguments": arguments})

    return tool_calls


# ============================================================================
# SESSION TRACKER
# ============================================================================


class SessionTracker:
    """Track API usage and metrics"""

    def __init__(self):
        self.request_count = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.tool_calls_made = 0

    def log_api_call(self, response):
        """Log API call"""
        self.request_count += 1
        if hasattr(response, "usage") and response.usage:
            usage = response.usage
            input_tokens = getattr(usage, "prompt_tokens", 0)
            output_tokens = getattr(usage, "completion_tokens", 0)
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens

    def log_tool_call(self):
        """Log tool call"""
        self.tool_calls_made += 1

    def summary(self):
        """Print summary"""
        print("\n" + "=" * 70)
        print("SESSION SUMMARY")
        print("=" * 70)
        print(f"API Calls: {self.request_count}")
        print(f"Tool Calls: {self.tool_calls_made}")
        print(
            f"Total Tokens: {self.total_input_tokens + self.total_output_tokens:,} "
            f"({self.total_input_tokens:,} in, {self.total_output_tokens:,} out)"
        )
        print("=" * 70)


# ============================================================================
# MAIN CLIENT
# ============================================================================


async def initialize_workspace(session, workspace_context):
    """Initialize workspace context automatically"""
    try:
        print("\n🔄 Initializing workspace context...")

        # Get workspaces
        workspaces_result = await session.call_tool("get_workspaces", {})
        workspaces_data = json.loads(workspaces_result.content[0].text)

        if not workspaces_data:
            print("❌ No workspaces found")
            return False

        # Use first workspace
        workspace = workspaces_data[0]
        workspace_id = workspace.get("workspace_id", workspace.get("id"))
        workspace_name = workspace.get("name")

        workspace_context.set_workspace(workspace_id, workspace_name)

        # Load spaces
        spaces_result = await session.call_tool(
            "get_spaces", {"workspace_id": workspace_id}
        )
        spaces_data = json.loads(spaces_result.content[0].text)

        for space in spaces_data:
            workspace_context.add_space(space["name"], space["space_id"])

        print(f"✓ Loaded {len(spaces_data)} spaces")

        # Load mapped projects
        try:
            projects_result = await session.call_tool("list_mapped_projects", {})
            projects_data = json.loads(projects_result.content[0].text)

            for proj in projects_data:
                proj_name = proj.get("alias", proj.get("name"))
                proj_type = proj.get("type", proj.get("clickup_type"))
                proj_id = proj.get("clickup_id", proj.get("id"))
                workspace_context.add_mapped_project(proj_name, proj_type, proj_id)

            print(f"✓ Loaded {len(projects_data)} mapped projects")
        except Exception:
            pass  # Mapped projects might not exist yet

        workspace_context.hierarchy_loaded = True
        print("✅ Workspace context initialized!\n")
        return True

    except Exception as e:
        print(f"❌ Failed to initialize workspace: {e}")
        return False


async def run_mcp_client():
    """Main client with comprehensive tool support"""

    async with sse_client(MCP_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # List tools
            tools_result = await session.list_tools()
            tools = tools_result.tools

            print("\n" + "=" * 70)
            print("ClickUp MCP Client - PRODUCTION VERSION")
            print("=" * 70)
            print(f"✓ Connected to MCP server: {len(tools)} tools available")
            print(f"✓ LM Studio model: {LM_STUDIO_MODEL}")
            print("=" * 70)

            # Initialize workspace context
            workspace_context = WorkspaceContext()
            tracker = SessionTracker()

            # Auto-initialize workspace
            await initialize_workspace(session, workspace_context)

            # Create system prompt
            system_prompt = create_comprehensive_system_prompt(workspace_context)
            conversation_history = [{"role": "system", "content": system_prompt}]

            print("\n💡 Ready! Ask me anything about your ClickUp data.")
            print(
                "   Examples: 'Show time report for AI space', 'Team workload for Luminique', 'Weekly digest'\n"
            )
            print("Type 'quit' to exit, 'context' to see workspace state\n")

            # Interactive loop
            while True:
                try:
                    user_input = input("You: ").strip()

                    if not user_input:
                        continue

                    if user_input.lower() in ["quit", "exit", "q"]:
                        tracker.summary()
                        print("\n👋 Goodbye!\n")
                        break

                    if user_input.lower() == "context":
                        summary = workspace_context.get_summary()
                        print("\n📊 WORKSPACE CONTEXT:")
                        for key, value in summary.items():
                            print(f"   {key}: {value}")
                        print()
                        continue

                    # Add user message
                    conversation_history.append({"role": "user", "content": user_input})

                    # Multi-turn agentic loop
                    max_iterations = 20
                    iteration = 0

                    while iteration < max_iterations:
                        iteration += 1

                        # Call LM Studio
                        response = client.chat.completions.create(
                            model=LM_STUDIO_MODEL,
                            messages=conversation_history,
                            temperature=0.2,
                            max_tokens=2048,
                        )

                        if iteration == 1:
                            tracker.log_api_call(response)

                        message = response.choices[0].message
                        assistant_response = message.content or ""

                        # Parse for tool calls
                        tool_calls = parse_tool_calls(assistant_response)

                        if not tool_calls:
                            # Final answer - display it
                            print(f"\n🤖 Assistant:\n{assistant_response}\n")
                            conversation_history.append(
                                {"role": "assistant", "content": assistant_response}
                            )
                            break

                        # Execute tool calls
                        if iteration == 1:
                            print(f"\n🔧 Processing {len(tool_calls)} tool call(s)...")

                        tool_results = []
                        for tc in tool_calls:
                            tool_name = tc["name"]
                            tool_args = tc["arguments"]

                            tracker.log_tool_call()
                            print(f"   → {tool_name}(...)")

                            try:
                                result = await session.call_tool(tool_name, tool_args)

                                # Extract result
                                if (
                                    isinstance(result.content, list)
                                    and len(result.content) > 0
                                ):
                                    if hasattr(result.content[0], "text"):
                                        raw_result = result.content[0].text
                                    else:
                                        raw_result = json.dumps(result.content)
                                else:
                                    raw_result = str(result.content)

                                # Format for display and LLM
                                display_text, parsed_data = format_tool_result(
                                    tool_name, raw_result
                                )

                                tool_results.append(
                                    {
                                        "tool": tool_name,
                                        "result": raw_result,
                                        "display": display_text,
                                        "parsed": parsed_data,
                                        "success": True,
                                    }
                                )

                                # Update context if needed
                                if tool_name == "map_project" and isinstance(
                                    parsed_data, dict
                                ):
                                    alias = parsed_data.get("alias")
                                    entity_type = parsed_data.get("clickup_type")
                                    entity_id = parsed_data.get("clickup_id")
                                    if alias and entity_type and entity_id:
                                        workspace_context.add_mapped_project(
                                            alias, entity_type, entity_id
                                        )

                            except Exception as e:
                                error_msg = f"Error: {str(e)}"
                                print(f"      ✗ {error_msg}")
                                tool_results.append(
                                    {
                                        "tool": tool_name,
                                        "result": error_msg,
                                        "display": error_msg,
                                        "parsed": None,
                                        "success": False,
                                    }
                                )

                        # Add assistant response to history
                        conversation_history.append(
                            {"role": "assistant", "content": assistant_response}
                        )

                        # Build results message for LLM
                        results_message = "TOOL RESULTS:\n\n"
                        for tr in tool_results:
                            status = "✓ SUCCESS" if tr["success"] else "✗ FAILED"
                            results_message += (
                                f"{tr['tool']} ({status}):\n{tr['result']}\n\n"
                            )

                        # Check if all succeeded
                        all_success = all(tr["success"] for tr in tool_results)

                        if all_success:
                            results_message += "\nAll tools executed successfully. Now provide the final formatted answer to the user based on these results. Do NOT just list the data - interpret it and present insights in a clear, readable format."
                        else:
                            results_message += (
                                "\nSome tools failed. Report the error to the user."
                            )

                        conversation_history.append(
                            {"role": "user", "content": results_message}
                        )

                    if iteration >= max_iterations:
                        print(
                            "\n⚠️  Max iterations reached - response may be incomplete\n"
                        )

                except KeyboardInterrupt:
                    tracker.summary()
                    print("\n\n👋 Goodbye!\n")
                    break
                except Exception as e:
                    print(f"\n❌ Error: {str(e)}\n")
                    import traceback

                    traceback.print_exc()


if __name__ == "__main__":
    print("\n🚀 Starting ClickUp MCP Client...")
    print("📋 Ensure LM Studio is running with gemma-3-4b loaded")
    print("📋 Ensure MCP server is running on port 8001\n")

    try:
        asyncio.run(run_mcp_client())
    except KeyboardInterrupt:
        print("\n\n👋 Goodbye!\n")
