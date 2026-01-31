import asyncio
import json
import os
from openai import AsyncOpenAI
from mcp import ClientSession
from mcp.client.sse import sse_client
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# --- CONFIGURATION ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
BASE_URL = "https://api.groq.com/openai/v1"
MODEL_NAME = "llama-3.3-70b-versatile"
MCP_SERVER_URL = "http://127.0.0.1:8001/sse"

client = AsyncOpenAI(api_key=GROQ_API_KEY, base_url=BASE_URL)
SPACE_MAP = {}


async def build_knowledge_graph(session):
    print("\n🧠 Building Knowledge Graph...")
    try:
        # We manually call the tool to start
        result = await session.call_tool("list_spaces", arguments={})
        data = json.loads(result.content[0].text)
        if "spaces" in data:
            for space in data["spaces"]:
                SPACE_MAP[space["name"].lower()] = space["id"]
        print(f"   -> Mapped {len(SPACE_MAP)} Spaces.")
    except Exception as e:
        print(f"   -> Warning: Auto-discovery failed ({e})")


async def run_chat_loop():
    print(f"🔌 Connecting to {MCP_SERVER_URL}...")

    async with sse_client(url=MCP_SERVER_URL) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            await build_knowledge_graph(session)

            # 1. Fetch Tools & Create a Plain Text List
            tools_list = await session.list_tools()
            print(f"✅ Connected! Loaded {len(tools_list.tools)} tools.")

            tools_desc = []
            for t in tools_list.tools:
                # We format this so the LLM can read it easily
                schema = json.dumps(t.inputSchema.get("properties", {}))
                tools_desc.append(f"- {t.name}: {t.description} | Params: {schema}")

            tools_text = "\n".join(tools_desc)

            # 2. System Prompt (The "JSON ONLY" Rule)
            system_prompt = (
                f"You are a ClickUp Assistant. You have access to these tools:\n{tools_text}\n\n"
                f"CONTEXT: {json.dumps(SPACE_MAP)}\n\n"
                "RULES:\n"
                '1. To use a tool, output ONLY a JSON object: {"tool": "tool_name", "args": {...}}\n'
                "2. Do NOT output any text before or after the JSON.\n"
                '3. If you have the answer, output ONLY a JSON object: {"answer": "Your final text answer here"}\n'
                "4. Use 'get_folderless_lists' or 'get_lists' to find Lists inside Spaces.\n"
                "5. Never guess IDs. Use the Context map or search first."
            )

            messages = [{"role": "system", "content": system_prompt}]

            print("\n🤖 Groq Ready! (Type 'quit' to exit)")
            print("-" * 50)

            while True:
                user_input = input("\nYou: ")
                if user_input.lower() in ["quit", "exit"]:
                    break

                messages.append({"role": "user", "content": user_input})
                print("... Groq is thinking ...")

                while True:
                    try:
                        # Call Groq (Text Mode, not Tool Mode)
                        response = await client.chat.completions.create(
                            model=MODEL_NAME,
                            messages=messages,
                            temperature=0.1,  # Keep it strict
                            response_format={"type": "json_object"},  # Force valid JSON
                        )

                        content = response.choices[0].message.content
                        if not content:
                            continue

                        # Parse the JSON Command
                        try:
                            cmd = json.loads(content)
                        except json.JSONDecodeError:
                            print(f"❌ Error: Model output invalid JSON: {content}")
                            break

                        # CASE A: It wants to run a tool
                        if "tool" in cmd:
                            tool_name = cmd["tool"]
                            tool_args = cmd.get("args", {})
                            print(f"🛠️  Groq Request: {tool_name}({tool_args})")

                            # Execute
                            try:
                                result = await session.call_tool(
                                    tool_name, arguments=tool_args
                                )
                                tool_output = (
                                    result.content[0].text
                                    if result.content
                                    else str(result)
                                )

                                # Truncate massive data
                                if len(tool_output) > 50000:
                                    print(
                                        f"   -> Data (Truncated): {tool_output[:200]}..."
                                    )
                                    tool_output = (
                                        tool_output[:6000] + "\n... [TRUNCATED]"
                                    )
                                else:
                                    print(f"   -> Data: {tool_output[:150]}...")

                                # Feed back to LLM
                                messages.append(
                                    {"role": "assistant", "content": content}
                                )
                                messages.append(
                                    {
                                        "role": "user",
                                        "content": f"Tool Result: {tool_output}",
                                    }
                                )

                            except Exception as e:
                                print(f"   -> Error: {e}")
                                messages.append(
                                    {"role": "assistant", "content": content}
                                )
                                messages.append(
                                    {
                                        "role": "user",
                                        "content": f"Error executing tool: {e}",
                                    }
                                )

                        # CASE B: It has the Final Answer
                        elif "answer" in cmd:
                            print(f"\n🤖 Agent: {cmd['answer']}")
                            messages.append({"role": "assistant", "content": content})
                            break

                        else:
                            print(f"❌ Unknown JSON command: {cmd}")
                            break

                    except Exception as e:
                        print(f"❌ Groq API Error: {e}")
                        break


if __name__ == "__main__":
    asyncio.run(run_chat_loop())
