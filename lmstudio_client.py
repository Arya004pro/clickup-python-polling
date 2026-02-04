"""
LM Studio MCP Client - IMPROVED VERSION
Structured Report Generation with Step-by-Step Instructions
Optimized for gemma-3-4b with minimal hallucination
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
# STRUCTURED REPORT WORKFLOWS
# ============================================================================

REPORT_WORKFLOWS = {
    "space_time_report": {
        "name": "Space-wise Time Entry Report",
        "description": "Generate time tracking report grouped by Space",
        "steps": [
            {
                "step": 1,
                "action": "get_workspaces",
                "description": "First, fetch all workspaces to get workspace_id",
                "params": {},
            },
            {
                "step": 2,
                "action": "get_spaces",
                "description": "Get all spaces in the workspace using workspace_id from step 1",
                "params": {"workspace_id": "FROM_STEP_1"},
            },
            {
                "step": 3,
                "action": "map_project",
                "description": "Map each space as a project for time tracking",
                "params": {"id": "FROM_STEP_2", "type": "space"},
            },
            {
                "step": 4,
                "action": "get_project_time_tracking",
                "description": "Get time tracking report for each mapped space",
                "params": {"project_name": "FROM_STEP_3", "group_by": "assignee"},
            },
        ],
    },
    "folder_team_time_report": {
        "name": "Space > Folder > Team Member Time Report",
        "description": "Generate hierarchical time tracking report",
        "steps": [
            {
                "step": 1,
                "action": "get_workspaces",
                "description": "Get workspace ID",
                "params": {},
            },
            {
                "step": 2,
                "action": "get_spaces",
                "description": "Get all spaces",
                "params": {"workspace_id": "FROM_STEP_1"},
            },
            {
                "step": 3,
                "action": "get_folders",
                "description": "For each space, get all folders",
                "params": {"space_id": "FROM_STEP_2"},
            },
            {
                "step": 4,
                "action": "map_project",
                "description": "Map each folder as a project",
                "params": {"id": "FROM_STEP_3", "type": "folder"},
            },
            {
                "step": 5,
                "action": "get_project_time_tracking",
                "description": "Get time report grouped by team member",
                "params": {"project_name": "FROM_STEP_4", "group_by": "assignee"},
            },
        ],
    },
    "team_member_time_report": {
        "name": "Team Member-wise Time Entry Report",
        "description": "Generate time report for all team members across all projects",
        "steps": [
            {
                "step": 1,
                "action": "list_mapped_projects",
                "description": "Get all mapped projects",
                "params": {},
            },
            {
                "step": 2,
                "action": "get_time_tracking_report",
                "description": "For each project, get time tracking grouped by assignee",
                "params": {"project": "FROM_STEP_1", "group_by": "assignee"},
            },
        ],
    },
    "weekly_report_all": {
        "name": "Weekly Report for All Report Types",
        "description": "Generate weekly digest combining all report types",
        "steps": [
            {
                "step": 1,
                "action": "list_mapped_projects",
                "description": "Get all mapped projects",
                "params": {},
            },
            {
                "step": 2,
                "action": "get_project_weekly_digest",
                "description": "Generate weekly digest for each project",
                "params": {"project_name": "FROM_STEP_1"},
            },
            {
                "step": 3,
                "action": "get_time_tracking_report",
                "description": "Get time tracking for the week",
                "params": {"project": "FROM_STEP_1", "group_by": "assignee"},
            },
        ],
    },
}


# ============================================================================
# SYSTEM PROMPT TEMPLATES
# ============================================================================


def create_structured_system_prompt(tools_list):
    """Create a highly structured system prompt with minimal hallucination"""

    return f"""You are a ClickUp Data Analysis Assistant with access to {len(tools_list)} tools.

CRITICAL RULES TO PREVENT ERRORS:
1. NEVER invent or guess data - ALWAYS use tools to get real information
2. ALWAYS call tools in the EXACT sequence specified in the workflow
3. NEVER skip steps in a workflow
4. If a tool call fails, STOP and report the error - DO NOT continue
5. Use the exact tool names and parameters as documented

TOOL CALLING FORMAT (STRICT):
To call a tool, use this EXACT XML format:

<tool_call>
<name>exact_tool_name</name>
<arguments>{{"param_name": "param_value"}}</arguments>
</tool_call>

IMPORTANT NOTES:
- Use {{}} for empty parameters if tool needs no arguments
- Always wait for tool results before proceeding
- Extract IDs carefully from previous results
- Never make assumptions about workspace/space/folder structure

AVAILABLE WORKFLOW COMMANDS:
When user requests a report, identify the workflow type and execute steps sequentially:

1. "space time report" → Execute space_time_report workflow
2. "folder team report" → Execute folder_team_time_report workflow  
3. "team member report" → Execute team_member_time_report workflow
4. "weekly report" → Execute weekly_report_all workflow

WORKFLOW EXECUTION RULES:
- Execute ONE step at a time
- Wait for confirmation before next step
- Extract required IDs from previous step results
- If a step fails, report the error and STOP

EXAMPLE CORRECT WORKFLOW:
User: "Generate space time report"

Step 1: Get workspaces
<tool_call>
<name>get_workspaces</name>
<arguments>{{}}</arguments>
</tool_call>

[Wait for result, extract workspace_id]

Step 2: Get spaces
<tool_call>
<name>get_spaces</name>
<arguments>{{"workspace_id": "extracted_id_here"}}</arguments>
</tool_call>

[Continue step by step...]

TOP 20 MOST USEFUL TOOLS FOR REPORTS:

1. get_workspaces - Get all workspaces (NO parameters)
2. get_spaces - Get spaces (workspace_id required)
3. get_folders - Get folders in space (space_id required)
4. get_folderless_lists - Get lists not in folders (space_id required)
5. list_mapped_projects - List all mapped projects (NO parameters)
6. map_project - Map space/folder/list as project (id, type required)
7. get_project_time_tracking - Time report for project (project_name, group_by)
8. get_time_tracking_report - Time report (project OR list_id, group_by)
9. get_project_weekly_digest - Weekly digest (project_name required)
10. get_workload - Workload by assignee (list_id required)
11. get_tasks - Get tasks from list (list_id required)
12. get_project_tasks - Get tasks from project (project required)
13. get_project_team_workload - Team workload (project_name required)
14. get_project_health_score - Health score (project_name required)
15. discover_hierarchy - Full workspace hierarchy (workspace_id optional)
16. get_sync_status - Check sync status (NO parameters)
17. refresh_project - Refresh project data (alias required)
18. get_progress_since - Progress since date (project/list_id, since_date)
19. get_project_daily_standup - Daily standup (project_name required)
20. get_project_blockers - Find blockers (project_name required)

REMEMBER:
- Start with get_workspaces to get workspace_id
- Then get_spaces to list all spaces
- Map entities before using project-specific tools
- Use exact IDs from tool responses
- Never guess or invent IDs
"""


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


class ConversationLogger:
    """Track API usage."""

    def __init__(self):
        self.request_count = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def log(self, response):
        """Log token usage."""
        self.request_count += 1
        if hasattr(response, "usage") and response.usage:
            usage = response.usage
            input_tokens = getattr(usage, "prompt_tokens", 0)
            output_tokens = getattr(usage, "completion_tokens", 0)
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            print(
                f"[API Call #{self.request_count}] Tokens: {input_tokens} in, {output_tokens} out"
            )

    def summary(self):
        """Print summary."""
        print("\n" + "=" * 60)
        print(f"Session Summary: {self.request_count} API calls")
        print(f"Total Tokens: {self.total_input_tokens + self.total_output_tokens:,}")
        print("=" * 60)


def parse_tool_calls(text):
    """Parse XML-style tool calls from model response."""
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
            print(f"Warning: Failed to parse arguments for {tool_name}: {args_str}")
            arguments = {}

        tool_calls.append(
            {"name": tool_name, "arguments": arguments, "raw": match.group(0)}
        )

    return tool_calls


def format_result_for_display(tool_name, result_data):
    """Format tool results in readable format."""
    try:
        if isinstance(result_data, str):
            try:
                data = json.loads(result_data)
            except Exception:
                return result_data
        else:
            data = result_data

        # Format based on tool type
        if tool_name == "get_workspaces":
            if isinstance(data, list) and len(data) > 0:
                output = "WORKSPACES:\n"
                for ws in data:
                    output += f"  • {ws.get('name', 'Unknown')} (ID: {ws.get('workspace_id', ws.get('id', 'N/A'))})\n"
                return output

        elif tool_name == "get_spaces":
            if isinstance(data, list):
                output = "SPACES:\n"
                for i, space in enumerate(data, 1):
                    output += f"  {i}. {space.get('name', 'Unknown')} (ID: {space.get('space_id', 'N/A')})\n"
                return output

        elif tool_name == "get_folders":
            if isinstance(data, list):
                output = "FOLDERS:\n"
                for folder in data:
                    output += f"  • {folder.get('name', 'Unknown')} (ID: {folder.get('folder_id', 'N/A')})\n"
                return output

        elif tool_name in ["get_project_time_tracking", "get_time_tracking_report"]:
            if isinstance(data, dict) and "report" in data:
                output = "TIME TRACKING REPORT:\n"
                for member, stats in data["report"].items():
                    output += f"\n  {member}:\n"
                    output += f"    Tracked: {stats.get('human_tracked', stats.get('time_tracked', 'N/A'))}\n"
                    output += f"    Estimated: {stats.get('human_est', stats.get('time_estimate', 'N/A'))}\n"
                    output += f"    Tasks: {stats.get('tasks', 'N/A')}\n"
                return output

        elif tool_name == "get_project_weekly_digest":
            if isinstance(data, dict):
                output = "WEEKLY DIGEST:\n"
                output += f"  Summary: {data.get('summary', 'N/A')}\n"
                if "key_metrics" in data:
                    output += "\n  Key Metrics:\n"
                    for k, v in data["key_metrics"].items():
                        output += f"    {k}: {v}\n"
                return output

        elif tool_name == "list_mapped_projects":
            if isinstance(data, list):
                output = "MAPPED PROJECTS:\n"
                for proj in data:
                    output += f"  • {proj.get('alias', proj.get('name', 'Unknown'))} "
                    output += (
                        f"(Type: {proj.get('type', proj.get('clickup_type', 'N/A'))})\n"
                    )
                return output

        # Default JSON formatting
        return json.dumps(data, indent=2)[:1000]

    except Exception:
        return str(data)[:500]


def display_workflow_menu():
    """Display available workflows to user"""
    print("\n" + "=" * 70)
    print("AVAILABLE REPORT WORKFLOWS:")
    print("=" * 70)
    for key, workflow in REPORT_WORKFLOWS.items():
        print(f"\n  Command: '{key}'")
        print(f"  Name: {workflow['name']}")
        print(f"  Description: {workflow['description']}")
        print(f"  Steps: {len(workflow['steps'])}")
    print("\n" + "=" * 70)
    print("\nType a workflow command or ask a question. Type 'menu' to see this again.")
    print("Type 'quit' to exit.\n")


# ============================================================================
# MAIN CLIENT
# ============================================================================


async def run_mcp_client():
    """Main client function with structured workflows"""

    async with sse_client(MCP_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # List available tools
            tools_result = await session.list_tools()
            tools = tools_result.tools

            print("\n" + "=" * 70)
            print("ClickUp MCP Client - STRUCTURED REPORT GENERATION")
            print("=" * 70)
            print(f"Connected to MCP server: {len(tools)} tools available")
            print(f"LM Studio model: {LM_STUDIO_MODEL}")

            # Display workflow menu
            display_workflow_menu()

            # Create system prompt
            system_prompt = create_structured_system_prompt(tools)
            conversation_history = [{"role": "system", "content": system_prompt}]
            logger = ConversationLogger()

            # Interactive loop
            while True:
                try:
                    user_input = input("You: ").strip()

                    if not user_input:
                        continue

                    if user_input.lower() in ["quit", "exit", "q"]:
                        logger.summary()
                        print("\nGoodbye!\n")
                        break

                    if user_input.lower() == "menu":
                        display_workflow_menu()
                        continue

                    # Add user message
                    conversation_history.append({"role": "user", "content": user_input})

                    # Multi-turn loop
                    max_iterations = 15  # Increased for complex workflows
                    iteration = 0

                    while iteration < max_iterations:
                        iteration += 1

                        # Call LM Studio
                        response = client.chat.completions.create(
                            model=LM_STUDIO_MODEL,
                            messages=conversation_history,
                            temperature=0.1,  # Very low for deterministic responses
                            max_tokens=2000,
                        )

                        if iteration == 1:
                            logger.log(response)

                        message = response.choices[0].message
                        assistant_response = message.content or ""

                        # Parse for tool calls
                        tool_calls = parse_tool_calls(assistant_response)

                        if not tool_calls:
                            # No tool calls - final answer
                            print(f"\nAssistant: {assistant_response}\n")
                            conversation_history.append(
                                {"role": "assistant", "content": assistant_response}
                            )
                            break

                        # Execute tool calls
                        print(
                            f"\n[Step {iteration}: Detected {len(tool_calls)} tool call(s)]"
                        )

                        tool_results = []
                        for tc in tool_calls:
                            tool_name = tc["name"]
                            tool_args = tc["arguments"]

                            print(f"  → {tool_name}({json.dumps(tool_args)})")

                            try:
                                result = await session.call_tool(tool_name, tool_args)

                                if (
                                    isinstance(result.content, list)
                                    and len(result.content) > 0
                                ):
                                    if hasattr(result.content[0], "text"):
                                        raw_result = result.content[0].text
                                    else:
                                        raw_result = json.dumps(
                                            result.content, indent=2
                                        )
                                else:
                                    raw_result = str(result.content)

                                display_result = format_result_for_display(
                                    tool_name, raw_result
                                )
                                print("    ✓ Success\n")

                                tool_results.append(
                                    {
                                        "tool": tool_name,
                                        "result": raw_result,
                                        "display": display_result,
                                        "success": True,
                                    }
                                )

                            except Exception as e:
                                error_msg = f"Error: {str(e)}"
                                print(f"    ✗ {error_msg}\n")
                                tool_results.append(
                                    {
                                        "tool": tool_name,
                                        "result": error_msg,
                                        "display": error_msg,
                                        "success": False,
                                    }
                                )

                        # Display results
                        print("\n" + "=" * 60)
                        for tr in tool_results:
                            print(tr["display"])
                        print("=" * 60 + "\n")

                        # Add to conversation history
                        conversation_history.append(
                            {"role": "assistant", "content": assistant_response}
                        )

                        # Format results for model
                        results_message = "TOOL EXECUTION RESULTS:\n\n"
                        for tr in tool_results:
                            status = "✓ SUCCESS" if tr["success"] else "✗ FAILED"
                            results_message += f"Tool: {tr['tool']} ({status})\n"
                            results_message += f"Result: {tr['result']}\n\n"

                        # Check if workflow is complete
                        all_success = all(tr["success"] for tr in tool_results)

                        if all_success:
                            results_message += "\nAll tools executed successfully. Continue to next step or provide final summary if workflow complete."
                        else:
                            results_message += "\nERROR: Some tools failed. Stop workflow and report error to user."

                        conversation_history.append(
                            {"role": "user", "content": results_message}
                        )

                        print()  # Blank line

                    if iteration >= max_iterations:
                        print(
                            "[Warning: Max iterations reached - workflow may be incomplete]\n"
                        )

                except KeyboardInterrupt:
                    logger.summary()
                    print("\n\nGoodbye!\n")
                    break
                except Exception as e:
                    print(f"\nError: {str(e)}\n")
                    import traceback

                    traceback.print_exc()


if __name__ == "__main__":
    print("\nStarting Structured ClickUp Report Generator...")
    print("Ensure LM Studio is running with gemma-3-4b loaded!")
    print("Ensure MCP server is running on port 8001\n")
    asyncio.run(run_mcp_client())
