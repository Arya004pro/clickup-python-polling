"""
Gemini 2.0 Flash MCP Client with 1M Context Window
Optimized for complex multi-turn ClickUp analytics queries
"""

import asyncio
import json
import os
from typing import Dict, List, Any
import google.generativeai as genai
from mcp import ClientSession
from mcp.client.sse import sse_client
from dotenv import load_dotenv


# --- CONVERSATION LOGGER ---
class ConversationLogger:
    """Track API requests and token usage for current conversation"""

    def __init__(self):
        self.total_requests = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.tool_calls = 0

    def log_api_call(self, response):
        """Log API response with usage info"""
        self.total_requests += 1
        # Gemini API uses usage_metadata with different field names
        if hasattr(response, "usage_metadata"):
            self.total_input_tokens += response.usage_metadata.prompt_token_count or 0
            self.total_output_tokens += (
                response.usage_metadata.candidates_token_count or 0
            )
        # Fallback for OpenAI-style responses
        elif hasattr(response, "usage"):
            self.total_input_tokens += response.usage.prompt_tokens or 0
            self.total_output_tokens += response.usage.completion_tokens or 0

    def log_tool_call(self):
        """Log a tool call"""
        self.tool_calls += 1

    def print_summary(self):
        """Print conversation statistics"""
        total_tokens = self.total_input_tokens + self.total_output_tokens
        print("\n" + "=" * 60)
        print("📊 CONVERSATION STATISTICS")
        print("=" * 60)
        print(f"   API Requests:      {self.total_requests}")
        print(f"   Tool Calls:        {self.tool_calls}")
        print(f"   Input Tokens:      {self.total_input_tokens:,}")
        print(f"   Output Tokens:     {self.total_output_tokens:,}")
        print(f"   Total Tokens Used: {total_tokens:,}")
        print("=" * 60 + "\n")

    def reset(self):
        """Reset stats for new conversation"""
        self.total_requests = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.tool_calls = 0


# Load environment variables
load_dotenv()

# --- CONFIGURATION ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
# Fallback models in order of preference (from verified available models)
FALLBACK_MODELS = [
    "gemini-2.5-flash",  # Latest, best performance
    "gemini-3-flash-preview",  # Next gen preview
    "gemini-2.5-flash-lite",  # Lighter version
    "gemini-2.0-flash",  # Stable, proven
    "gemini-exp-1206",  # Experimental fallback
]
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/sse")

# Initialize conversation logger
logger = ConversationLogger()

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)

# Initialize model with function calling enabled
generation_config = {
    "temperature": 0.1,
    "top_p": 0.95,
    "top_k": 40,
    "max_output_tokens": 8192,
}

safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

# Knowledge graph for faster lookups
SPACE_MAP = {}
FOLDER_MAP = {}
LIST_MAP = {}


async def build_knowledge_graph(session: ClientSession):
    """Build initial context maps for faster tool parameter resolution"""
    print("\n🧠 Building Knowledge Graph...")
    try:
        # Get all spaces
        result = await session.call_tool("get_spaces", arguments={})
        data = json.loads(result.content[0].text)

        if isinstance(data, list):
            for space in data:
                SPACE_MAP[space["name"].lower()] = space["space_id"]
            print(f"   ✓ Mapped {len(SPACE_MAP)} Spaces")

        # Get folders from first space (if available)
        if SPACE_MAP:
            first_space_id = list(SPACE_MAP.values())[0]
            try:
                folder_result = await session.call_tool(
                    "get_folders", arguments={"space_id": first_space_id}
                )
                folder_data = json.loads(folder_result.content[0].text)
                if isinstance(folder_data, dict) and "folders" in folder_data:
                    for folder in folder_data["folders"]:
                        FOLDER_MAP[folder["name"].lower()] = folder["id"]
                    print(f"   ✓ Mapped {len(FOLDER_MAP)} Folders")
            except:
                pass

    except Exception as e:
        print(f"   ⚠️  Auto-discovery warning: {e}")


def format_tools_for_gemini(tools_list) -> List[Dict[str, Any]]:
    """Convert MCP tools to Gemini function declarations"""
    function_declarations = []

    for tool in tools_list.tools:
        properties = tool.inputSchema.get("properties", {})
        required = tool.inputSchema.get("required", [])

        # Convert to Gemini-compatible parameter format
        # Use STRING type for all properties to avoid proto type issues
        param_defs = {}
        for param_name, param_schema in properties.items():
            param_desc = param_schema.get("description", param_name)
            param_defs[param_name] = {
                "type_": "STRING",  # Gemini expects STRING enum, not string
                "description": param_desc,
            }

        function_declarations.append(
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": {
                    "type_": "OBJECT",
                    "properties": param_defs,
                    "required": required,
                },
            }
        )

    return function_declarations


def try_create_model(model_name: str, system_instruction: str, gemini_tools: List):
    """Try to create a Gemini model, return None if quota exceeded"""
    try:
        model = genai.GenerativeModel(
            model_name=model_name,
            generation_config=generation_config,
            safety_settings=safety_settings,
            system_instruction=system_instruction,
            tools=gemini_tools,
        )
        # Test if model is accessible
        test_chat = model.start_chat(enable_automatic_function_calling=False)
        test_response = test_chat.send_message("Hi")
        print(f"✅ Using model: {model_name}")
        return model
    except Exception as e:
        error_msg = str(e)
        if "429" in error_msg or "quota" in error_msg.lower():
            print(f"⚠️  {model_name} quota exceeded, trying next model...")
            return None
        else:
            raise e


async def run_chat_loop():
    print(f"🔌 Connecting to MCP Server: {MCP_SERVER_URL}")
    print(f"🤖 Using Model: {GEMINI_MODEL} (1M context window)")

    async with sse_client(url=MCP_SERVER_URL) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            await build_knowledge_graph(session)

            # Fetch available tools
            tools_list = await session.list_tools()
            print(f"✅ Connected! Loaded {len(tools_list.tools)} MCP tools\n")

            # Convert tools to Gemini format
            gemini_tools = format_tools_for_gemini(tools_list)

            # Create system instruction
            system_instruction = f"""You are an expert ClickUp Project Analytics Assistant with access to {len(tools_list.tools)} specialized tools.

CONTEXT KNOWLEDGE:
- Available Spaces: {json.dumps(SPACE_MAP, indent=2)}
- Available Folders: {json.dumps(FOLDER_MAP, indent=2) if FOLDER_MAP else "Not yet loaded"}

CAPABILITIES:
1. Workspace Structure: Get spaces, folders, lists, and their hierarchies
2. Task Management: Query, filter, and analyze tasks by status, assignee, dates, tags
3. PM Analytics: Generate reports on task distribution, time tracking, progress metrics
4. Project Intelligence: Analyze patterns, bottlenecks, employee performance
5. Configuration: Access custom fields, statuses, tags, priorities
6. Sync Mapping: Access database-synced data for complex queries

GUIDELINES:
1. For complex queries, break them into logical tool calls
2. Always use exact IDs from context when available
3. For user-friendly names, first resolve them to IDs using list/search tools
4. Aggregate data across multiple tool calls when needed for comprehensive reports
5. Present insights clearly with metrics, percentages, and actionable summaries
6. If you need to find a space/folder/list by name, use the get_* tools first

CONVERSATION STYLE:
- Be direct and data-driven
- Use markdown tables for multi-item results
- Highlight key metrics in bold
- Provide context for numbers (e.g., "15 tasks (30% of total)")
"""

            # Initialize Gemini model with tools (try fallback models if quota exceeded)
            model = None
            models_to_try = [GEMINI_MODEL] + [
                m for m in FALLBACK_MODELS if m != GEMINI_MODEL
            ]

            for model_name in models_to_try:
                print(f"🔄 Trying model: {model_name}...")
                model = try_create_model(model_name, system_instruction, gemini_tools)
                if model:
                    break

            if not model:
                print(
                    "\n❌ All models quota exceeded. Please try again later or upgrade to paid tier."
                )
                print("   Check quota at: https://ai.dev/rate-limit")
                return

            # Start chat session
            chat = model.start_chat(enable_automatic_function_calling=False)

            print("━" * 60)
            print("🚀 Gemini 2.0 Flash Ready! (Type 'quit' to exit)")
            print("💡 Try: 'Show me all tasks in progress assigned to John'")
            print("━" * 60)

            while True:
                user_input = input("\n📊 You: ")
                if user_input.lower() in ["quit", "exit", "q"]:
                    logger.print_summary()
                    print("👋 Goodbye!")
                    break

                print("\n🤔 Analyzing...")

                try:
                    # Send message to Gemini
                    response = chat.send_message(user_input)
                    logger.log_api_call(response)

                    # Handle function calling loop
                    max_iterations = 15  # Prevent infinite loops
                    iteration = 0

                    while iteration < max_iterations:
                        iteration += 1

                        # Check if model wants to call functions
                        if response.candidates[0].content.parts[0].function_call:
                            function_calls = []

                            # Collect all function calls from this response
                            for part in response.candidates[0].content.parts:
                                if hasattr(part, "function_call"):
                                    function_calls.append(part.function_call)

                            # Execute all function calls
                            function_responses = []

                            for fc in function_calls:
                                tool_name = fc.name
                                tool_args = dict(fc.args)

                                print(
                                    f"   🔧 Tool: {tool_name}({json.dumps(tool_args, indent=2)})"
                                )

                                try:
                                    # Call MCP tool
                                    logger.log_tool_call()
                                    result = await session.call_tool(
                                        tool_name, arguments=tool_args
                                    )
                                    tool_output = (
                                        result.content[0].text
                                        if result.content
                                        else "{}"
                                    )

                                    # Parse and optionally truncate
                                    try:
                                        parsed = json.loads(tool_output)
                                        # For very large responses, truncate but keep structure
                                        if len(tool_output) > 50000:
                                            if (
                                                isinstance(parsed, list)
                                                and len(parsed) > 50
                                            ):
                                                parsed = parsed[:50]
                                                parsed.append(
                                                    {
                                                        "_truncated": f"Showing first 50 of {len(parsed)} items"
                                                    }
                                                )
                                            tool_output = json.dumps(parsed, indent=2)
                                            print(f"      ⚠️  Large response truncated")
                                    except:
                                        pass

                                    print(f"      ✓ Success ({len(tool_output)} chars)")

                                    function_responses.append(
                                        genai.protos.Part(
                                            function_response=genai.protos.FunctionResponse(
                                                name=tool_name,
                                                response={"result": tool_output},
                                            )
                                        )
                                    )

                                except Exception as e:
                                    print(f"      ❌ Error: {e}")
                                    function_responses.append(
                                        genai.protos.Part(
                                            function_response=genai.protos.FunctionResponse(
                                                name=tool_name,
                                                response={"error": str(e)},
                                            )
                                        )
                                    )

                            # Send function results back to model
                            response = chat.send_message(function_responses)
                            logger.log_api_call(response)

                        else:
                            # Model has final answer
                            answer = response.text
                            print(f"\n🤖 Assistant:\n{answer}\n")
                            break

                    if iteration >= max_iterations:
                        print(
                            "\n⚠️  Maximum tool iterations reached. Conversation reset."
                        )
                        chat = model.start_chat(enable_automatic_function_calling=False)

                except Exception as e:
                    print(f"\n❌ Error: {e}")
                    print(
                        f"   Tip: Try rephrasing your question or breaking it into smaller parts"
                    )


async def test_connection():
    """Quick test to verify MCP server connectivity"""
    print("🔍 Testing MCP Server Connection...")
    try:
        async with sse_client(url=MCP_SERVER_URL) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                print(f"✅ Connection successful! Found {len(tools.tools)} tools")
                return True
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        print(f"   Make sure MCP server is running on {MCP_SERVER_URL}")
        return False


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  GEMINI 2.0 FLASH - CLICKUP MCP CLIENT")
    print("  1M Context Window | Function Calling Enabled")
    print("=" * 60 + "\n")

    if not GEMINI_API_KEY:
        print("❌ Error: GEMINI_API_KEY not found in .env file")
        print("   Add your API key: GEMINI_API_KEY=your_key_here")
        exit(1)

    # Test connection first
    if asyncio.run(test_connection()):
        print("\n🚀 Starting chat interface...\n")
        asyncio.run(run_chat_loop())
