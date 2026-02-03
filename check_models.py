"""Check available models across configured providers.

This script attempts to list models for providers available in the environment:
- Google Gemini (`google.generativeai`) if `GEMINI_API_KEY` is set
- Anthropic Claude (`anthropic`) if installed and `CLAUDE_API_KEY` is set
- Ollama (local) via HTTP to `OLLAMA_BASE_URL` (default localhost)
- Groq (`groq`) if SDK present and `GROQ_API_KEY` is set

The script is best-effort and prints useful diagnostics when SDKs or keys
are missing.
"""

import os
import json
from dotenv import load_dotenv

load_dotenv()

    print("Checking available models for configured providers...")

# --- Gemini (Google) ---
try:
    import google.generativeai as genai

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        print("\nGemini models:")
        try:
            for m in genai.list_models():
                # show models that can generate content
                if (
                    getattr(m, "supported_generation_methods", None)
                    and "generateContent" in m.supported_generation_methods
                ):
                    print(f" - {m.name}")
        except Exception as e:
            print(f"  Error listing Gemini models: {e}")
    else:
        print("\nGemini: GEMINI_API_KEY not set, skipping")
except Exception as e:
    print(f"\nGemini: google.generativeai not available ({e})")

# --- Anthropic / Claude ---
try:
    import anthropic

    CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
    if CLAUDE_API_KEY:
        print("\nClaude / Anthropic models:")
        try:
            # New Anthropic SDKs may provide a models.list() method; try best-effort.
            client = anthropic.Client(api_key=CLAUDE_API_KEY)
            if hasattr(client, "models") and hasattr(client.models, "list"):
                for m in client.models.list():
                    # `m` may be a dict-like or a ModelInfo object depending on SDK
                    model_id = None
                    try:
                        if isinstance(m, dict):
                            model_id = m.get("id") or m.get("name")
                        elif hasattr(m, "id"):
                            model_id = getattr(m, "id")
                        elif hasattr(m, "name"):
                            model_id = getattr(m, "name")
                    except Exception:
                        model_id = None

                    if model_id:
                        print(f" - {model_id}")
                    else:
                        print(f" - {m}")
            else:
                print(
                    "  Anthropic SDK installed but model listing not supported by this client version."
                )
                print("  Suggested models: claude-2.1, claude-instant-v1, claude-3")
        except Exception as e:
            print(f"  Error querying Anthropic models: {e}")
    else:
        print("\nClaude: CLAUDE_API_KEY not set, skipping")
except Exception as e:
    print(f"\nClaude: anthropic SDK not installed ({e})")

# --- Ollama (local) ---
try:
    import requests

    OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    print("\nOllama (local) models:")
    try:
        r = requests.get(f"{OLLAMA_BASE}/api/models", timeout=2)
        if r.status_code == 200:
            models = r.json()
            for m in models:
                print(f" - {m.get('model') or m}")
        else:
            print(f"  Ollama API returned {r.status_code}")
    except Exception as e:
        print(f"  Could not reach Ollama at {OLLAMA_BASE}: {e}")
except Exception as e:
    print(f"\nOllama: requests not available ({e})")

# --- Groq ---
try:
    import groq

    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    if GROQ_API_KEY:
        print("\nGroq models:")
        try:
            client = groq.Client(api_key=GROQ_API_KEY)
            if hasattr(client, "models") and hasattr(client.models, "list"):
                for m in client.models.list():
                    print(f" - {m}")
            else:
                print(
                    "  Groq SDK available but model listing not supported in this version."
                )
        except Exception as e:
            print(f"  Error listing Groq models: {e}")
    else:
        print("\nGroq: GROQ_API_KEY not set, skipping")
except Exception as e:
    print(f"\nGroq: groq SDK not available ({e})")

print("\nDone.")
