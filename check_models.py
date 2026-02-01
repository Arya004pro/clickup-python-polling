"""
ClickUp MCP Server - Setup Validator & Diagnostics
===================================================
This script validates your entire setup and helps troubleshoot issues.

Run this BEFORE starting the MCP server or SLM client!

Usage:
    python check_models.py

Author: ClickUp MCP Team
Version: 2.0
"""

import os
import sys
import json
import asyncio
from datetime import datetime

# ============================================================================
# CONSOLE HELPERS
# ============================================================================


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    END = "\033[0m"


def print_header(text: str):
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'=' * 60}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.CYAN}{text.center(60)}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'=' * 60}{Colors.END}")


def print_section(text: str):
    print(f"\n{Colors.BOLD}{Colors.BLUE}▶ {text}{Colors.END}")


def print_success(text: str):
    print(f"  {Colors.GREEN}✓{Colors.END} {text}")


def print_error(text: str):
    print(f"  {Colors.RED}✗{Colors.END} {text}")


def print_warning(text: str):
    print(f"  {Colors.YELLOW}⚠{Colors.END} {text}")


def print_info(text: str):
    print(f"  {Colors.CYAN}ℹ{Colors.END} {text}")


# ============================================================================
# VALIDATION CHECKS
# ============================================================================


def check_python_version():
    """Check Python version is 3.11+"""
    print_section("Python Version")

    version = sys.version_info
    version_str = f"{version.major}.{version.minor}.{version.micro}"

    if version.major >= 3 and version.minor >= 11:
        print_success(f"Python {version_str} ✓ (Recommended: 3.11.9)")
        return True
    elif version.major >= 3 and version.minor >= 10:
        print_warning(f"Python {version_str} - Works but 3.11+ recommended")
        return True
    else:
        print_error(f"Python {version_str} - Requires Python 3.11+")
        return False


def check_env_file():
    """Check .env file exists and has required variables"""
    print_section("Environment Configuration (.env)")

    env_path = os.path.join(os.path.dirname(__file__), ".env")

    if not os.path.exists(env_path):
        print_error(".env file not found!")
        print_info("Create .env file with required variables (see .env.example)")
        return False

    print_success(".env file exists")

    # Load and check variables
    from dotenv import load_dotenv

    load_dotenv()

    required_vars = {
        "CLICKUP_API_TOKEN": "ClickUp API Token",
        "DATABASE_URL": "PostgreSQL/Supabase Database URL",
    }

    optional_vars = {
        "GEMINI_API_KEY": "Google Gemini API Key (for SLM Client)",
        "CLICKUP_TEAM_ID": "ClickUp Team/Workspace ID",
        "MCP_SERVER_URL": "MCP Server URL (default: http://127.0.0.1:8001/sse)",
    }

    all_required = True

    for var, desc in required_vars.items():
        value = os.getenv(var)
        if value:
            # Mask sensitive values
            masked = (
                value[:4] + "*" * (len(value) - 8) + value[-4:]
                if len(value) > 8
                else "***"
            )
            print_success(f"{var}: {masked}")
        else:
            print_error(f"{var}: NOT SET ({desc})")
            all_required = False

    print_info("Optional variables:")
    for var, desc in optional_vars.items():
        value = os.getenv(var)
        if value:
            masked = (
                value[:4] + "*" * (len(value) - 8) + value[-4:]
                if len(value) > 8
                else "***"
            )
            print_success(f"  {var}: {masked}")
        else:
            print_warning(f"  {var}: Not set ({desc})")

    return all_required


def check_dependencies():
    """Check all required packages are installed"""
    print_section("Python Dependencies")

    required_packages = [
        ("fastapi", "FastAPI web framework"),
        ("uvicorn", "ASGI server"),
        ("fastmcp", "Fast MCP server framework"),
        ("requests", "HTTP client"),
        ("psycopg2", "PostgreSQL adapter"),
        ("dotenv", "Environment variables (python-dotenv)"),
        ("google.generativeai", "Google Gemini AI SDK"),
        ("mcp", "MCP protocol client"),
        ("apscheduler", "Background scheduler"),
    ]

    all_installed = True

    for package, desc in required_packages:
        try:
            __import__(package.replace("-", "_").split(".")[0])
            print_success(f"{package}")
        except ImportError:
            print_error(f"{package} - NOT INSTALLED ({desc})")
            all_installed = False

    if not all_installed:
        print_info("Install missing packages: pip install -r requirements.txt")

    return all_installed


def check_gemini_api():
    """Check Gemini API key and list available models"""
    print_section("Google Gemini API")

    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        print_warning("GEMINI_API_KEY not set - SLM Client won't work")
        print_info("Get your free API key at: https://aistudio.google.com/apikey")
        return False

    try:
        import warnings

        warnings.filterwarnings("ignore", category=FutureWarning)

        import google.generativeai as genai

        genai.configure(api_key=api_key)

        print_success("API Key configured")
        print_info("Available models with generateContent:")

        models_found = []
        count = 0
        for model in genai.list_models():
            if "generateContent" in model.supported_generation_methods:
                models_found.append(model.name)
                count += 1
                if count <= 5:  # Only show first 5 models
                    print_success(f"  {model.name}")

        if count > 5:
            print_info(f"  ... and {count - 5} more models available")

        # Check for recommended model
        recommended = "models/gemini-2.0-flash"
        if any(recommended in m for m in models_found):
            print_success("Recommended model available: gemini-2.0-flash")
        else:
            print_warning("gemini-2.0-flash not found, will use available model")

        return True

    except Exception as e:
        print_error(f"Gemini API Error: {e}")
        return False


def check_clickup_api():
    """Check ClickUp API connectivity"""
    print_section("ClickUp API")

    token = os.getenv("CLICKUP_API_TOKEN")

    if not token:
        print_error("CLICKUP_API_TOKEN not set!")
        return False

    try:
        import requests

        headers = {"Authorization": token}
        response = requests.get(
            "https://api.clickup.com/api/v2/team", headers=headers, timeout=10
        )

        if response.status_code == 200:
            data = response.json()
            teams = data.get("teams", [])

            print_success("API Token valid")
            print_success(f"Found {len(teams)} workspace(s)")

            for team in teams:
                print_info(f"  • {team['name']} (ID: {team['id']})")

            return True
        else:
            print_error(f"API Error: {response.status_code} - {response.text[:100]}")
            return False

    except Exception as e:
        print_error(f"Connection Error: {e}")
        return False


def check_database():
    """Check PostgreSQL/Supabase connectivity"""
    print_section("Database (PostgreSQL/Supabase)")

    db_url = os.getenv("DATABASE_URL")

    if not db_url:
        print_error("DATABASE_URL not set!")
        return False

    try:
        import psycopg2

        conn = psycopg2.connect(db_url)
        cursor = conn.cursor()

        # Test query
        cursor.execute("SELECT version();")
        version = cursor.fetchone()[0]

        print_success("Database connected")
        print_info(f"PostgreSQL: {version[:50]}...")

        # Check for required tables
        cursor.execute("""
            SELECT table_name FROM information_schema.tables 
            WHERE table_schema = 'public'
            ORDER BY table_name;
        """)
        tables = [row[0] for row in cursor.fetchall()]

        if tables:
            print_info(f"Found {len(tables)} tables:")
            for table in tables[:10]:
                print_success(f"  • {table}")
            if len(tables) > 10:
                print_info(f"  ... and {len(tables) - 10} more")
        else:
            print_warning("No tables found - run initial sync first")

        cursor.close()
        conn.close()
        return True

    except Exception as e:
        print_error(f"Database Error: {e}")
        return False


def check_mcp_server():
    """Check MCP server files exist"""
    print_section("MCP Server Files")

    mcp_files = [
        "app/mcp/__init__.py",
        "app/mcp/mcp_server.py",
        "app/mcp/workspace_structure.py",
        "app/mcp/task_management.py",
        "app/mcp/pm_analytics.py",
        "app/mcp/project_configuration.py",
        "app/mcp/project_intelligence.py",
        "app/mcp/sync_mapping.py",
    ]

    base_dir = os.path.dirname(__file__)
    all_found = True

    for file in mcp_files:
        full_path = os.path.join(base_dir, file)
        if os.path.exists(full_path):
            print_success(file)
        else:
            print_error(f"{file} - NOT FOUND")
            all_found = False

    return all_found


def count_mcp_tools():
    """Count and list all MCP tools"""
    print_section("MCP Tools Inventory")

    try:
        # Count tools by parsing module files directly
        tools = _count_tools_from_files()

        print_success(f"Total Tools Found: {len(tools)}")

        # Categorize tools
        categories = {
            "Workspace Structure": [],
            "Task Management": [],
            "PM Analytics": [],
            "Project Configuration": [],
            "Project Intelligence": [],
            "Sync & Mapping": [],
        }

        # Simple categorization based on naming
        for tool in sorted(tools):
            if (
                "workspace" in tool
                or "space" in tool
                or "folder" in tool
                or "list" in tool
                and "project" not in tool
            ):
                categories["Workspace Structure"].append(tool)
            elif (
                "task" in tool
                or "workload" in tool
                or "overdue" in tool
                or "search" in tool
            ):
                categories["Task Management"].append(tool)
            elif (
                "time" in tool
                or "progress" in tool
                or "estimation" in tool
                or "stale" in tool
                or "inactive" in tool
                or "untracked" in tool
                or "risk" in tool
                or "status_summary" in tool
            ):
                categories["PM Analytics"].append(tool)
            elif (
                "discover_projects" in tool
                or "add_project" in tool
                or "remove_project" in tool
                or "refresh_projects" in tool
                or "get_project_status" in tool
                or "get_all_projects" in tool
                or "list_projects" in tool
            ):
                categories["Project Configuration"].append(tool)
            elif (
                "health" in tool
                or "standup" in tool
                or "blocker" in tool
                or "digest" in tool
                or "intelligence" in tool
                or "defined_statuses" in tool
                or "project_statuses" in tool
                or "at_risk" in tool
                or "weekly" in tool
                or "team_workload" in tool
                or "project_time" in tool
            ):
                categories["Project Intelligence"].append(tool)
            elif (
                "map" in tool
                or "sync" in tool
                or "hierarchy" in tool
                or "cache" in tool
                or "prune" in tool
                or "clear" in tool
            ):
                categories["Sync & Mapping"].append(tool)
            else:
                # Fallback - add to most likely category
                if "project" in tool:
                    categories["Project Intelligence"].append(tool)
                else:
                    categories["Workspace Structure"].append(tool)

        for category, cat_tools in categories.items():
            if cat_tools:
                print_info(f"\n  {category} ({len(cat_tools)} tools):")
                for tool in sorted(cat_tools):
                    print_success(f"    • {tool}")

        return len(tools) >= 50

    except Exception as e:
        print_error(f"Could not count tools: {e}")
        return False


def _count_tools_from_files():
    """Parse MCP module files to find tool definitions"""
    import re

    tools = []
    base_dir = os.path.dirname(__file__)
    mcp_dir = os.path.join(base_dir, "app", "mcp")

    module_files = [
        "workspace_structure.py",
        "task_management.py",
        "pm_analytics.py",
        "project_configuration.py",
        "project_intelligence.py",
        "sync_mapping.py",
    ]

    for filename in module_files:
        filepath = os.path.join(mcp_dir, filename)
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
                # Find @mcp.tool() or @mcp.tool decorated functions
                matches = re.findall(r"@mcp\.tool\(?[^\)]*\)?\s+def\s+(\w+)", content)
                tools.extend(matches)

    return tools


# ============================================================================
# MAIN
# ============================================================================


def main():
    """Run all validation checks"""

    print_header("ClickUp MCP Server - Setup Validator")
    print(f"  Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  Working Directory: {os.getcwd()}")

    results = {}

    # Run checks
    results["python"] = check_python_version()
    results["env"] = check_env_file()
    results["deps"] = check_dependencies()
    results["gemini"] = check_gemini_api()
    results["clickup"] = check_clickup_api()
    results["database"] = check_database()
    results["mcp_files"] = check_mcp_server()
    results["mcp_tools"] = count_mcp_tools()

    # Summary
    print_header("Validation Summary")

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for check, status in results.items():
        if status:
            print_success(f"{check.replace('_', ' ').title()}: PASSED")
        else:
            print_error(f"{check.replace('_', ' ').title()}: FAILED")

    print(f"\n{Colors.BOLD}Result: {passed}/{total} checks passed{Colors.END}")

    if passed == total:
        print(f"\n{Colors.GREEN}✓ All checks passed! Your setup is ready.{Colors.END}")
        print(f"\n{Colors.CYAN}Next Steps:{Colors.END}")
        print("  1. Start MCP Server:  python -m app.mcp.mcp_server")
        print("  2. Start SLM Client:  python slm_client.py")
    else:
        print(
            f"\n{Colors.YELLOW}⚠ Some checks failed. Please fix the issues above.{Colors.END}"
        )

    return passed == total


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
