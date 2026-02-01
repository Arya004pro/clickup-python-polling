"""
ClickUp MCP Server - Multi-Provider SLM/LLM Client
===================================================
A sophisticated client that bridges the ClickUp MCP Server with multiple AI providers.

Supported Providers (in order of recommendation):
1. CEREBRAS (Primary) - UNLIMITED free, fastest inference (2000+ tokens/sec), Llama 3.1 70B
2. GROQ (Secondary) - 14,400 requests/day free, fast inference, Llama 3.3 70B
3. OLLAMA (Local Fallback) - Unlimited, runs locally, uses small models (phi3, qwen2.5)

Features:
- NO character/token truncation - All providers support 128K+ context
- Native function calling (tool use) across all providers
- Full 54-tool integration with smart tool selection
- Knowledge graph building for smart context
- Automatic provider fallback on quota errors
- Token-optimized prompts for efficiency
- Robust error handling with detailed logging

Author: ClickUp MCP Team
Version: 4.0 (Optimized Multi-Provider)
"""

import asyncio
import json
import os
import sys
import warnings
import traceback
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from enum import Enum

# Python 3.11+ compatibility for ExceptionGroup
if sys.version_info >= (3, 11):
    from builtins import ExceptionGroup
else:
    try:
        from exceptiongroup import ExceptionGroup
    except ImportError:
        ExceptionGroup = BaseException  # Fallback for older Python versions

# Suppress deprecation warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

from mcp import ClientSession
from mcp.client.sse import sse_client
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# CONFIGURATION
# ============================================================================


# --- Provider Selection ---
class Provider(Enum):
    CEREBRAS = "cerebras"  # NEW: Unlimited free, fastest
    GROQ = "groq"
    GEMINI = "gemini"
    OLLAMA = "ollama"


# Get provider from environment or default to CEREBRAS (unlimited free)
PROVIDER = Provider(os.getenv("LLM_PROVIDER", "cerebras").lower())

# --- API Keys ---
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY")  # NEW
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# --- MCP Server ---
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/sse")

# --- Model Names ---
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "llama-3.3-70b")  # Cerebras uses this
GROQ_MODEL = os.getenv(
    "GROQ_MODEL", "llama-3.1-8b-instant"
)  # Smaller, faster, less tokens
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
# IMPORTANT: Use small models that fit in 6GB RAM
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")  # Only 2GB RAM needed


# ============================================================================
# KNOWLEDGE GRAPH (Context Building)
# ============================================================================


class KnowledgeGraph:
    """
    Builds and maintains a knowledge graph of ClickUp workspace structure.
    This provides context to the SLM for accurate tool invocations.
    """

    def __init__(self):
        self.workspaces: Dict[str, str] = {}
        self.spaces: Dict[str, str] = {}
        self.folders: Dict[str, str] = {}
        self.lists: Dict[str, str] = {}
        self.last_updated: Optional[datetime] = None

    async def build(self, session: ClientSession):
        """Builds the knowledge graph from MCP server data."""
        print("\n🧠 Building Knowledge Graph...")

        try:
            # 1. Fetch Workspaces
            result = await session.call_tool("get_workspaces", arguments={})
            data = self._parse_result(result)
            if isinstance(data, list):
                for ws in data:
                    if isinstance(ws, dict):
                        self.workspaces[ws.get("name", "").lower()] = ws.get(
                            "workspace_id", ""
                        )
            print(f"   ✓ Mapped {len(self.workspaces)} Workspaces")

            # 2. Fetch Spaces
            result = await session.call_tool("get_spaces", arguments={})
            data = self._parse_result(result)
            if isinstance(data, list):
                for space in data:
                    if isinstance(space, dict):
                        self.spaces[space.get("name", "").lower()] = space.get(
                            "space_id", ""
                        )
            print(f"   ✓ Mapped {len(self.spaces)} Spaces")

            self.last_updated = datetime.now()
            print(f"   ✓ Knowledge Graph Ready ({self.total_entities} entities)")

        except Exception as e:
            print(f"   ⚠ Warning: Knowledge Graph partial build ({e})")

    def _parse_result(self, result) -> Any:
        """Parse MCP tool result to Python object."""
        try:
            if result and result.content:
                text = result.content[0].text
                return json.loads(text)
        except (json.JSONDecodeError, AttributeError, IndexError):
            pass
        return {}

    @property
    def total_entities(self) -> int:
        return (
            len(self.workspaces)
            + len(self.spaces)
            + len(self.folders)
            + len(self.lists)
        )

    def get_context_string(self) -> str:
        """Returns a context string for the SLM."""
        return json.dumps(
            {
                "workspaces": self.workspaces,
                "spaces": self.spaces,
                "folders": self.folders,
                "lists": self.lists,
            },
            indent=2,
        )


# ============================================================================
# SCHEMA SANITIZATION
# ============================================================================


def sanitize_schema(schema: Dict, provider: Provider) -> Dict:
    """
    Sanitize JSON schema for provider compatibility.
    Different providers have different limitations.
    """
    if not isinstance(schema, dict):
        return schema

    # Fields that cause issues across providers
    UNSUPPORTED_FIELDS = {
        "default",
        "examples",
        "example",
        "$schema",
        "$id",
        "$ref",
        "definitions",
        "$defs",
        "const",
        "pattern",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "additionalProperties",
        "patternProperties",
        "if",
        "then",
        "else",
        "dependentRequired",
        "dependentSchemas",
        "deprecated",
        "readOnly",
        "writeOnly",
    }

    # Gemini also doesn't like 'title'
    if provider == Provider.GEMINI:
        UNSUPPORTED_FIELDS.add("title")

    result = {}

    for key, value in schema.items():
        if key in UNSUPPORTED_FIELDS:
            continue

        if key == "properties" and isinstance(value, dict):
            result[key] = {
                prop_name: sanitize_schema(prop_schema, provider)
                for prop_name, prop_schema in value.items()
            }
        elif key == "items" and isinstance(value, dict):
            result[key] = sanitize_schema(value, provider)
        elif key in ("oneOf", "anyOf", "allOf") and isinstance(value, list):
            if len(value) == 1:
                result.update(sanitize_schema(value[0], provider))
            else:
                result["type"] = "string"
                result["description"] = (
                    schema.get("description", "") + " (provide as string)"
                )
        elif isinstance(value, dict):
            result[key] = sanitize_schema(value, provider)
        elif isinstance(value, list):
            result[key] = [
                sanitize_schema(item, provider) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            result[key] = value

    if "type" not in result and "properties" in result:
        result["type"] = "object"

    return result


# ============================================================================
# BASE PROVIDER CLASS
# ============================================================================


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    def __init__(self, mcp_tools: List, knowledge_graph: KnowledgeGraph):
        self.mcp_tools = mcp_tools
        self.knowledge_graph = knowledge_graph
        self.conversation_history: List[Dict] = []

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the provider."""
        pass

    @abstractmethod
    async def send_message(self, message: str) -> Tuple[str, List[Dict]]:
        """
        Send a message and return (response_text, tool_calls).
        tool_calls is a list of {"name": str, "arguments": dict}
        """
        pass

    @abstractmethod
    async def send_tool_results(
        self, tool_results: List[Dict]
    ) -> Tuple[str, List[Dict]]:
        """
        Send tool results back and return (response_text, more_tool_calls).
        """
        pass

    def get_system_prompt(self) -> str:
        """Build the system prompt."""
        return f"""You are a ClickUp Project Management Assistant powered by 54 specialized tools.

## Your Capabilities:
You can access and manage ClickUp data through these tool categories:
1. **Workspace Structure** (10 tools): Navigate workspaces, spaces, folders, lists
2. **Task Management** (12 tools): Create, update, search, and analyze tasks
3. **PM Analytics** (9 tools): Time tracking, progress reports, estimations
4. **Project Intelligence** (8 tools): AI insights, summaries, recommendations  
5. **Sync & Mapping** (8 tools): External integrations, data synchronization
6. **Project Configuration** (7 tools): Settings, templates, custom fields

## CRITICAL INSTRUCTIONS - NEVER BREAK THESE RULES:

### 🚫 ANTI-HALLUCINATION RULES:
1. **ONLY use data from actual tool results or the knowledge graph**
2. **NEVER invent, guess, or assume data that wasn't returned by tools**
3. **If a tool fails, say it failed - don't make up alternative data**
4. **If you don't have the requested information, say so clearly**
5. **Always cite which tool provided specific information**
6. **When tools return errors, report the error - don't fabricate success**

### 📋 DATA ACCURACY RULES:
- Task IDs, names, statuses must be EXACTLY as returned by tools
- Dates, numbers, metrics must be PRECISELY what tools returned
- User names, assignees must be VERBATIM from tool results
- If data is missing or null in tool results, acknowledge this
- Never fill in "reasonable" values that weren't actually provided

### 🔧 Tool Usage Rules:
- Use tools to get fresh, current data rather than relying on old information
- If one tool fails, try alternative tools when appropriate
- Explain what each tool call is attempting to accomplish
- When multiple tools are available for a task, choose the most appropriate one
- Always validate tool results before presenting them to the user

### 🆔 ID vs NAME Resolution (IMPORTANT):
**The enhanced tools now accept BOTH IDs and Names!**

- **get_spaces(workspace_id)**: workspace_id can be a NAME like "Avinashi" or an ID like "3301011"
- **get_space(space_id)**: space_id can be a NAME like "AIX" or an ID like "90165253762"
- **get_folders(space_id)**: space_id can be a NAME like "AIX" or an ID
- **map_project(id, type)**: id can be a NAME (for spaces) like "AIX" or an ID

**When a user mentions a workspace/space by name:**
1. You can DIRECTLY pass the name to these tools (e.g., get_spaces("Avinashi"))
2. The tools will automatically resolve names to IDs
3. If the name doesn't exist, tools return helpful error with available options
4. DO NOT manually try to "resolve" names first - let the tools handle it!

**Example Correct Usage:**
- User: "Map the project AIX which is a space"
- Correct: map_project(id="AIX", type="space") ✅
- Incorrect: Asking user for space ID ❌
- Incorrect: Calling get_spaces first to get ID ❌ (unless you need to list all spaces)

Current Knowledge Graph: {self.knowledge_graph.get_context_string()}

Remember: Your responses must be grounded in actual tool results and knowledge graph data. Never hallucinate or invent information."""


# ============================================================================
# CEREBRAS PROVIDER (PRIMARY - UNLIMITED FREE!)
# ============================================================================


class CerebrasProvider(LLMProvider):
    """
    Cerebras provider - THE BEST FREE OPTION!

    Benefits:
    - UNLIMITED requests (no daily limits!)
    - UNLIMITED tokens (no TPD limits!)
    - 2000+ tokens/second (fastest in the world)
    - Llama 3.3 70B model
    - OpenAI-compatible API

    Get API key: https://cloud.cerebras.ai/
    """

    def __init__(self, mcp_tools: List, knowledge_graph: KnowledgeGraph):
        super().__init__(mcp_tools, knowledge_graph)
        self.client = None
        self.tools_formatted = []

    async def initialize(self) -> None:
        """Initialize Cerebras client using OpenAI SDK."""
        try:
            from openai import OpenAI

            api_key = CEREBRAS_API_KEY
            if not api_key:
                raise ValueError("CEREBRAS_API_KEY not set")

            # Cerebras uses OpenAI-compatible API
            self.client = OpenAI(api_key=api_key, base_url="https://api.cerebras.ai/v1")

            # Format tools for OpenAI-compatible API
            self.tools_formatted = self._convert_tools()

            print(f"🧠 Cerebras ({CEREBRAS_MODEL}) initialized - UNLIMITED & FASTEST!")

        except ImportError:
            raise ImportError(
                "OpenAI package required for Cerebras. Run: pip install openai"
            )

    def _convert_tools(self) -> List[Dict]:
        """Convert MCP tools to OpenAI function calling format."""
        tools = []
        for tool in self.mcp_tools:
            try:
                schema = tool.inputSchema if hasattr(tool, "inputSchema") else {}
                schema = sanitize_schema(schema, Provider.CEREBRAS)
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": (tool.description or f"Tool: {tool.name}")[
                                :500
                            ],
                            "parameters": {
                                "type": "object",
                                "properties": schema.get("properties", {}),
                                "required": schema.get("required", []),
                            },
                        },
                    }
                )
            except Exception as e:
                print(f"   ⚠ Could not convert tool '{tool.name}': {e}")
        return tools

    async def send_message(self, message: str) -> Tuple[str, List[Dict]]:
        """Send message to Cerebras."""
        self.conversation_history.append({"role": "user", "content": message})

        try:
            response = await asyncio.to_thread(
                self.client.chat.completions.create,
                model=CEREBRAS_MODEL,
                messages=self.conversation_history,
                tools=self.tools_formatted if self.tools_formatted else None,
                tool_choice="auto" if self.tools_formatted else None,
                temperature=0.1,
                max_tokens=8192,
            )
        except Exception as e:
            # If tools fail, try without tools
            if "tool" in str(e).lower() or "function" in str(e).lower():
                print(f"   ⚠ Function calling failed, retrying without tools")
                response = await asyncio.to_thread(
                    self.client.chat.completions.create,
                    model=CEREBRAS_MODEL,
                    messages=self.conversation_history,
                    temperature=0.1,
                    max_tokens=8192,
                )
                assistant_message = response.choices[0].message
                self.conversation_history.append(
                    {
                        "role": "assistant",
                        "content": assistant_message.content,
                    }
                )
                return (
                    assistant_message.content + "\n\n💡 Using knowledge graph mode.",
                    [],
                )
            raise e

        assistant_message = response.choices[0].message
        clean_message = {"role": "assistant", "content": assistant_message.content}

        if assistant_message.tool_calls:
            clean_message["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in assistant_message.tool_calls
            ]

        self.conversation_history.append(clean_message)

        # Extract tool calls
        tool_calls = []
        if assistant_message.tool_calls:
            for tc in assistant_message.tool_calls:
                try:
                    arguments = (
                        json.loads(tc.function.arguments)
                        if tc.function.arguments
                        else {}
                    )
                    tool_calls.append(
                        {
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments": arguments,
                        }
                    )
                except json.JSONDecodeError as e:
                    print(f"   ⚠ Failed to parse args for {tc.function.name}: {e}")

        return assistant_message.content or "", tool_calls

    async def send_tool_results(
        self, tool_results: List[Dict]
    ) -> Tuple[str, List[Dict]]:
        """Send tool results back to Cerebras."""
        for result in tool_results:
            self.conversation_history.append(
                {
                    "role": "tool",
                    "tool_call_id": result["id"],
                    "content": result["output"],
                }
            )

        response = await asyncio.to_thread(
            self.client.chat.completions.create,
            model=CEREBRAS_MODEL,
            messages=self.conversation_history,
            tools=self.tools_formatted if self.tools_formatted else None,
            tool_choice="auto",
            temperature=0.1,
            max_tokens=8192,
        )

        assistant_message = response.choices[0].message
        clean_message = {"role": "assistant", "content": assistant_message.content}

        if assistant_message.tool_calls:
            clean_message["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in assistant_message.tool_calls
            ]

        self.conversation_history.append(clean_message)

        tool_calls = []
        if assistant_message.tool_calls:
            for tc in assistant_message.tool_calls:
                try:
                    arguments = (
                        json.loads(tc.function.arguments)
                        if tc.function.arguments
                        else {}
                    )
                    tool_calls.append(
                        {
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments": arguments,
                        }
                    )
                except json.JSONDecodeError:
                    pass

        return assistant_message.content or "", tool_calls


# ============================================================================
# GROQ PROVIDER (SECONDARY - 14,400 requests/day)
# ============================================================================


class GroqProvider(LLMProvider):
    """
    Groq provider using Llama 3_3 70B model.

    Rate Limits (Free Tier):
    - 14,400 requests/day (vs Gemini's ~50)
    - 6,000 tokens/minute
    - Very fast inference (100+ tokens/sec)

    Get API key: https://console.groq.com/keys
    """

    def __init__(self, mcp_tools: List, knowledge_graph: KnowledgeGraph):
        super().__init__(mcp_tools, knowledge_graph)
        self.client = None
        self.tools_formatted = []

    async def initialize(self) -> None:
        """Initialize Groq client."""
        try:
            from groq import Groq
        except ImportError:
            raise ImportError("Groq package not installed. Run: pip install groq")

        if not GROQ_API_KEY:
            raise ValueError(
                "GROQ_API_KEY not found!\n"
                "Get your free API key at: https://console.groq.com/keys\n"
                "Add to .env: GROQ_API_KEY=your_key_here"
            )

        self.client = Groq(api_key=GROQ_API_KEY)
        self.tools_formatted = self._convert_tools()

        # Initialize conversation with system prompt
        self.conversation_history = [
            {"role": "system", "content": self.get_system_prompt()}
        ]

        print(f"🤖 Groq ({GROQ_MODEL}) initialized successfully!")

    def _convert_tools(self) -> List[Dict]:
        """Convert MCP tools to Groq/OpenAI format."""
        tools = []
        for tool in self.mcp_tools:
            try:
                schema = sanitize_schema(tool.inputSchema or {}, Provider.GROQ)
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": (tool.description or f"Tool: {tool.name}")[
                                :1024
                            ],
                            "parameters": {
                                "type": "object",
                                "properties": schema.get("properties", {}),
                                "required": schema.get("required", []),
                            },
                        },
                    }
                )
            except Exception as e:
                print(f"   ⚠ Could not convert tool '{tool.name}': {e}")
        return tools

    async def send_message(self, message: str) -> Tuple[str, List[Dict]]:
        """Send message to Groq."""
        self.conversation_history.append({"role": "user", "content": message})

        try:
            response = await asyncio.to_thread(
                self.client.chat.completions.create,
                model=GROQ_MODEL,
                messages=self.conversation_history,
                tools=self.tools_formatted if self.tools_formatted else None,
                tool_choice="auto",
                temperature=0.1,
                max_tokens=4096,  # Reduced for better function calling
            )
        except Exception as e:
            # If tools fail, try without tools as fallback
            if "tool" in str(e).lower() and self.tools_formatted:
                print(f"   ⚠ Function calling failed, trying without tools: {e}")
                response = await asyncio.to_thread(
                    self.client.chat.completions.create,
                    model=GROQ_MODEL,
                    messages=self.conversation_history,
                    temperature=0.1,
                    max_tokens=4096,
                )
                assistant_message = response.choices[0].message
                clean_message = {
                    "role": "assistant",
                    "content": assistant_message.content,
                }
                self.conversation_history.append(clean_message)
                return (
                    assistant_message.content
                    + "\n\n💡 Note: Function calling disabled due to issues, using knowledge graph.",
                    [],
                )
            else:
                raise e

        assistant_message = response.choices[0].message

        # Create clean message dict for history (no annotations or unsupported fields)
        clean_message = {"role": "assistant", "content": assistant_message.content}

        # Add tool calls if present
        if assistant_message.tool_calls:
            clean_message["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in assistant_message.tool_calls
            ]

        self.conversation_history.append(clean_message)

        # Extract tool calls for processing
        tool_calls = []
        if assistant_message.tool_calls:
            for tc in assistant_message.tool_calls:
                try:
                    # Debug: Print the raw tool call to see its format
                    print(f"🔍 Debug - Raw tool call: {tc}")
                    print(f"🔍 Debug - Function name: {tc.function.name}")
                    print(f"🔍 Debug - Function args: {tc.function.arguments}")

                    arguments = (
                        json.loads(tc.function.arguments)
                        if tc.function.arguments
                        else {}
                    )
                    tool_calls.append(
                        {
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments": arguments,
                        }
                    )
                except json.JSONDecodeError as e:
                    print(
                        f"⚠️ Failed to parse tool call arguments for {tc.function.name}: {e}"
                    )
                    print(f"   Raw arguments: {tc.function.arguments}")
                    # Try to extract function name and arguments from malformed format
                    if "<function=" in str(tc.function.arguments):
                        print(
                            "🔧 Detected malformed Groq function call format, attempting to parse..."
                        )
                        # Skip this tool call - Groq is generating wrong format
                        continue
                except Exception as e:
                    print(f"⚠️ Unexpected error processing tool call: {e}")
                except Exception as e:
                    print(f"⚠️ Unexpected error processing tool call: {e}")
                    continue

        return assistant_message.content or "", tool_calls

    async def send_tool_results(
        self, tool_results: List[Dict]
    ) -> Tuple[str, List[Dict]]:
        """Send tool results back to Groq."""
        for result in tool_results:
            self.conversation_history.append(
                {
                    "role": "tool",
                    "tool_call_id": result["id"],
                    "content": result["output"],
                }
            )

        response = await asyncio.to_thread(
            self.client.chat.completions.create,
            model=GROQ_MODEL,
            messages=self.conversation_history,
            tools=self.tools_formatted if self.tools_formatted else None,
            tool_choice="auto",
            temperature=0.1,
            max_tokens=8192,
        )

        assistant_message = response.choices[0].message

        # Create clean message dict for history (no annotations or unsupported fields)
        clean_message = {"role": "assistant", "content": assistant_message.content}

        # Add tool calls if present
        if assistant_message.tool_calls:
            clean_message["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in assistant_message.tool_calls
            ]

        self.conversation_history.append(clean_message)

        tool_calls = []
        if assistant_message.tool_calls:
            for tc in assistant_message.tool_calls:
                tool_calls.append(
                    {
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": json.loads(tc.function.arguments)
                        if tc.function.arguments
                        else {},
                    }
                )

        return assistant_message.content or "", tool_calls


# ============================================================================
# GEMINI PROVIDER (FALLBACK - Limited quota)
# ============================================================================


class GeminiProvider(LLMProvider):
    """
    Google Gemini provider.

    Rate Limits (Free Tier):
    - ~50-100 requests/day (very limited!)
    - 1M token context window

    Get API key: https://aistudio.google.com/apikey
    """

    def __init__(self, mcp_tools: List, knowledge_graph: KnowledgeGraph):
        super().__init__(mcp_tools, knowledge_graph)
        self.model = None
        self.chat = None

    async def initialize(self) -> None:
        """Initialize Gemini client."""
        try:
            import google.generativeai as genai

            self.genai = genai
        except ImportError:
            raise ImportError(
                "Google Generative AI package not installed. Run: pip install google-generativeai"
            )

        if not GEMINI_API_KEY:
            raise ValueError(
                "GEMINI_API_KEY not found!\n"
                "Get your free API key at: https://aistudio.google.com/apikey\n"
                "Add to .env: GEMINI_API_KEY=your_key_here"
            )

        genai.configure(api_key=GEMINI_API_KEY)

        # Convert tools
        gemini_tools = self._convert_tools()

        # Create model
        self.model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            generation_config={
                "temperature": 0.1,
                "top_p": 0.95,
                "max_output_tokens": 8192,
            },
            tools=[{"function_declarations": gemini_tools}] if gemini_tools else None,
            system_instruction=self.get_system_prompt(),
        )

        self.chat = self.model.start_chat(enable_automatic_function_calling=False)
        print(f"🤖 Gemini ({GEMINI_MODEL}) initialized successfully!")

    def _convert_tools(self) -> List[Dict]:
        """Convert MCP tools to Gemini format."""
        tools = []
        for tool in self.mcp_tools:
            try:
                schema = sanitize_schema(tool.inputSchema or {}, Provider.GEMINI)
                func_decl = {
                    "name": tool.name,
                    "description": (tool.description or f"Tool: {tool.name}")[:1024],
                }
                if schema.get("properties"):
                    func_decl["parameters"] = {
                        "type": "object",
                        "properties": schema.get("properties", {}),
                        "required": schema.get("required", []),
                    }
                tools.append(func_decl)
            except Exception as e:
                print(f"   ⚠ Could not convert tool '{tool.name}': {e}")
        return tools

    async def send_message(self, message: str) -> Tuple[str, List[Dict]]:
        """Send message to Gemini."""
        response = await asyncio.to_thread(self.chat.send_message, message)
        return self._parse_response(response)

    async def send_tool_results(
        self, tool_results: List[Dict]
    ) -> Tuple[str, List[Dict]]:
        """Send tool results back to Gemini."""
        parts = [
            self.genai.protos.Part(
                function_response=self.genai.protos.FunctionResponse(
                    name=result["name"], response={"result": result["output"]}
                )
            )
            for result in tool_results
        ]
        response = await asyncio.to_thread(self.chat.send_message, parts)
        return self._parse_response(response)

    def _parse_response(self, response) -> Tuple[str, List[Dict]]:
        """Parse Gemini response."""
        tool_calls = []
        text_parts = []

        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    tool_calls.append(
                        {
                            "id": fc.name,  # Gemini doesn't have IDs, use name
                            "name": fc.name,
                            "arguments": dict(fc.args) if fc.args else {},
                        }
                    )
                elif hasattr(part, "text") and part.text:
                    text_parts.append(part.text)

        return "\n".join(text_parts), tool_calls


# ============================================================================
# OLLAMA PROVIDER (LOCAL - Unlimited, Low RAM)
# ============================================================================


class OllamaProvider(LLMProvider):
    """
    Ollama provider for local models - OPTIMIZED FOR LOW RAM.

    Features:
    - Unlimited requests (runs locally)
    - No API costs
    - Requires Ollama installed: https://ollama.ai

    Recommended models by RAM:
    - 4GB RAM: phi3:mini, gemma2:2b, qwen2.5:1.5b
    - 6GB RAM: qwen2.5:3b, phi3:medium, llama3.2:3b
    - 8GB RAM: llama3.1:8b, mistral:7b
    - 16GB+ RAM: llama3.1:70b, qwen2.5:32b
    """

    def __init__(self, mcp_tools: List, knowledge_graph: KnowledgeGraph):
        super().__init__(mcp_tools, knowledge_graph)
        self.client = None
        self.tools_formatted = []
        self.available_models = []

    async def initialize(self) -> None:
        """Initialize Ollama client with automatic model selection for low RAM."""
        try:
            import ollama

            self.ollama = ollama
        except ImportError:
            raise ImportError(
                "Ollama package not installed. Run: pip install ollama\n"
                "Also ensure Ollama is installed: https://ollama.ai"
            )

        # Check if Ollama is running
        try:
            self.client = ollama.Client(host=OLLAMA_BASE_URL)
            # Get available models
            models_response = self.client.list()
            self.available_models = [
                m.get("name", m.get("model", ""))
                for m in models_response.get("models", [])
            ]
            print(f"   📦 Available Ollama models: {self.available_models[:5]}...")
        except Exception as e:
            raise ConnectionError(
                f"Cannot connect to Ollama at {OLLAMA_BASE_URL}\n"
                f"Ensure Ollama is running: ollama serve\n"
                f"Error: {e}"
            )

        # ALWAYS prefer small models for low RAM systems (< 16GB)
        # These models work well with 4-8GB RAM
        small_models_priority = [
            "qwen2.5:3b",  # 2GB RAM, excellent quality
            "phi3:mini",  # 2.5GB RAM
            "gemma2:2b",  # 2GB RAM
            "llama3.2:3b",  # 2.5GB RAM
            "qwen2.5:1.5b",  # 1GB RAM
            "gemma3:4b",  # 3GB RAM
            "tinyllama",  # 1GB RAM
        ]

        model_to_use = None

        # First, try to find a small model that's already available
        for small_model in small_models_priority:
            if small_model in self.available_models:
                model_to_use = small_model
                print(f"   ✓ Selected small model: {small_model} (RAM-friendly)")
                break

        # If no small model available, try to pull qwen2.5:3b
        if not model_to_use:
            print(f"   📥 No small model found. Pulling qwen2.5:3b (2GB RAM)...")
            try:
                self.client.pull("qwen2.5:3b")
                model_to_use = "qwen2.5:3b"
                print(f"   ✓ Successfully pulled qwen2.5:3b")
            except Exception as e:
                print(f"   ⚠ Could not pull qwen2.5:3b: {e}")
                # Last resort: use smallest available model
                if self.available_models:
                    model_to_use = self.available_models[0]
                    print(f"   ⚠ Falling back to: {model_to_use}")
                else:
                    raise ValueError(
                        "No Ollama models available. Run: ollama pull qwen2.5:3b"
                    )

        # Store the model to use for this instance
        self.model_name = model_to_use

        self.tools_formatted = self._convert_tools()
        self.conversation_history = [
            {"role": "system", "content": self.get_system_prompt()}
        ]

        print(f"🤖 Ollama ({self.model_name}) initialized successfully!")

    def _convert_tools(self) -> List[Dict]:
        """Convert MCP tools to Ollama format."""
        tools = []
        for tool in self.mcp_tools:
            try:
                schema = sanitize_schema(tool.inputSchema or {}, Provider.OLLAMA)
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": (tool.description or f"Tool: {tool.name}")[
                                :1024
                            ],
                            "parameters": {
                                "type": "object",
                                "properties": schema.get("properties", {}),
                                "required": schema.get("required", []),
                            },
                        },
                    }
                )
            except Exception as e:
                print(f"   ⚠ Could not convert tool '{tool.name}': {e}")
        return tools

    async def send_message(self, message: str) -> Tuple[str, List[Dict]]:
        """Send message to Ollama."""
        self.conversation_history.append({"role": "user", "content": message})

        response = await asyncio.to_thread(
            self.client.chat,
            model=self.model_name,
            messages=self.conversation_history,
            tools=self.tools_formatted if self.tools_formatted else None,
        )

        assistant_message = response["message"]
        self.conversation_history.append(assistant_message)

        tool_calls = []
        if "tool_calls" in assistant_message and assistant_message["tool_calls"]:
            for tc in assistant_message["tool_calls"]:
                tool_calls.append(
                    {
                        "id": tc["function"]["name"],
                        "name": tc["function"]["name"],
                        "arguments": tc["function"].get("arguments", {}),
                    }
                )

        return assistant_message.get("content", ""), tool_calls

    async def send_tool_results(
        self, tool_results: List[Dict]
    ) -> Tuple[str, List[Dict]]:
        """Send tool results back to Ollama."""
        for result in tool_results:
            self.conversation_history.append(
                {"role": "tool", "content": result["output"]}
            )

        response = await asyncio.to_thread(
            self.client.chat,
            model=self.model_name,
            messages=self.conversation_history,
            tools=self.tools_formatted if self.tools_formatted else None,
        )

        assistant_message = response["message"]
        self.conversation_history.append(assistant_message)

        tool_calls = []
        if "tool_calls" in assistant_message and assistant_message["tool_calls"]:
            for tc in assistant_message["tool_calls"]:
                tool_calls.append(
                    {
                        "id": tc["function"]["name"],
                        "name": tc["function"]["name"],
                        "arguments": tc["function"].get("arguments", {}),
                    }
                )

        return assistant_message.get("content", ""), tool_calls


# ============================================================================
# MAIN CLIENT
# ============================================================================


class ClickUpMCPClient:
    """
    Main client that orchestrates communication between:
    - User (natural language)
    - LLM Provider (Groq/Gemini/Ollama) with automatic fallback
    - MCP Server (tool execution)
    """

    def __init__(self, provider: Provider):
        self.provider_type = provider
        self.provider: Optional[LLMProvider] = None
        self.fallback_provider: Optional[LLMProvider] = None
        self.session: Optional[ClientSession] = None
        self.knowledge_graph = KnowledgeGraph()
        self.tools_list = []
        self.rate_limited_providers = set()

    async def initialize(self, session: ClientSession):
        """Initialize the client with MCP session and set up providers."""
        self.session = session

        # 1. Build Knowledge Graph
        await self.knowledge_graph.build(session)

        # 2. Fetch Tools
        tools_response = await session.list_tools()
        self.tools_list = tools_response.tools
        print(f"✅ Loaded {len(self.tools_list)} MCP tools")

        # 3. Initialize Primary Provider
        try:
            if self.provider_type == Provider.CEREBRAS:
                print("🧠 Initializing Cerebras provider (UNLIMITED!)...")
                self.provider = CerebrasProvider(self.tools_list, self.knowledge_graph)
                # Set up Ollama as fallback (for local backup)
                print("🔄 Setting up Ollama fallback...")
                try:
                    self.fallback_provider = OllamaProvider(
                        self.tools_list, self.knowledge_graph
                    )
                    await self.fallback_provider.initialize()
                    print("🔄 Ollama fallback provider initialized")
                except Exception as e:
                    print(f"⚠️ Ollama fallback not available: {e}")
                    self.fallback_provider = None
            elif self.provider_type == Provider.GROQ:
                print("🔗 Initializing Groq provider...")
                self.provider = GroqProvider(self.tools_list, self.knowledge_graph)
                # Set up Ollama as fallback for Groq
                print("🔄 Setting up Ollama fallback...")
                try:
                    self.fallback_provider = OllamaProvider(
                        self.tools_list, self.knowledge_graph
                    )
                    await self.fallback_provider.initialize()
                    print("🔄 Ollama fallback provider initialized")
                except Exception as e:
                    print(f"⚠️ Ollama fallback not available: {e}")
                    self.fallback_provider = None
            elif self.provider_type == Provider.GEMINI:
                print("🔗 Initializing Gemini provider...")
                self.provider = GeminiProvider(self.tools_list, self.knowledge_graph)
                # Set up Ollama as fallback for Gemini
                print("🔄 Setting up Ollama fallback...")
                try:
                    self.fallback_provider = OllamaProvider(
                        self.tools_list, self.knowledge_graph
                    )
                    await self.fallback_provider.initialize()
                    print("🔄 Ollama fallback provider initialized")
                except Exception as e:
                    print(f"⚠️ Ollama fallback not available: {e}")
                    self.fallback_provider = None
            elif self.provider_type == Provider.OLLAMA:
                print("🔗 Initializing Ollama provider...")
                self.provider = OllamaProvider(self.tools_list, self.knowledge_graph)
                # No fallback needed for Ollama (unlimited local)
            else:
                raise ValueError(f"Unknown provider: {self.provider_type}")

            print("🔗 Initializing primary provider...")
            await self.provider.initialize()
            print("✅ Primary provider initialized successfully")
        except Exception as e:
            print(f"❌ Provider initialization failed: {e}")
            import traceback

            traceback.print_exc()
            raise

    async def process_message(self, user_message: str) -> str:
        """Process a user message through the full pipeline with automatic fallback."""
        if not self.provider or not self.session:
            return "❌ Client not initialized. Please restart."

        current_provider = self.provider
        provider_name = "Primary"

        try:
            # Send to LLM
            response_text, tool_calls = await current_provider.send_message(
                user_message
            )

            # Process tool calls if any
            max_iterations = 10
            iteration = 0

            while tool_calls and iteration < max_iterations:
                iteration += 1

                # Execute all tool calls
                tool_results = []
                for tc in tool_calls:
                    tool_name = tc["name"]
                    tool_args = tc["arguments"]

                    print(
                        f"🔧 Tool: {tool_name}({json.dumps(tool_args, default=str)[:100]}...)"
                    )

                    try:
                        result = await self.session.call_tool(
                            tool_name, arguments=tool_args
                        )
                        tool_output = result.content[0].text if result.content else "{}"
                        print(f"   ✓ Result: {len(tool_output):,} chars")
                    except Exception as e:
                        tool_output = json.dumps({"error": str(e)})
                        print(f"   ✗ Error: {e}")

                    tool_results.append(
                        {"id": tc["id"], "name": tool_name, "output": tool_output}
                    )

                # Send results back to LLM
                try:
                    (
                        response_text,
                        tool_calls,
                    ) = await current_provider.send_tool_results(tool_results)
                except Exception as e:
                    # Check if this is a rate limit error and we have a fallback
                    if self._is_rate_limit_error(str(e)) and self.fallback_provider:
                        print(
                            f"⚠️ {provider_name} provider rate limited, switching to Ollama fallback"
                        )
                        current_provider = self.fallback_provider
                        provider_name = "Fallback (Ollama)"
                        # Retry with fallback
                        (
                            response_text,
                            tool_calls,
                        ) = await current_provider.send_tool_results(tool_results)
                    else:
                        raise e

            return response_text or "⚠ Received empty response from model."

        except Exception as e:
            error_msg = str(e)
            # Check for rate limit errors and try fallback
            if (
                self._is_rate_limit_error(error_msg)
                and self.fallback_provider
                and current_provider != self.fallback_provider
            ):
                print(
                    f"⚠️ {provider_name} provider failed with rate limit, trying Ollama fallback..."
                )
                try:
                    # Switch to fallback provider
                    current_provider = self.fallback_provider
                    response_text, tool_calls = await current_provider.send_message(
                        user_message
                    )

                    # Process tool calls with fallback provider
                    max_iterations = 10
                    iteration = 0

                    while tool_calls and iteration < max_iterations:
                        iteration += 1

                        tool_results = []
                        for tc in tool_calls:
                            tool_name = tc["name"]
                            tool_args = tc["arguments"]

                            print(
                                f"🔧 Tool: {tool_name}({json.dumps(tool_args, default=str)[:100]}...)"
                            )

                            try:
                                result = await self.session.call_tool(
                                    tool_name, arguments=tool_args
                                )
                                tool_output = (
                                    result.content[0].text if result.content else "{}"
                                )
                                print(f"   ✓ Result: {len(tool_output):,} chars")
                            except Exception as tool_e:
                                tool_output = json.dumps({"error": str(tool_e)})
                                print(f"   ✗ Error: {tool_e}")

                            tool_results.append(
                                {
                                    "id": tc["id"],
                                    "name": tool_name,
                                    "output": tool_output,
                                }
                            )

                        (
                            response_text,
                            tool_calls,
                        ) = await current_provider.send_tool_results(tool_results)

                    return (
                        response_text
                        or "✅ Switched to Ollama fallback - response generated successfully."
                    )

                except Exception as fallback_e:
                    print(f"❌ Fallback provider also failed: {fallback_e}")
                    return f"❌ Both primary and fallback providers failed. Primary error: {error_msg}, Fallback error: {fallback_e}"

            print(f"❌ Provider error: {e}")
            return f"❌ Error: {error_msg}"

    def _is_rate_limit_error(self, error_message: str) -> bool:
        """Check if error is related to rate limiting."""
        error_lower = error_message.lower()
        rate_limit_indicators = [
            "429",
            "quota",
            "rate limit",
            "rate_limit",
            "too many requests",
            "tokens per day",
            "tpd",
            "requests per minute",
            "rpm",
        ]
        return any(indicator in error_lower for indicator in rate_limit_indicators)


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================


def print_provider_info():
    """Print provider configuration info."""
    print("\n📊 Provider Comparison:")
    print("┌─────────────┬────────────────────┬──────────────┬─────────────────┐")
    print("│ Provider    │ Free Tier Limit    │ Context      │ Status          │")
    print("├─────────────┼────────────────────┼──────────────┼─────────────────┤")
    print(
        f"│ CEREBRAS    │ UNLIMITED! 🎉      │ 128K tokens  │ {'✅ Key Set' if CEREBRAS_API_KEY else '❌ No Key':<15} │"
    )
    print(
        f"│ GROQ        │ 14,400 req/day     │ 128K tokens  │ {'✅ Key Set' if GROQ_API_KEY else '❌ No Key':<15} │"
    )
    print(
        f"│ GEMINI      │ ~50 req/day        │ 1M tokens    │ {'✅ Key Set' if GEMINI_API_KEY else '❌ No Key':<15} │"
    )
    print(f"│ OLLAMA      │ Unlimited (local)  │ 128K tokens  │ {'🏠 Local':<15} │")
    print("└─────────────┴────────────────────┴──────────────┴─────────────────┘")


async def main():
    """Main entry point for the ClickUp MCP Client."""

    print("=" * 60)
    print("🚀 ClickUp MCP Server - Multi-Provider SLM/LLM Client v4.0")
    print("=" * 60)

    print_provider_info()

    # Determine provider
    provider = PROVIDER
    print(f"\n🎯 Selected Provider: {provider.value.upper()}")

    # Validate provider configuration
    if provider == Provider.CEREBRAS and not CEREBRAS_API_KEY:
        print("\n❌ ERROR: CEREBRAS_API_KEY not configured!")
        print("\n📋 Setup Instructions:")
        print("   1. Go to: https://cloud.cerebras.ai/")
        print("   2. Sign up (free) and create an API key")
        print("   3. Add to .env file: CEREBRAS_API_KEY=your_key_here")
        print("\n🎉 CEREBRAS is UNLIMITED - no daily limits, no token limits!")
        print("\n💡 Or switch provider: LLM_PROVIDER=groq or LLM_PROVIDER=ollama")
        return

    if provider == Provider.GROQ and not GROQ_API_KEY:
        print("\n❌ ERROR: GROQ_API_KEY not configured!")
        print("\n📋 Setup Instructions:")
        print("   1. Go to: https://console.groq.com/keys")
        print("   2. Create a free API key (14,400 requests/day!)")
        print("   3. Add to .env file: GROQ_API_KEY=your_key_here")
        print(
            "\n💡 Or switch provider: LLM_PROVIDER=cerebras (UNLIMITED!) or LLM_PROVIDER=ollama"
        )
        return

    if provider == Provider.GEMINI and not GEMINI_API_KEY:
        print("\n❌ ERROR: GEMINI_API_KEY not configured!")
        print("\n📋 Setup Instructions:")
        print("   1. Go to: https://aistudio.google.com/apikey")
        print("   2. Create a free API key")
        print("   3. Add to .env file: GEMINI_API_KEY=your_key_here")
        return

    print(f"\n🔗 Connecting to MCP Server: {MCP_SERVER_URL}")

    try:
        async with sse_client(url=MCP_SERVER_URL) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()

                # Initialize client
                client = ClickUpMCPClient(provider)
                await client.initialize(session)

                print("\n" + "=" * 60)
                print("💬 Chat Ready! Type 'quit' to exit, 'help' for commands")
                print("=" * 60)

                # Chat loop
                while True:
                    try:
                        user_input = input("\n📝 You: ").strip()

                        if not user_input:
                            continue

                        if user_input.lower() in ["quit", "exit", "q"]:
                            print("\n👋 Goodbye!")
                            break

                        if user_input.lower() == "help":
                            print(_get_help_text())
                            continue

                        if user_input.lower() == "tools":
                            print(f"\n📦 Available Tools ({len(client.tools_list)}):")
                            for i, tool in enumerate(client.tools_list, 1):
                                print(f"   {i:2}. {tool.name}")
                            continue

                        if user_input.lower() == "provider":
                            print_provider_info()
                            continue

                        print("\n🤔 Thinking...")
                        response = await client.process_message(user_input)
                        print(f"\n🤖 Assistant:\n{response}")

                    except KeyboardInterrupt:
                        print("\n\n👋 Goodbye!")
                        break
                    except Exception as e:
                        print(f"\n❌ Error: {e}")
                        traceback.print_exc()

    except ConnectionRefusedError:
        print(f"\n❌ ERROR: Cannot connect to MCP Server at {MCP_SERVER_URL}")
        print("\n📋 Please ensure the MCP server is running:")
        print("   fastmcp run app/mcp/mcp_server.py:mcp --transport sse --port 8001")
    except ExceptionGroup as eg:
        print(f"\n❌ ERROR: ExceptionGroup caught:")
        for i, exc in enumerate(eg.exceptions, 1):
            print(f"   Sub-exception {i}: {type(exc).__name__}: {exc}")
    except Exception as e:
        print(f"\n❌ ERROR: {type(e).__name__}: {e}")
        traceback.print_exc()


def _get_help_text() -> str:
    """Returns help text for the CLI."""
    return f"""
📚 Available Commands:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  help      - Show this help message
  tools     - List all 54 available MCP tools
  provider  - Show provider comparison table
  quit      - Exit the application

🔧 Current Provider: {PROVIDER.value.upper()}

💡 Example Queries:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  "Show me all workspaces"
  "List tasks in the Development space"
  "What's the project health score for Marketing?"
  "Find all overdue tasks"
  "Generate a daily standup for Project Alpha"
  "Who has the most tasks assigned?"
  "Show time tracking report for this week"
  "Search for tasks mentioning 'bug'"
"""


if __name__ == "__main__":
    asyncio.run(main())
