# ClickUp MCP Server with Multi-Provider AI Client

> **Transform ClickUp project management with natural language AI** - A production-ready Model Context Protocol (MCP) server with 54 specialized tools, supporting Groq, Gemini, and Ollama.

<div align="center">

![Python Version](https://img.shields.io/badge/python-3.11.9+-blue.svg)
![MCP Protocol](https://img.shields.io/badge/MCP-FastMCP%203.0-green.svg)
![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Tools](https://img.shields.io/badge/tools-54-orange.svg)

</div>

---

## 📋 Table of Contents

- [Overview](#-overview)
- [Architecture](#-architecture)
- [AI Provider Comparison](#-ai-provider-comparison)
- [Features](#-features)
- [Quick Start Guide](#-quick-start-guide)
- [Detailed Setup Instructions](#-detailed-setup-instructions)
- [MCP Server Tools](#-mcp-server-tools)
- [SLM Client Usage](#-slm-client-usage)
- [Configuration Reference](#-configuration-reference)
- [Troubleshooting](#-troubleshooting)
- [API Reference](#-api-reference)

---

## 🚀 Overview

This project provides a comprehensive **Model Context Protocol (MCP) Server** that bridges your ClickUp workspace with AI language models. The client supports **multiple AI providers** with automatic tool calling and zero data truncation.

### What is MCP?

The Model Context Protocol (MCP) is an open standard that enables AI models to interact with external tools and data sources in a structured, secure way. This server exposes 54 ClickUp-related tools that any MCP-compatible AI can use.

---

## 🤖 AI Provider Comparison

| Provider    | Free Tier Limit    | Context Window | Speed      | Best For                 |
| ----------- | ------------------ | -------------- | ---------- | ------------------------ |
| **🏆 GROQ** | **14,400 req/day** | 128K tokens    | ⚡ Fastest | **Testing & Production** |
| GEMINI      | ~50 req/day        | 1M tokens      | Fast       | Large context needs      |
| OLLAMA      | **Unlimited**      | 128K tokens    | Varies     | Privacy & offline use    |

### Why Groq is Recommended (Primary Provider)

| Feature               | Groq (Llama 3.3 70B) | Gemini 2.0 Flash |
| --------------------- | -------------------- | ---------------- |
| **Free Requests/Day** | 14,400 ✅            | ~50 ❌           |
| **Context Window**    | 128K tokens          | 1M tokens        |
| **Inference Speed**   | 100+ tokens/sec      | ~50 tokens/sec   |
| **Tool Calling**      | Native support       | Native support   |
| **Accuracy**          | Excellent            | Excellent        |
| **Rate Limit Issues** | Rare                 | Very common      |

---

## 🏗 Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         USER (Natural Language)                  │
│                    "Show me overdue tasks in Marketing"          │
└─────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────┐
│                    SLM CLIENT (slm_client.py)                    │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  │
│  │ Knowledge Graph │  │  LLM Provider   │  │   MCP Client    │  │
│  │   (Context)     │  │ Groq/Gemini/    │  │   (Protocol)    │  │
│  │                 │  │    Ollama       │  │                 │  │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼ (SSE Transport)
┌─────────────────────────────────────────────────────────────────┐
│                    MCP SERVER (mcp_server.py)                    │
│  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐        │
│  │  Workspace    │  │    Task       │  │  PM Analytics │        │
│  │  Structure    │  │  Management   │  │    Tools      │        │
│  │  (10 tools)   │  │  (12 tools)   │  │  (9 tools)    │        │
│  └───────────────┘  └───────────────┘  └───────────────┘        │
│  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐        │
│  │   Project     │  │   Project     │  │  Sync &       │        │
│  │   Config      │  │ Intelligence  │  │  Mapping      │        │
│  │  (7 tools)    │  │  (12 tools)   │  │  (10 tools)   │        │
│  └───────────────┘  └───────────────┘  └───────────────┘        │
└─────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
            ┌───────────┐   ┌───────────┐   ┌───────────┐
            │  ClickUp  │   │ Supabase  │   │   Local   │
            │    API    │   │ PostgreSQL│   │   Cache   │
            └───────────┘   └───────────┘   └───────────┘
```

---

## ✨ Features

### 54 MCP Tools Organized in 6 Categories

<details>
<summary><b>🏢 Workspace Structure (10 tools)</b></summary>

| Tool                   | Description                    |
| ---------------------- | ------------------------------ |
| `get_workspaces`       | List all accessible workspaces |
| `get_spaces`           | List spaces in a workspace     |
| `get_space`            | Get detailed space information |
| `get_folders`          | List folders in a space        |
| `get_folder`           | Get folder details with lists  |
| `get_lists`            | List all lists in a folder     |
| `get_folderless_lists` | List direct space lists        |
| `get_list`             | Get detailed list information  |
| `invalidate_cache`     | Clear cached data              |

</details>

<details>
<summary><b>📝 Task Management (12 tools)</b></summary>

| Tool                | Description                |
| ------------------- | -------------------------- |
| `get_tasks`         | List tasks with filters    |
| `get_task`          | Get detailed task info     |
| `create_task`       | Create a new task          |
| `update_task`       | Update task properties     |
| `search_tasks`      | Search across project      |
| `get_project_tasks` | Get all project tasks      |
| `get_list_progress` | Sprint progress summary    |
| `get_workload`      | Team workload distribution |
| `get_overdue_tasks` | Find overdue tasks         |

</details>

<details>
<summary><b>📊 PM Analytics (9 tools)</b></summary>

| Tool                       | Description                 |
| -------------------------- | --------------------------- |
| `get_progress_since`       | Progress since date         |
| `get_time_tracking_report` | Time tracking summary       |
| `get_task_time_breakdown`  | Detailed time breakdown     |
| `get_estimation_accuracy`  | Estimate vs actual analysis |
| `get_at_risk_tasks`        | Find at-risk tasks          |
| `get_stale_tasks`          | Find stagnant tasks         |
| `get_untracked_tasks`      | Tasks without time logged   |
| `get_inactive_assignees`   | Inactive team members       |
| `get_status_summary`       | Status distribution         |

</details>

<details>
<summary><b>⚙️ Project Configuration (7 tools)</b></summary>

| Tool                      | Description             |
| ------------------------- | ----------------------- |
| `discover_projects`       | Scan for projects       |
| `add_project`             | Track a project         |
| `list_projects`           | List tracked projects   |
| `remove_project`          | Untrack project         |
| `refresh_projects`        | Verify tracked projects |
| `get_project_status`      | Project status metrics  |
| `get_all_projects_status` | All projects overview   |

</details>

<details>
<summary><b>🧠 Project Intelligence (12 tools)</b></summary>

| Tool                             | Description              |
| -------------------------------- | ------------------------ |
| `get_workspace_folderless_lists` | Find folderless lists    |
| `get_list_defined_statuses`      | List status definitions  |
| `get_project_statuses`           | Project status workflow  |
| `get_project_health_score`       | Health score (A-F grade) |
| `get_project_daily_standup`      | Daily standup report     |
| `get_project_time_tracking`      | Project time report      |
| `get_project_blockers`           | Identify blockers        |
| `get_project_at_risk`            | Risk assessment          |
| `get_project_weekly_digest`      | Weekly summary           |
| `get_project_team_workload`      | Team workload analysis   |

</details>

<details>
<summary><b>🔄 Sync & Mapping (10 tools)</b></summary>

| Tool                   | Description              |
| ---------------------- | ------------------------ |
| `discover_hierarchy`   | Full workspace tree      |
| `map_project`          | Map ClickUp entity       |
| `list_mapped_projects` | Show mappings            |
| `get_mapped_project`   | Get mapping details      |
| `refresh_project`      | Refresh mapping          |
| `unmap_project`        | Remove mapping           |
| `get_sync_status`      | Sync health check        |
| `list_spaces`          | Spaces with mapping info |
| `clear_sync`           | Reset all mappings       |
| `prune_cache`          | Clean expired cache      |

</details>

---

## ⚡ Quick Start Guide

### Prerequisites

- Python 3.11.9+ (tested and recommended)
- ClickUp account with API access
- PostgreSQL database (Supabase recommended)
- An API Key from Gemini, Groq, or Ollama


### 5-Minute Setup

```bash
# 1. Clone the repository
git clone https://github.com/Arya004pro/clickup-python-polling.git
cd clickup-python-polling

# 2. Create Python virtual environment
python -m venv myenv
myenv\Scripts\activate  # Windows
# source myenv/bin/activate  # Linux/Mac

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create .env file
copy .env.example .env
# Edit .env with your credentials (see below)

# 5. Validate setup
python check_models.py

# 6. Start MCP Server (Terminal 1)
$env:PYTHONPATH = (Get-Location).Path
fastmcp run app/mcp/mcp_server.py:mcp --transport sse --port 8001

# 7. Start SLM Client (Terminal 2)
python slm_client.py
```

### Minimum .env Configuration

```bash
# Required
CLICKUP_API_TOKEN=pk_YOUR_TOKEN_HERE
DATABASE_URL=postgresql://user:pass@host:6543/postgres

# AI Provider (choose one)
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_YOUR_KEY_HERE  # Get from https://console.groq.com/keys
```

---

## 📖 Detailed Setup Instructions

### Step 1: Install Python 3.11.9

<details>
<summary><b>Windows Installation</b></summary>

1. Download Python 3.11.9 from [python.org](https://www.python.org/downloads/release/python-3119/)
2. Run installer with these options checked:
   - ✅ Add Python to PATH
   - ✅ Install pip
   - ✅ Install for all users (recommended)
3. Verify installation:
   ```powershell
   python --version
   # Should show: Python 3.11.9
   ```

</details>

<details>
<summary><b>Linux/Mac Installation</b></summary>

```bash
# Using pyenv (recommended)
curl https://pyenv.run | bash

# Add to ~/.bashrc or ~/.zshrc:
export PATH="$HOME/.pyenv/bin:$PATH"
eval "$(pyenv init -)"

# Install Python 3.11.9
pyenv install 3.11.9
pyenv global 3.11.9

# Verify
python --version
```

</details>

### Step 2: Create Virtual Environment

```bash
# Navigate to project directory
cd clickup-python-polling

# Create virtual environment
python -m venv myenv

# Activate (Windows PowerShell)
myenv\Scripts\Activate.ps1

# Activate (Windows CMD)
myenv\Scripts\activate.bat

# Activate (Linux/Mac)
source myenv/bin/activate

# Verify activation (you should see (myenv) in prompt)
```

### Step 3: Install Dependencies

```bash
# Upgrade pip first
python -m pip install --upgrade pip

# Install all requirements
pip install -r requirements.txt

# Verify key packages
python -c "import fastmcp; print(f'FastMCP: {fastmcp.__version__}')"
python -c "import google.generativeai; print('Gemini SDK: OK')"
```

### Step 4: Get API Keys

#### ClickUp API Token

1. Go to [ClickUp Settings](https://app.clickup.com/settings/apps)
2. Navigate to **Apps** → **API Token**
3. Click **Generate** or copy existing token
4. Token format: `pk_123456_XXXXXXXXXXXXXXXXXXXXXXXXX`

#### ClickUp Team ID

1. Open ClickUp in browser
2. Navigate to any space
3. Copy the team ID from URL: `https://app.clickup.com/{TEAM_ID}/v/...`

#### AI Provider API Keys

##### 🏆 Groq API Key (RECOMMENDED - 14,400 requests/day FREE)

1. Go to [Groq Console](https://console.groq.com/keys)
2. Sign up/Login (free)
3. Click **Create API Key**
4. Copy the key (starts with `gsk_...`)

##### Google Gemini API Key (Fallback - only ~50 requests/day)

1. Go to [Google AI Studio](https://aistudio.google.com/apikey)
2. Click **Create API Key**
3. Copy the key (starts with `AIza...`)

##### Ollama (Local - Unlimited, requires GPU)

1. Install from [ollama.ai](https://ollama.ai)
2. Run: `ollama pull llama3.1:8b`
3. No API key needed

#### Supabase/PostgreSQL

1. Create project at [Supabase](https://supabase.com)
2. Go to **Settings** → **Database**
3. Copy the **Connection string** (use Transaction Pooler for production)

### Step 5: Configure Environment

Create a `.env` file in the project root:

```env
# ===========================================
# REQUIRED CONFIGURATION
# ===========================================

# ClickUp API Token (from ClickUp Settings > Apps > API Token)
CLICKUP_API_TOKEN=pk_123456_XXXXXXXXXXXXXXXXXXXXXXXXXXXX

# PostgreSQL/Supabase Connection String
# Format: postgresql://user:password@host:port/database
DATABASE_URL=postgresql://postgres.xxxxx:password@aws-0-region.pooler.supabase.com:6543/postgres

# ===========================================
# AI PROVIDER CONFIGURATION
# ===========================================

# Select provider: groq (recommended), gemini, or ollama
LLM_PROVIDER=groq

# Groq API Key (14,400 requests/day FREE - https://console.groq.com/keys)
GROQ_API_KEY=gsk_XXXXXXXXXXXXXXXXXXXXXXXX
GROQ_MODEL=llama-3.3-70b-versatile

# Gemini API Key (only ~50 requests/day - https://aistudio.google.com/apikey)
# GEMINI_API_KEY=AIzaSyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX

# Ollama (local, unlimited)
# OLLAMA_BASE_URL=http://localhost:11434
# OLLAMA_MODEL=llama3.1:8b

# MCP Server URL
MCP_SERVER_URL=http://127.0.0.1:8001/sse

# ===========================================
# OPTIONAL CONFIGURATION
# ===========================================

# ClickUp Team/Workspace ID (auto-detected if not set)
CLICKUP_TEAM_ID=12345678

# ClickUp Space ID (optional - for filtering)
CLICKUP_SPACE_ID=
```

### Step 6: Validate Setup

Run the comprehensive validation script:

```bash
python check_models.py
```

Expected output:

```
============================================================
         ClickUp MCP Server - Setup Validator
============================================================

▶ Python Version
  ✓ Python 3.11.9 ✓ (Recommended: 3.11.9)

▶ Environment Configuration (.env)
  ✓ .env file exists
  ✓ CLICKUP_API_TOKEN: pk_1***...***XX
  ✓ DATABASE_URL: post***...***gres
  ✓ GROQ_API_KEY: gsk_***...***XX

▶ Python Dependencies
  ✓ fastapi
  ✓ uvicorn
  ✓ fastmcp
  ... (all packages)

▶ Google Gemini API
  ✓ API Key configured
  ✓ models/gemini-2.0-flash
  ✓ Recommended model available

▶ ClickUp API
  ✓ API Token valid
  ✓ Found 1 workspace(s)
    • My Workspace (ID: 12345678)

▶ MCP Tools Inventory
  ✓ Total Tools Registered: 54
  ... (tool categories)

============================================================
               Validation Summary
============================================================
Result: 8/8 checks passed

✓ All checks passed! Your setup is ready.

Next Steps:
  1. Start MCP Server:  python -m app.mcp.mcp_server
  2. Start SLM Client:  python slm_client.py
```

### Step 7: Start the System

**Terminal 1 - MCP Server:**

```bash
python -m app.mcp.mcp_server
```

Expected output:

```
Starting ClickUp MCP Server...
INFO:     Started server process
INFO:     Uvicorn running on http://0.0.0.0:8001
```

**Terminal 2 - SLM Client:**

```bash
python slm_client.py
```

Expected output:

```
============================================================
🚀 ClickUp MCP Server - SLM Client (Gemini 2.0 Flash)
============================================================

🔗 Connecting to MCP Server: http://127.0.0.1:8001/sse

🧠 Building Knowledge Graph...
   ✓ Mapped 1 Workspaces
   ✓ Mapped 5 Spaces
   ✓ Knowledge Graph Ready (6 entities)

✅ Loaded 54 MCP tools
🤖 Gemini gemini-2.0-flash initialized successfully!

============================================================
💬 Chat Ready! Type 'quit' to exit, 'help' for commands
============================================================

📝 You:
```

---

## 💬 SLM Client Usage

### Available Commands

| Command | Description             |
| ------- | ----------------------- |
| `help`  | Show available commands |
| `tools` | List all 54 MCP tools   |
| `quit`  | Exit the application    |

### Example Queries

```
📝 You: Show me all workspaces

🤖 Assistant:
Found 1 workspace:
| Workspace | ID | Members | Plan |
|-----------|-----|---------|------|
| My Company | 12345678 | 15 | Business |
```

```
📝 You: What's the project health for Marketing?

🤖 Assistant:
📊 Project Health Score: Marketing

Score: 78/100 (Grade: B)

Breakdown:
• Overdue Tasks: 85% ✓
• Freshness: 72% ⚠
• Progress: 65%
• Assignment Coverage: 90% ✓
• Time Estimates: 55% ⚠

Recommendations:
1. Update stale tasks (7 untouched >5 days)
2. Add time estimates to 12 tasks
```

```
📝 You: Generate daily standup for Development

🤖 Assistant:
📋 Daily Standup - Development
Date: 2026-02-01

✅ Completed Yesterday:
• API endpoint optimization (John)
• Bug fix: Login timeout (Sarah)

🔄 In Progress:
• Dashboard redesign (Mike) - 60% complete
• Database migration (John) - Blocked: waiting for credentials

🚫 Blockers:
• Database migration blocked - needs DevOps support

📅 Due Today:
• Code review for PR #142 (Sarah)
```

### Advanced Queries

```
# Time tracking analysis
📝 You: Show time tracking report grouped by assignee for Q1

# Risk assessment
📝 You: Find all tasks that are overdue or due within 3 days

# Search functionality
📝 You: Search for tasks mentioning "API" or "integration"

# Team analysis
📝 You: Who has the highest workload? Show task distribution
```

---

## ⚙️ Configuration Reference

### Environment Variables

| Variable            | Required | Default                     | Description                  |
| ------------------- | -------- | --------------------------- | ---------------------------- |
| `CLICKUP_API_TOKEN` | ✅ Yes   | -                           | ClickUp personal API token   |
| `DATABASE_URL`      | ✅ Yes   | -                           | PostgreSQL connection string |
| `GEMINI_API_KEY`    | ⚠️ SLM   | -                           | Google AI API key for SLM    |
| `MCP_SERVER_URL`    | No       | `http://127.0.0.1:8001/sse` | MCP server endpoint          |
| `CLICKUP_TEAM_ID`   | No       | Auto-detected               | Default workspace ID         |
| `CLICKUP_SPACE_ID`  | No       | -                           | Default space ID             |

### Model Selection

The SLM client uses **Gemini 2.0 Flash** by default. To change:

```python
# In slm_client.py
MODEL_NAME = "gemini-2.0-flash"  # Recommended
# OR
MODEL_NAME = "gemini-1.5-pro"    # More capable, slower
# OR
MODEL_NAME = "gemini-1.5-flash"  # Fallback
```

### Alternative SLM Providers

If you prefer other providers, modify `slm_client.py`:

<details>
<summary><b>Groq (Llama 3.3 70B)</b></summary>

```python
from openai import AsyncOpenAI

client = AsyncOpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

# Free tier: 100K tokens/min
# Model: llama-3.3-70b-versatile
```

</details>

<details>
<summary><b>OpenRouter (Multiple Models)</b></summary>

```python
from openai import AsyncOpenAI

client = AsyncOpenAI(
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1"
)

# Supports: Claude, GPT-4, Llama, Mistral, etc.
# Free tier available for some models
```

</details>

---

## 🔧 Troubleshooting

### Common Issues

<details>
<summary><b>❌ MCP Server won't start</b></summary>

**Error:** `Address already in use`

```bash
# Find and kill process on port 8001
netstat -ano | findstr :8001
taskkill /PID <PID> /F
```

**Error:** `ModuleNotFoundError`

```bash
# Ensure virtual environment is activated
myenv\Scripts\activate
pip install -r requirements.txt
```

</details>

<details>
<summary><b>❌ SLM Client can't connect</b></summary>

**Error:** `ConnectionRefusedError`

1. Ensure MCP server is running in another terminal
2. Check the URL in `.env`: `MCP_SERVER_URL=http://127.0.0.1:8001/sse`
3. Try `http://localhost:8001/sse` instead

</details>

<details>
<summary><b>❌ Gemini API errors</b></summary>

**Error:** `API key invalid`

1. Verify key at [Google AI Studio](https://aistudio.google.com/apikey)
2. Create a new key if needed
3. Ensure no extra spaces in `.env`

**Error:** `Resource exhausted`

- Free tier limit reached (1,500 req/day)
- Wait until next day or upgrade plan

</details>

<details>
<summary><b>❌ ClickUp API errors</b></summary>

**Error:** `401 Unauthorized`

- Token expired - regenerate in ClickUp settings
- Ensure no extra spaces in `.env`

**Error:** `Rate limited`

- ClickUp free tier: 100 requests/minute
- Add delays between bulk operations

</details>

<details>
<summary><b>❌ Database connection issues</b></summary>

**Error:** `Connection refused`

1. Check Supabase project is active
2. Use Transaction Pooler URL (port 6543)
3. Verify password has no special characters (URL encode if needed)

</details>

### Debug Mode

Enable verbose logging:

```bash
# Set environment variable
set DEBUG=true  # Windows
export DEBUG=true  # Linux/Mac

python slm_client.py
```

---

## 📚 API Reference

### MCP Server Endpoints

| Endpoint  | Method | Description                 |
| --------- | ------ | --------------------------- |
| `/sse`    | GET    | SSE stream for MCP protocol |
| `/health` | GET    | Server health check         |

### Tool Response Format

All tools return JSON with this structure:

```json
{
  "success": true,
  "data": { ... },
  "error": null,
  "metadata": {
    "tool": "get_tasks",
    "execution_time_ms": 245
  }
}
```

### Error Response Format

```json
{
  "success": false,
  "data": null,
  "error": "Detailed error message",
  "hint": "Suggested fix or alternative"
}
```

---

## 🗂 Project Structure

```
clickup-python-polling/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI REST endpoints
│   ├── clickup.py           # ClickUp API client
│   ├── config.py            # Configuration loader
│   ├── sync.py              # Task sync logic
│   ├── daily_sync.py        # Daily snapshot sync
│   ├── supabase_db.py       # Database operations
│   ├── scheduler.py         # Background jobs
│   ├── time_tracking.py     # Time entry handling
│   ├── employee_sync.py     # User mapping
│   ├── logging_config.py    # Logging setup
│   └── mcp/
│       ├── __init__.py
│       ├── mcp_server.py           # MCP Server entry point
│       ├── workspace_structure.py  # Workspace tools (10)
│       ├── task_management.py      # Task tools (12)
│       ├── pm_analytics.py         # Analytics tools (9)
│       ├── project_configuration.py # Config tools (7)
│       ├── project_intelligence.py # Intelligence tools (12)
│       └── sync_mapping.py         # Sync tools (10)
├── myenv/                   # Virtual environment
├── .env                     # Environment variables
├── .env.example             # Example configuration
├── requirements.txt         # Python dependencies
├── check_models.py          # Setup validator
├── slm_client.py            # SLM Client (Gemini)
├── project_map.json         # Cached project mappings
└── README.md                # This file
```

---

## 🤝 Contributing

1. Fork the repository
2. Create feature branch: `git checkout -b feature/amazing-feature`
3. Commit changes: `git commit -m 'Add amazing feature'`
4. Push to branch: `git push origin feature/amazing-feature`
5. Open a Pull Request

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- [FastMCP](https://github.com/jlowin/fastmcp) - MCP server framework
- [Google Gemini](https://ai.google.dev/) - AI model provider
- [ClickUp](https://clickup.com/) - Project management platform
- [Supabase](https://supabase.com/) - PostgreSQL database platform

---

## 🛠 Step-by-Step Setup Guide for Testers

Follow these steps to set up the ClickUp MCP Server on your device:

### 1. Prerequisites

- **Python 3.11.9+**: Ensure Python is installed. [Download Python](https://www.python.org/downloads/)
- **Pip**: Comes with Python installation.
- **Virtual Environment**: Recommended for dependency isolation.

### 2. Clone the Repository

```bash
# Clone the repository
$ git clone https://github.com/Arya004pro/clickup-python-polling.git
$ cd clickup-python-polling
```

### 3. Set Up Virtual Environment

```bash
# Create and activate a virtual environment
$ python -m venv myenv
$ source myenv/Scripts/activate  # On Windows
$ source myenv/bin/activate     # On macOS/Linux
```

### 4. Install Dependencies

```bash
# Install required Python packages
$ pip install -r requirements.txt
```

### 5. Configure Environment Variables

- Create a `.env` file in the root directory.
- Add the following variables:
  ```env
  LLM_PROVIDER=cerebras
  CEREBRAS_API_KEY=your_cerebras_api_key
  GROQ_API_KEY=your_groq_api_key
  GEMINI_API_KEY=your_gemini_api_key
  MCP_SERVER_URL=http://127.0.0.1:8001/sse
  ```

### 6. Run the MCP Server

```bash
# Start the MCP server
$ fastmcp run app/mcp/mcp_server.py:mcp --transport sse --port 8001
```

### 7. Test the SLM Client

```bash
# Run the SLM client
$ python slm_client.py
```

### 8. Verify Setup

- Open the terminal and ensure the server and client are running without errors.
- Test basic commands like listing spaces or fetching tasks.

---

For troubleshooting, refer to the [Troubleshooting](#-troubleshooting) section.
