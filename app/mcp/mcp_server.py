from fastmcp import FastMCP
# from app.logging_config import logger  #optional - your existing logger

# Import category modules (we'll create them one by one)
from app.mcp.workspace_structure import register_workspace_tools
from app.mcp.task_management import register_task_tools
from app.mcp.pm_analytics import register_pm_analytics_tools
from app.mcp.project_configuration import register_project_configuration_tools
from app.mcp.project_intelligence import register_project_intelligence_tools
from app.mcp.sync_mapping import register_sync_mapping_tools

mcp = FastMCP(
    name="ClickUp Sync MCP Server",
    instructions="Access and manage ClickUp data synced to Supabase. Tools use real-time ClickUp API + cached Supabase queries.",
)

# Register tools from different categories
register_workspace_tools(mcp)
register_task_tools(mcp)
register_pm_analytics_tools(mcp)
register_project_configuration_tools(mcp)
register_project_intelligence_tools(mcp)
register_sync_mapping_tools(mcp)

if __name__ == "__main__":
    print("Starting ClickUp MCP Server...")
    mcp.run(
        transport="streamable-http",  # most reliable for your current success
        host="0.0.0.0",
        port=8001,  # or 8001 if you prefer to keep separate from FastAPI
        stateless=True,
    )
