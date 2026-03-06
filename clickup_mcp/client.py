#!/usr/bin/env python
"""
ClickUp MCP - AI Client Entry Point

Interactive terminal client for querying ClickUp via MCP + LLM provider.
Run with: python -m clickup_mcp.client
"""

import os
import sys


def _resolve_client_main():
    provider = os.getenv("AI_CLIENT_PROVIDER", "zai").strip().lower()
    if provider == "openrouter":
        from openrouter_client import main

        return main
    if provider == "zai":
        from zai_client import main

        return main
    raise RuntimeError(
        f"Unsupported AI_CLIENT_PROVIDER='{provider}'. Use 'zai' or 'openrouter'."
    )


if __name__ == "__main__":
    import asyncio

    sys.path.insert(0, "/app")
    main = _resolve_client_main()
    asyncio.run(main())
