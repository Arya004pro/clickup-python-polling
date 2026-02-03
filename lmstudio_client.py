"""
LM Studio MCP Client - Prompt-Based Tool Calling for gemma-3-4b
Uses XML-style tags to teach gemma-3-4b how to call MCP tools reliably.
"""

import asyncio
import os
import json
import re
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
    """
    Parse XML-style tool calls from model response.

    Expected format:
    <tool_call>
    <name>tool_name</name>
    <arguments>{"key": "value"}</arguments>
    </tool_call>

    Returns list of {'name': str, 'arguments': dict, 'raw': str}
    """
    tool_calls = []

    # Regex to match tool_call blocks
    pattern = r"<tool_call>\s*<name>([^<]+)</name>\s*<arguments>(.*?)</arguments>\s*</tool_call>"
    matches = re.finditer(pattern, text, re.DOTALL | re.IGNORECASE)

    for match in matches:
        tool_name = match.group(1).strip()
        args_str = match.group(2).strip()

        # Parse JSON arguments
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


def format_result_human_readable(tool_name, result_data):
    """
    Format tool results in human-readable format for non-technical users.
    """
    try:
        # Parse if it's JSON string
        if isinstance(result_data, str):
            try:
                data = json.loads(result_data)
            except:
                return result_data
        else:
            data = result_data

        # Format based on tool type
        if tool_name == "get_workspaces":
            if isinstance(data, list) and len(data) > 0:
                ws = data[0]
                return f"Workspace: {ws.get('name', 'Unknown')} (ID: {ws.get('id', 'N/A')})"
            return str(data)

        elif tool_name == "get_spaces":
            if isinstance(data, list):
                output = "Spaces in Workspace:\n"
                for i, space in enumerate(data, 1):
                    name = space.get("name", "Unknown")
                    space_id = space.get("space_id", "N/A")
                    color = space.get("color", "#gray")
                    lists = space.get("list_count", 0)
                    archived = " (Archived)" if space.get("is_archived") else ""
                    output += f"\n{i}. {name} (ID: {space_id}){archived}\n"
                    output += f"   Color: {color} | Lists: {lists}\n"
                return output
            return str(data)

        elif tool_name == "get_lists":
            if isinstance(data, list):
                output = "Lists/Folders:\n"
                for i, lst in enumerate(data, 1):
                    name = lst.get("name", "Unknown")
                    list_id = lst.get("list_id", "N/A")
                    tasks = lst.get("task_count", 0)
                    output += f"\n{i}. {name} (ID: {list_id}) - {tasks} tasks\n"
                return output
            return str(data)

        elif tool_name == "get_tasks":
            if isinstance(data, list):
                output = "Tasks:\n"
                for i, task in enumerate(data, 1):
                    name = task.get("name", "Unknown")
                    task_id = task.get("id", "N/A")
                    status = task.get("status", "Unknown")
                    assignee = task.get("assignee", {})
                    assignee_name = (
                        assignee.get("username", "Unassigned")
                        if assignee
                        else "Unassigned"
                    )
                    output += f"\n{i}. {name} (ID: {task_id})\n"
                    output += f"   Status: {status} | Assigned to: {assignee_name}\n"
                return output
            return str(data)

        elif tool_name == "list_mapped_projects":
            if isinstance(data, list):
                output = "Mapped Projects:\n"
                for i, proj in enumerate(data, 1):
                    name = proj.get("name", "Unknown")
                    proj_id = proj.get("id", "N/A")
                    output += f"\n{i}. {name} (ID: {proj_id})\n"
                return output
            return str(data)

        # Default formatting for unknown tools
        if isinstance(data, list):
            output = f"Results from {tool_name}:\n"
            for i, item in enumerate(data[:10], 1):  # Show first 10 items
                if isinstance(item, dict):
                    item_name = item.get("name") or item.get("title") or str(item)
                    output += f"{i}. {item_name}\n"
                else:
                    output += f"{i}. {item}\n"
            if len(data) > 10:
                output += f"\n... and {len(data) - 10} more items"
            return output
        elif isinstance(data, dict):
            output = f"Result from {tool_name}:\n"
            for key, value in list(data.items())[:10]:
                output += f"  {key}: {value}\n"
            if len(data) > 10:
                output += f"  ... and {len(data) - 10} more fields"
            return output

        return str(data)
    except Exception as e:
        return str(data)


async def run_mcp_client():
    """Main client function with prompt-based tool calling."""

    async with sse_client(MCP_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # List available tools
            tools_result = await session.list_tools()
            tools = tools_result.tools

            print("\n" + "=" * 70)
            print("ClickUp MCP Client - Prompt-Based Tool Calling (gemma-3-4b)")
            print("=" * 70)
            print(f"Connected to MCP server: {len(tools)} tools available")
            print(f"LM Studio model: {LM_STUDIO_MODEL}")
            print("\nType your questions or 'quit' to exit\n")

            # Build tool descriptions
            tool_descriptions = []
            for tool in tools:
                desc = f"- {tool.name}: {tool.description}"

                # Add parameter info if available
                if hasattr(tool, "inputSchema") and "properties" in tool.inputSchema:
                    params = []
                    for param_name, param_info in tool.inputSchema[
                        "properties"
                    ].items():
                        param_type = param_info.get("type", "any")
                        param_desc = param_info.get("description", "")
                        params.append(
                            f"    * {param_name} ({param_type}): {param_desc}"
                        )

                    if params:
                        desc += "\n" + "\n".join(params)

                tool_descriptions.append(desc)

            # Create system prompt with STRICT tool calling instructions
            system_prompt = f"""You are an AI assistant with access to {len(tools)} ClickUp project management tools.

CRITICAL RULES:
1. NEVER make up or guess data - you MUST use tools to get real information
2. When asked about ClickUp data, ALWAYS call the appropriate tool first
3. To call a tool, use this EXACT format:

<tool_call>
<name>tool_name_here</name>
<arguments>{{"param": "value"}}</arguments>
</tool_call>

4. Use {{}} for empty arguments if tool needs no parameters
5. You can call multiple tools in one response if needed
6. After I show you the tool results, answer the user's question using that REAL data

AVAILABLE TOOLS:
{chr(10).join(tool_descriptions[:30])}
{"... and " + str(len(tools) - 30) + " more tools" if len(tools) > 30 else ""}

EXAMPLE CONVERSATION:
User: "List all mapped projects"
You: I'll fetch the mapped projects for you.
<tool_call>
<name>list_mapped_projects</name>
<arguments>{{}}</arguments>
</tool_call>

User: "Get details for project named Luminique"
You: Let me get the details for the Luminique project.
<tool_call>
<name>get_project_details</name>
<arguments>{{"project_name": "Luminique"}}</arguments>
</tool_call>

Remember: ALWAYS use tools. NEVER invent data."""

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

                    # Add user message
                    conversation_history.append({"role": "user", "content": user_input})

                    # Multi-turn loop: model may need several iterations
                    max_iterations = 8
                    iteration = 0

                    while iteration < max_iterations:
                        iteration += 1

                        # Call LM Studio (NO function calling parameters - just chat)
                        response = client.chat.completions.create(
                            model=LM_STUDIO_MODEL,
                            messages=conversation_history,
                            temperature=0.2,  # Low temp for predictable tool calls
                            max_tokens=2000,
                        )

                        if iteration == 1:
                            logger.log(response)

                        message = response.choices[0].message
                        assistant_response = message.content or ""

                        # Parse response for tool calls
                        tool_calls = parse_tool_calls(assistant_response)

                        if not tool_calls:
                            # No tool calls detected - this is the final answer
                            print(f"\nAssistant: {assistant_response}\n")
                            conversation_history.append(
                                {"role": "assistant", "content": assistant_response}
                            )
                            break

                        # Tool calls detected - execute them
                        print(f"\n[Detected {len(tool_calls)} tool call(s)]")

                        tool_results = []
                        for tc in tool_calls:
                            tool_name = tc["name"]
                            tool_args = tc["arguments"]

                            print(f"  → Calling: {tool_name}({json.dumps(tool_args)})")

                            try:
                                result = await session.call_tool(tool_name, tool_args)

                                # Format result - keep raw for model, display formatted for user
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

                                # Format for display
                                display_result = format_result_human_readable(
                                    tool_name, raw_result
                                )
                                print(f"    ✓ Result received\n")

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
                                print(f"    ✗ {error_msg}")
                                tool_results.append(
                                    {
                                        "tool": tool_name,
                                        "result": error_msg,
                                        "display": error_msg,
                                        "success": False,
                                    }
                                )

                        # Display human-readable results to user
                        print("\n" + "=" * 60)
                        for tr in tool_results:
                            print(tr["display"])
                        print("=" * 60 + "\n")

                        # Add model's response (with tool calls) to history
                        conversation_history.append(
                            {"role": "assistant", "content": assistant_response}
                        )

                        # Format tool results for model (use raw data)
                        results_message = "TOOL RESULTS:\n\n"
                        for tr in tool_results:
                            status = "SUCCESS" if tr["success"] else "FAILED"
                            results_message += (
                                f"=== {tr['tool']} ({status}) ===\n{tr['result']}\n\n"
                            )

                        results_message += "Now answer the user's original question using this REAL data. Do NOT call tools again."

                        # Add tool results as user message (simulating tool system)
                        conversation_history.append(
                            {"role": "user", "content": results_message}
                        )

                        print()  # Blank line before next iteration

                    if iteration >= max_iterations:
                        print("[Warning: Max iterations reached]\n")

                except KeyboardInterrupt:
                    logger.summary()
                    print("\n\nGoodbye!\n")
                    break
                except Exception as e:
                    print(f"\nError: {str(e)}\n")
                    import traceback

                    traceback.print_exc()


if __name__ == "__main__":
    print("\nStarting LM Studio MCP Client with Prompt-Based Tool Calling...")
    print("Ensure LM Studio is running with gemma-3-4b loaded!")
    print("Ensure MCP server is running on port 8001\n")
    asyncio.run(run_mcp_client())
