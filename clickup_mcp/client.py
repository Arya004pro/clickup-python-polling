#!/usr/bin/env python
"""
ClickUp MCP - Z.AI AI Client Entry Point

Interactive terminal client for querying ClickUp via MCP + Z.AI LLM.
Run with: python -m clickup_mcp.client
"""

import sys

# Import the actual client logic from zai_client.py
# This allows running via: python -m clickup_mcp.client
if __name__ == "__main__":
    # Import here to ensure package is initialized
    import asyncio
    sys.path.insert(0, "/app")
    
    # Import the main function from zai_client
    from zai_client import main
    
    asyncio.run(main())
