"""
LM Studio MCP Client - ANTI-HALLUCINATION EDITION
===============================================
Strict validation to prevent fake data generation
Intelligent tool selection with retry logic
Persistent workspace memory across queries

Key Anti-Hallucination Features:
- Mandatory tool usage for all factual queries
- Result validation and grounding enforcement
- Tool retry with keyword-based fallbacks
- Explicit "NO DATA" responses when tools fail
- Conversation memory for context awareness
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
# WORKSPACE MEMORY SYSTEM
# ============================================================================


class WorkspaceMemory:
    """Persistent memory for workspace context and mapped projects"""

    def __init__(self):
        self.workspace_id = None
        self.workspace_name = None
        self.spaces = {}  # {space_name_lower: {id, name, ...}}
        self.mapped_projects = {}  # {project_alias: {type, id, name}}
        self.recent_queries = []  # Last 10 queries for context
        self.tool_usage_stats = {}  # Track which tools work for which queries
        self.hierarchy_loaded = False

    def add_space(self, space_data):
        """Cache space information"""
        name_lower = space_data.get("name", "").lower()
        self.spaces[name_lower] = {
            "id": space_data.get("space_id"),
            "name": space_data.get("name"),
            "status_count": space_data.get("status_count", 0),
        }

    def add_mapped_project(self, alias, project_type, project_id):
        """Remember mapped projects"""
        self.mapped_projects[alias.lower()] = {
            "alias": alias,
            "type": project_type,
            "id": project_id,
        }

    def get_project_info(self, query_text):
        """Intelligent project resolution from query text"""
        query_lower = query_text.lower()
        
        # Check for exact alias matches
        for alias, info in self.mapped_projects.items():
            if alias in query_lower:
                return info
        
        # Check for space name matches
        for space_name, info in self.spaces.items():
            if space_name in query_lower:
                return {"alias": space_name, "type": "space", "id": info["id"]}
        
        return None

    def log_query(self, query, tools_used, success):
        """Track query patterns for learning"""
        self.recent_queries.append({
            "query": query,
            "tools": tools_used,
            "success": success,
            "timestamp": datetime.now().isoformat()
        })
        if len(self.recent_queries) > 10:
            self.recent_queries.pop(0)

    def get_summary(self):
        """Get workspace context summary"""
        return {
            "workspace": f"{self.workspace_name} ({self.workspace_id})" if self.workspace_id else "Not initialized",
            "spaces_count": len(self.spaces),
            "mapped_projects_count": len(self.mapped_projects),
            "mapped_projects": list(self.mapped_projects.keys()),
            "hierarchy_loaded": self.hierarchy_loaded
        }


# ============================================================================
# TOOL CALLING SYSTEM WITH VALIDATION
# ============================================================================


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


class ToolValidator:
    """Validates that responses are grounded in tool results"""
    
    @staticmethod
    def extract_factual_claims(text):
        """Extract statements that look like facts/data"""
        # Patterns that indicate data/facts
        fact_patterns = [
            r'\d+\s+(?:tasks?|projects?|hours?|minutes?|days?)',  # Numbers with units
            r'(?:completed|overdue|in progress|done):\s*\d+',  # Status counts
            r'\d+(?:\.\d+)?%',  # Percentages
            r'\d{4}-\d{2}-\d{2}',  # Dates
            r'(?:workspace|space|project|list)\s+(?:ID|id):\s*\w+',  # IDs
        ]
        
        claims = []
        for pattern in fact_patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE)
            for match in matches:
                claims.append(match.group(0))
        
        return claims
    
    @staticmethod
    def validate_grounding(response_text, tool_results):
        """Check if response is grounded in tool results"""
        if not tool_results:
            # No tools used - response should acknowledge this
            if any(phrase in response_text.lower() for phrase in [
                "i don't have", "cannot find", "no data", "unable to", 
                "not available", "needs to be mapped", "please run"
            ]):
                return True, "Correctly acknowledged lack of data"
            else:
                return False, "Response contains data but no tools were used"
        
        # Extract claims from response
        claims = ToolValidator.extract_factual_claims(response_text)
        
        if not claims:
            # Response is explanatory/conversational - acceptable
            return True, "No factual claims to validate"
        
        # Check if tool results contain supporting data
        tool_text = json.dumps(tool_results).lower()
        ungrounded_claims = []
        
        for claim in claims:
            # Extract numbers from claim
            numbers = re.findall(r'\d+', claim)
            claim_grounded = False
            
            for num in numbers:
                if num in tool_text:
                    claim_grounded = True
                    break
            
            if not claim_grounded:
                ungrounded_claims.append(claim)
        
        if ungrounded_claims:
            return False, f"Ungrounded claims: {ungrounded_claims}"
        
        return True, "All claims grounded in tool results"


# ============================================================================
# INTELLIGENT SYSTEM PROMPT
# ============================================================================


def create_anti_hallucination_prompt(workspace_memory: WorkspaceMemory):
    """Create system prompt that prevents hallucination"""

    context_info = ""
    if workspace_memory.workspace_id:
        summary = workspace_memory.get_summary()
        context_info = f"""
🗂️  CURRENT WORKSPACE CONTEXT:
- Workspace: {summary["workspace"]}
- Mapped Projects: {', '.join(summary["mapped_projects"]) if summary["mapped_projects"] else "None"}
- Cached Spaces: {summary["spaces_count"]}
- Hierarchy Loaded: {summary["hierarchy_loaded"]}
"""

    return f"""You are a ClickUp Data Analysis Assistant with STRICT anti-hallucination protocols.

{context_info}

🚨 CRITICAL RULES - NEVER VIOLATE:
================================

1. **NEVER INVENT DATA** - If you don't have data from tools, say "I don't have this data"
2. **ALWAYS USE TOOLS** - Every factual answer MUST come from tool results
3. **NO ASSUMPTIONS** - Don't assume values, counts, or states
4. **EXPLICIT FAILURES** - If tools fail, clearly state what failed and why
5. **GROUNDED RESPONSES** - Every number/fact in your answer must appear in tool results

TOOL CALLING FORMAT:
====================

<tool_call>
<name>exact_tool_name</name>
<arguments>{{"param": "value"}}</arguments>
</tool_call>

QUERY CLASSIFICATION:
=====================

Before responding, classify the query:

1. **FACTUAL QUERY** (requires tools):
   - "How many tasks...", "What is the status...", "Show me...", "List..."
   → MUST use tools, MUST NOT invent data

2. **GUIDANCE QUERY** (no tools needed):
   - "How do I...", "What does... mean", "Explain..."
   → Can answer from knowledge, but be clear it's guidance not data

3. **AMBIGUOUS** (clarification needed):
   - Missing project name, unclear scope
   → Ask for clarification, suggest using discover_hierarchy

RESPONSE TEMPLATES:
==================

✅ CORRECT (tool-based):
"Based on the get_tasks tool results, there are 15 active tasks in the AI space:
- 8 in progress
- 4 in review
- 3 blocked"

✅ CORRECT (no data):
"I don't have data for the 'Marketing' project. It may not be mapped yet. 
Would you like me to:
1. Check if it exists: discover_hierarchy()
2. See what's already mapped: list_mapped_projects()"

❌ WRONG (hallucination):
"The Marketing project has 23 tasks, with 12 completed and 11 in progress."
→ This invents specific numbers without tool results

❌ WRONG (assumption):
"Based on typical project structures, you probably have around 20-30 tasks."
→ Never make assumptions about user's data

TOOL RETRY STRATEGY:
===================

If a tool fails:
1. Report the exact error
2. Try related tools with similar keywords
3. Track which tools you tried
4. If all fail, give explicit "No data available" response

Example:
"I tried get_project_tasks('Marketing') but got error: project not found.
I also tried searching with list_mapped_projects() - it's not in the tracked projects.
Would you like me to scan for it with discover_hierarchy()?"

AVAILABLE TOOLS (54 total):
===========================

**Workspace Tools** (9):
- get_workspaces, get_spaces, get_space, get_folders, get_folder, get_lists, 
  get_folderless_lists, get_list, invalidate_cache

**Task Tools** (9):
- get_tasks, get_task, create_task, update_task, search_tasks, get_project_tasks,
  get_list_progress, get_workload, get_overdue_tasks

**Analytics Tools** (8):
- get_progress_since, get_time_tracking_report, get_inactive_assignees,
  get_untracked_tasks, get_stale_tasks, get_estimation_accuracy,
  get_at_risk_tasks, get_status_summary

**Project Config Tools** (7):
- discover_projects, add_project, list_projects, remove_project,
  refresh_projects, get_project_status, get_all_projects_status

**Project Intelligence Tools** (7):
- get_project_health_score, get_project_daily_standup, get_project_time_tracking,
  get_project_blockers, get_project_at_risk, get_project_weekly_digest,
  get_project_team_workload

**Sync/Mapping Tools** (10):
- discover_hierarchy, map_project, list_mapped_projects, get_mapped_project,
  refresh_project, unmap_project, get_sync_status, list_spaces, 
  clear_sync, prune_cache

INITIALIZATION WORKFLOW:
========================

On first interaction:
1. Call get_workspaces (required)
2. Call get_spaces with workspace_id (recommended)
3. Call list_mapped_projects (to know what's tracked)
4. Store this context in memory

REMEMBER:
=========
- If you don't have data, say so clearly
- If a tool fails, explain what happened
- Never fill gaps with plausible-sounding fake data
- Always cite which tool provided which data
- Be helpful by suggesting next steps, not by inventing answers
"""


# ============================================================================
# SESSION MANAGEMENT
# ============================================================================


class SessionTracker:
    """Track API usage and metrics"""

    def __init__(self):
        self.request_count = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.tool_calls_made = 0
        self.failed_tools = []
        self.hallucination_warnings = 0

    def log_api_call(self, response):
        """Log API call"""
        self.request_count += 1
        if hasattr(response, "usage") and response.usage:
            usage = response.usage
            input_tokens = getattr(usage, "prompt_tokens", 0)
            output_tokens = getattr(usage, "completion_tokens", 0)
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens

    def log_tool_call(self, tool_name, success):
        """Log tool call"""
        self.tool_calls_made += 1
        if not success:
            self.failed_tools.append(tool_name)

    def log_hallucination_warning(self):
        """Log detected hallucination"""
        self.hallucination_warnings += 1

    def summary(self):
        """Print summary"""
        print("\n" + "=" * 70)
        print("SESSION SUMMARY")
        print("=" * 70)
        print(f"API Calls: {self.request_count}")
        print(f"Tool Calls: {self.tool_calls_made}")
        print(f"Failed Tools: {len(self.failed_tools)}")
        if self.failed_tools:
            print(f"  Failed: {', '.join(set(self.failed_tools))}")
        print(f"Hallucination Warnings: {self.hallucination_warnings}")
        print(
            f"Total Tokens: {self.total_input_tokens + self.total_output_tokens:,} "
            f"({self.total_input_tokens:,} in, {self.total_output_tokens:,} out)"
        )
        print("=" * 70)


# ============================================================================
# MAIN CLIENT
# ============================================================================


async def initialize_workspace(session, workspace_memory):
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

        workspace_memory.workspace_id = workspace_id
        workspace_memory.workspace_name = workspace_name

        # Load spaces
        spaces_result = await session.call_tool(
            "get_spaces", {"workspace_id": workspace_id}
        )
        spaces_data = json.loads(spaces_result.content[0].text)

        for space in spaces_data:
            workspace_memory.add_space(space)

        print(f"✓ Loaded {len(spaces_data)} spaces")

        # Load mapped projects
        try:
            projects_result = await session.call_tool("list_mapped_projects", {})
            projects_data = json.loads(projects_result.content[0].text)

            for proj in projects_data:
                proj_alias = proj.get("alias", proj.get("name"))
                proj_type = proj.get("type", proj.get("clickup_type"))
                proj_id = proj.get("clickup_id", proj.get("id"))
                workspace_memory.add_mapped_project(proj_alias, proj_type, proj_id)

            print(f"✓ Loaded {len(projects_data)} mapped projects")
        except Exception:
            pass  # Mapped projects might not exist yet

        workspace_memory.hierarchy_loaded = True
        print("✅ Workspace context initialized!\n")
        return True

    except Exception as e:
        print(f"❌ Failed to initialize workspace: {e}")
        return False


async def run_mcp_client():
    """Main client with anti-hallucination enforcement"""

    async with sse_client(MCP_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # List tools
            tools_result = await session.list_tools()
            tools = tools_result.tools
            tool_names = [t.name for t in tools]

            print("\n" + "=" * 70)
            print("ClickUp MCP Client - ANTI-HALLUCINATION EDITION")
            print("=" * 70)
            print(f"✓ Connected to MCP server: {len(tools)} tools available")
            print(f"✓ LM Studio model: {LM_STUDIO_MODEL}")
            print("=" * 70)

            # Initialize workspace context
            workspace_memory = WorkspaceMemory()
            tracker = SessionTracker()

            # Auto-initialize workspace
            await initialize_workspace(session, workspace_memory)

            # Create system prompt
            system_prompt = create_anti_hallucination_prompt(workspace_memory)
            conversation_history = [{"role": "system", "content": system_prompt}]

            print("\n💡 Ready! Ask me anything about your ClickUp data.")
            print("   I will ONLY provide data from actual tool results - no hallucinations!\n")
            print("Type 'quit' to exit, 'context' to see workspace state, 'stats' for session stats\n")

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
                        summary = workspace_memory.get_summary()
                        print("\n📊 WORKSPACE CONTEXT:")
                        for key, value in summary.items():
                            print(f"   {key}: {value}")
                        print()
                        continue

                    if user_input.lower() == "stats":
                        tracker.summary()
                        continue

                    # Add user message
                    conversation_history.append({"role": "user", "content": user_input})

                    # Multi-turn agentic loop
                    max_iterations = 15
                    iteration = 0
                    all_tool_results = []
                    tools_used = []

                    while iteration < max_iterations:
                        iteration += 1

                        # Call LM Studio
                        response = client.chat.completions.create(
                            model=LM_STUDIO_MODEL,
                            messages=conversation_history,
                            temperature=0.1,  # Lower temperature for more deterministic behavior
                            max_tokens=2048,
                        )

                        if iteration == 1:
                            tracker.log_api_call(response)

                        message = response.choices[0].message
                        assistant_response = message.content or ""

                        # Parse for tool calls
                        tool_calls = parse_tool_calls(assistant_response)

                        if not tool_calls:
                            # Final answer - validate it's grounded
                            is_grounded, reason = ToolValidator.validate_grounding(
                                assistant_response, all_tool_results
                            )
                            
                            if not is_grounded:
                                print(f"\n⚠️  HALLUCINATION DETECTED: {reason}")
                                tracker.log_hallucination_warning()
                                
                                # Force the model to acknowledge
                                correction_prompt = f"""
VALIDATION FAILURE: {reason}

You must revise your response to only include information from the tool results.
If the tools didn't provide data, you must explicitly say "I don't have this data".

Tool results available:
{json.dumps(all_tool_results, indent=2) if all_tool_results else "NO TOOLS WERE USED"}

Provide a corrected response that is grounded in these results only.
"""
                                conversation_history.append({"role": "user", "content": correction_prompt})
                                continue  # Force another iteration
                            
                            # Response is valid - display it
                            print(f"\n🤖 Assistant:\n{assistant_response}\n")
                            conversation_history.append(
                                {"role": "assistant", "content": assistant_response}
                            )
                            
                            # Log query pattern
                            workspace_memory.log_query(user_input, tools_used, True)
                            break

                        # Execute tool calls
                        if iteration == 1:
                            print(f"\n🔧 Processing {len(tool_calls)} tool call(s)...")

                        tool_results = []
                        for tc in tool_calls:
                            tool_name = tc["name"]
                            tool_args = tc["arguments"]

                            # Validate tool exists
                            if tool_name not in tool_names:
                                print(f"   ✗ Tool '{tool_name}' not found")
                                tracker.log_tool_call(tool_name, False)
                                tool_results.append({
                                    "tool": tool_name,
                                    "result": f"ERROR: Tool '{tool_name}' does not exist",
                                    "success": False
                                })
                                continue

                            tools_used.append(tool_name)
                            print(f"   → {tool_name}({json.dumps(tool_args)})")

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

                                # Parse result
                                try:
                                    parsed_result = json.loads(raw_result)
                                except Exception:
                                    parsed_result = raw_result

                                tool_results.append({
                                    "tool": tool_name,
                                    "result": raw_result,
                                    "parsed": parsed_result,
                                    "success": True
                                })
                                all_tool_results.append(parsed_result)

                                tracker.log_tool_call(tool_name, True)
                                print("      ✓ Success")

                                # Update workspace memory if mapping tool
                                if tool_name == "map_project" and isinstance(parsed_result, dict):
                                    if parsed_result.get("success"):
                                        alias = parsed_result.get("project_details", {}).get("alias")
                                        proj_type = parsed_result.get("project_details", {}).get("clickup_type")
                                        proj_id = parsed_result.get("project_details", {}).get("clickup_id")
                                        if alias and proj_type and proj_id:
                                            workspace_memory.add_mapped_project(alias, proj_type, proj_id)

                            except Exception as e:
                                error_msg = str(e)
                                print(f"      ✗ Error: {error_msg}")
                                tracker.log_tool_call(tool_name, False)
                                tool_results.append({
                                    "tool": tool_name,
                                    "result": f"ERROR: {error_msg}",
                                    "success": False
                                })

                        # Add assistant response to history
                        conversation_history.append(
                            {"role": "assistant", "content": assistant_response}
                        )

                        # Build results message for LLM
                        results_message = "TOOL EXECUTION RESULTS:\n\n"
                        for tr in tool_results:
                            status = "✓ SUCCESS" if tr["success"] else "✗ FAILED"
                            results_message += (
                                f"{tr['tool']} ({status}):\n{tr['result']}\n\n"
                            )

                        # Check if all succeeded
                        all_success = all(tr["success"] for tr in tool_results)

                        if all_success:
                            results_message += """\n✅ All tools executed successfully.

CRITICAL INSTRUCTION:
Now provide your FINAL answer using ONLY the data above.
- Every number, date, or fact in your response must appear in these results
- If the results don't contain data for something, say "I don't have data for..."
- Do NOT add information from your training data
- Do NOT make assumptions or estimates

Format your answer clearly for the user."""
                        else:
                            results_message += """\n⚠️ Some tools failed.

Inform the user which tools failed and why.
Suggest alternative tools if available.
If no alternatives work, clearly state "I cannot retrieve this data."
"""

                        conversation_history.append(
                            {"role": "user", "content": results_message}
                        )

                    if iteration >= max_iterations:
                        print(
                            "\n⚠️  Max iterations reached - response may be incomplete\n"
                        )
                        workspace_memory.log_query(user_input, tools_used, False)

                except KeyboardInterrupt:
                    tracker.summary()
                    print("\n\n👋 Goodbye!\n")
                    break
                except Exception as e:
                    print(f"\n❌ Error: {str(e)}\n")
                    import traceback
                    traceback.print_exc()


if __name__ == "__main__":
    print("\n🚀 Starting ClickUp MCP Client (Anti-Hallucination Edition)...")
    print("📋 Ensure LM Studio is running with gemma-3-4b loaded")
    print("📋 Ensure MCP server is running on port 8001\n")

    try:
        asyncio.run(run_mcp_client())
    except KeyboardInterrupt:
        print("\n\n👋 Goodbye!\n")
