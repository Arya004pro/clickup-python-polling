"""
Gemini MCP Client with flexible model selection
Optimized for complex multi-turn ClickUp analytics queries
"""

import asyncio
import json
import os
import time
import hashlib
from typing import Dict, List, Any, Optional
import google.generativeai as genai

# Optional protobuf JSON helpers (used when tool outputs are protobuf messages)
try:
    from google.protobuf.json_format import MessageToJson, MessageToDict
except Exception:
    MessageToJson = None
    MessageToDict = None


def proto_to_dict(obj):
    """Recursively convert protobuf objects to JSON-serializable dict/list"""
    if isinstance(obj, dict):
        return {k: proto_to_dict(v) for k, v in obj.items()}
    elif hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
        # Handle RepeatedComposite and other iterables
        return [proto_to_dict(item) for item in obj]
    elif hasattr(obj, "DESCRIPTOR"):  # Protobuf message
        if MessageToDict:
            return MessageToDict(obj, preserving_proto_field_name=True)
        return str(obj)
    else:
        return obj


# --- INTELLIGENT CACHING SYSTEM ---
class ResponseCache:
    """Cache tool outputs and model responses with TTL to reduce token usage"""

    def __init__(self, default_ttl: int = 300):  # 5 minutes default
        self.cache: Dict[str, tuple[Any, float]] = {}  # key -> (value, expiry_time)
        self.default_ttl = default_ttl
        self.hits = 0
        self.misses = 0

    def _make_key(self, tool_name: str, args: dict) -> str:
        """Generate cache key from tool name and arguments"""
        args_json = json.dumps(args, sort_keys=True, separators=(",", ":"))
        return f"{tool_name}:{hashlib.md5(args_json.encode()).hexdigest()}"

    def get(self, tool_name: str, args: dict) -> Optional[str]:
        """Get cached tool output if not expired"""
        key = self._make_key(tool_name, args)
        if key in self.cache:
            value, expiry = self.cache[key]
            if time.time() < expiry:
                self.hits += 1
                return value
            else:
                # Expired - remove from cache
                del self.cache[key]
        self.misses += 1
        return None

    def set(self, tool_name: str, args: dict, value: str, ttl: Optional[int] = None):
        """Store tool output in cache with TTL"""
        key = self._make_key(tool_name, args)
        expiry_time = time.time() + (ttl or self.default_ttl)
        self.cache[key] = (value, expiry_time)

    def invalidate(self, pattern: Optional[str] = None):
        """Invalidate cache entries matching pattern (or all if None)"""
        if pattern is None:
            count = len(self.cache)
            self.cache.clear()
            return count
        else:
            keys_to_remove = [k for k in self.cache.keys() if pattern in k]
            for key in keys_to_remove:
                del self.cache[key]
            return len(keys_to_remove)

    def get_stats(self) -> dict:
        """Get cache statistics"""
        total = self.hits + self.misses
        hit_rate = (self.hits / total * 100) if total > 0 else 0
        return {
            "hits": self.hits,
            "misses": self.misses,
            "total_requests": total,
            "hit_rate": f"{hit_rate:.1f}%",
            "cached_items": len(self.cache),
        }

    def reset_stats(self):
        """Reset hit/miss counters"""
        self.hits = 0
        self.misses = 0


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

    def log_cache_hit(self):
        """Log a cache hit (tool call avoided)"""
        self.tool_calls += 1  # Count as tool call for consistency

    def print_summary(self, cache: Optional["ResponseCache"] = None):
        """Print conversation statistics with cache stats"""
        total_tokens = self.total_input_tokens + self.total_output_tokens
        print("\n" + "=" * 60)
        print("📊 CONVERSATION STATISTICS")
        print("=" * 60)
        print(f"   API Requests:      {self.total_requests}")
        print(f"   Tool Calls:        {self.tool_calls}")
        print(f"   Input Tokens:      {self.total_input_tokens:,}")
        print(f"   Output Tokens:     {self.total_output_tokens:,}")
        print(f"   Total Tokens Used: {total_tokens:,}")

        if cache:
            cache_stats = cache.get_stats()
            print("\n   CACHE PERFORMANCE:")
            print(f"   Cache Hits:        {cache_stats['hits']}")
            print(f"   Cache Misses:      {cache_stats['misses']}")
            print(f"   Hit Rate:          {cache_stats['hit_rate']}")
            print(f"   Cached Items:      {cache_stats['cached_items']}")

            # Estimate token savings (approximate 2000 tokens per cache hit)
            if cache_stats["hits"] > 0:
                estimated_savings = cache_stats["hits"] * 2000
                print(f"   Est. Tokens Saved: ~{estimated_savings:,}")

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
CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))  # Default 5 minutes

# Initialize conversation logger and cache
logger = ConversationLogger()
response_cache = ResponseCache(default_ttl=CACHE_TTL)

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

    def map_json_type_to_gemini(json_type: str) -> str:
        """Map JSON Schema types to Gemini proto types"""
        type_map = {
            "string": "STRING",
            "number": "NUMBER",
            "integer": "INTEGER",
            "boolean": "BOOLEAN",
            "array": "ARRAY",
            "object": "OBJECT",
        }
        return type_map.get(json_type, "STRING")

    for tool in tools_list.tools:
        properties = tool.inputSchema.get("properties", {})
        required = tool.inputSchema.get("required", [])

        # Convert to Gemini-compatible parameter format with proper types
        param_defs = {}
        for param_name, param_schema in properties.items():
            param_desc = param_schema.get("description", param_name)
            param_type = param_schema.get("type", "string")

            param_def = {
                "type_": map_json_type_to_gemini(param_type),
                "description": param_desc,
            }

            # Add items schema for arrays
            if param_type == "array" and "items" in param_schema:
                items_type = param_schema["items"].get("type", "string")
                param_def["items"] = {"type_": map_json_type_to_gemini(items_type)}

            param_defs[param_name] = param_def

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

            # Create system instruction (optimized for minimal tokens)
            system_instruction = f"""You are a ClickUp Analytics Assistant with {len(tools_list.tools)} tools.

RULES:
1. Use tools to fetch data before answering. Start with get_spaces, get_folders, etc.
2. For names, resolve to IDs first using list/search tools.
3. Present results in markdown tables. Be concise and data-driven.
4. For complex queries, chain multiple tool calls logically.
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
            # Display the actual model selected rather than a hardcoded label
            try:
                print(f"🚀 Using model: {model_name} (Type 'quit' to exit)")
            except Exception:
                print("🚀 Gemini model initialized (Type 'quit' to exit)")
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

                        # Safely check for function calls in the response
                        function_calls = []
                        candidates = getattr(response, "candidates", []) or []
                        if candidates:
                            content = getattr(candidates[0], "content", None)
                            parts = (
                                getattr(content, "parts", [])
                                if content is not None
                                else []
                            )
                            for part in parts:
                                fc = getattr(part, "function_call", None)
                                if fc:
                                    function_calls.append(fc)

                        # If no function calls, model has final answer
                        if not function_calls:
                            try:
                                answer = response.text
                                if answer and answer.strip():
                                    print(f"\n🤖 Assistant:\n{answer}\n")
                                else:
                                    print("\n🤖 Assistant: (empty response)\n")
                                    # Debug: check what's in the response
                                    if candidates:
                                        print(
                                            f"[DEBUG] Response has {len(candidates)} candidates but no text/function_calls"
                                        )
                            except Exception as e:
                                print(
                                    f"\n🤖 Assistant: (error getting response: {e})\n"
                                )
                            break

                        # Execute all function calls
                        function_responses = []

                        for fc in function_calls:
                            tool_name = fc.name
                            # Recursively convert protobuf args to dict (handles RepeatedComposite)
                            tool_args = proto_to_dict(dict(fc.args))

                            print(
                                f"   🔧 Tool: {tool_name}({json.dumps(tool_args, indent=2)})"
                            )

                            try:
                                # Check cache first
                                cached_output = response_cache.get(tool_name, tool_args)

                                if cached_output is not None:
                                    tool_output = cached_output
                                    logger.log_cache_hit()
                                    print(
                                        f"      💾 Cache hit ({len(tool_output)} chars)"
                                    )
                                else:
                                    # Call MCP tool
                                    logger.log_tool_call()
                                    result = await session.call_tool(
                                        tool_name, arguments=tool_args
                                    )
                                    # Normalize tool output to a string. Some tool implementations
                                    # may return protobuf messages (RepeatedComposite, Message, etc.)
                                    tool_output = "{}"
                                    if getattr(result, "content", None):
                                        part0 = result.content[0]
                                        part_text = getattr(part0, "text", None)
                                        if isinstance(part_text, str):
                                            tool_output = part_text
                                        else:
                                            # Try protobuf -> json conversion
                                            if MessageToJson and hasattr(
                                                part_text, "__class__"
                                            ):
                                                try:
                                                    tool_output = MessageToJson(
                                                        part_text
                                                    )
                                                except Exception:
                                                    try:
                                                        tool_output = (
                                                            json.dumps(
                                                                MessageToDict(part_text)
                                                            )
                                                            if MessageToDict
                                                            else str(part_text)
                                                        )
                                                    except Exception:
                                                        tool_output = str(part_text)
                                            else:
                                                try:
                                                    tool_output = json.dumps(part_text)
                                                except Exception:
                                                    tool_output = str(part_text)

                                    # Cache the raw output before truncation
                                    # Determine TTL based on tool type
                                    if tool_name in [
                                        "list_mapped_projects",
                                        "list_spaces",
                                        "get_workspaces",
                                    ]:
                                        ttl = (
                                            600  # 10 minutes for workspace/space lists
                                        )
                                    elif tool_name.startswith("get_project"):
                                        ttl = 180  # 3 minutes for project data
                                    else:
                                        ttl = None  # Use default (5 min)

                                    response_cache.set(
                                        tool_name, tool_args, tool_output, ttl
                                    )

                                # Parse and truncate to save tokens
                                try:
                                    parsed = json.loads(tool_output)
                                    original_len = len(tool_output)
                                    # Truncate large lists to 20 items
                                    if isinstance(parsed, list) and len(parsed) > 20:
                                        total = len(parsed)
                                        parsed = parsed[:20]
                                        parsed.append(
                                            {
                                                "_truncated": f"Showing 20 of {total} items"
                                            }
                                        )
                                        tool_output = json.dumps(
                                            parsed, separators=(",", ":")
                                        )
                                    # Compact JSON (no indent) to save tokens
                                    elif isinstance(parsed, (dict, list)):
                                        tool_output = json.dumps(
                                            parsed, separators=(",", ":")
                                        )
                                    # Hard limit at 8000 chars
                                    if len(tool_output) > 8000:
                                        tool_output = (
                                            tool_output[:8000] + "...(truncated)"
                                        )
                                        print(
                                            f"      ⚠️  Truncated {original_len} → 8000 chars"
                                        )
                                except:
                                    pass

                                if cached_output is None:
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
                        if function_responses:
                            response = chat.send_message(function_responses)
                            logger.log_api_call(response)

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
    print("  GEMINI MCP CLIENT")
    print("  Function Calling Enabled")
    print("=" * 60 + "\n")

    if not GEMINI_API_KEY:
        print("❌ Error: GEMINI_API_KEY not found in .env file")
        print("   Add your API key: GEMINI_API_KEY=your_key_here")
        exit(1)

    # Test connection first
    if asyncio.run(test_connection()):
        print("\n🚀 Starting chat interface...\n")
        asyncio.run(run_chat_loop())
