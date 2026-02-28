from fastmcp import FastMCP
# from app.logging_config import logger  #optional - your existing logger

# Import category modules (we'll create them one by one)
from clickup_mcp.workspace_structure import register_workspace_tools
from clickup_mcp.task_management import register_task_tools
from clickup_mcp.pm_analytics import register_pm_analytics_tools
from clickup_mcp.project_configuration import register_project_configuration_tools
from clickup_mcp.project_intelligence import register_project_intelligence_tools
from clickup_mcp.sync_mapping import (
    register_sync_mapping_tools,
    start_mapping_maintenance_scheduler,
)
from clickup_mcp.task_reports import register_task_report_tools

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
register_task_report_tools(mcp)
start_mapping_maintenance_scheduler()


if __name__ == "__main__":
    import time
    import uvicorn

    print("Starting ClickUp MCP Server in 2s to allow initialization...")
    time.sleep(2)

    # Create the ASGI app (FastMCP 3.x uses http_app())
    app = mcp.http_app(transport="sse")

    # Run with uvicorn directly to control timeout settings
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8001,
    )
    server = uvicorn.Server(config)
    server.run()
