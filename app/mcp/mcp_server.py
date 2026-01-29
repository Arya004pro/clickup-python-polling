from fastmcp import FastMCP
# from app.logging_config import logger  #optional - your existing logger

# Import category modules (we'll create them one by one)
from .workspace_structure import register_workspace_tools
from .task_management import register_task_tools
from .pm_analytics import register_pm_analytics_tools
from .project_configuration import register_project_configuration_tools

mcp = FastMCP(
    name="ClickUp Sync MCP Server",
    instructions="Access and manage ClickUp data synced to Supabase. Tools use real-time ClickUp API + cached Supabase queries.",
)

# Register tools from different categories
register_workspace_tools(mcp)
register_task_tools(mcp)
register_pm_analytics_tools(mcp)
register_project_configuration_tools(mcp)

if __name__ == "__main__":
    print("Starting ClickUp MCP Server...")
    mcp.run(
        transport="streamable-http",  # most reliable for your current success
        host="0.0.0.0",
        port=8001,  # or 8001 if you prefer to keep separate from FastAPI
        stateless=True,
    )
