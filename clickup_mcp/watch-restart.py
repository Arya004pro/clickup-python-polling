import time
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Ensure project root is in PYTHONPATH
ROOT = Path(__file__).resolve().parent.parent
os.environ["PYTHONPATH"] = str(ROOT)

# Use the fastmcp executable from the same venv as the current Python
FASTMCP = str(Path(sys.executable).parent / "fastmcp")

SERVER_CMD = [
    FASTMCP,
    "run",
    "clickup_mcp/mcp_server.py:mcp",
    "--transport",
    "sse",
    "--port",
    "8001",
]

# How long (seconds) to wait after the last file change before restarting.
# Prevents killing the server mid-report due to a stray file save.
DEBOUNCE_SECONDS = 3.0

server_process = None
_pending_restart = None
_restart_lock = threading.Lock()
_file_signatures: Dict[str, Tuple[int, int]] = {}


def _normalize_path(path: str) -> str:
    """Normalize paths for stable cache keys on Windows/macOS/Linux."""
    return os.path.normcase(os.path.abspath(path))


def _py_file_signature(path: str) -> Optional[Tuple[int, int]]:
    """
    Lightweight signature for real writes:
    - mtime_ns changes when file content is saved
    - size catches edge cases where mtime granularity is coarse
    """
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _seed_file_signatures(watch_path: str) -> None:
    """Snapshot existing .py files so open/read metadata events are ignored."""
    for py_file in Path(watch_path).rglob("*.py"):
        if "__pycache__" in str(py_file):
            continue
        sig = _py_file_signature(str(py_file))
        if sig is not None:
            _file_signatures[_normalize_path(str(py_file))] = sig


def start_server():
    global server_process
    print("Starting MCP server...")
    server_process = subprocess.Popen(SERVER_CMD, cwd=str(ROOT))


def _do_restart():
    """Perform the actual server restart (called after debounce period)."""
    global server_process
    print("Restarting MCP server...")
    if server_process and server_process.poll() is None:
        server_process.terminate()
        try:
            server_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_process.kill()
    time.sleep(1)
    start_server()


def schedule_restart(src_path: str):
    """Schedule a restart after DEBOUNCE_SECONDS, cancelling any pending restart."""
    global _pending_restart
    with _restart_lock:
        if _pending_restart is not None:
            _pending_restart.cancel()
        _pending_restart = threading.Timer(DEBOUNCE_SECONDS, _do_restart)
        _pending_restart.daemon = True
        _pending_restart.start()
    print(f"File changed: {src_path} -> restart scheduled in {DEBOUNCE_SECONDS}s")


class RestartHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if event.is_directory:
            return
        # Ignore __pycache__ and .pyc files
        if "__pycache__" in event.src_path or event.src_path.endswith(".pyc"):
            return
        if not event.src_path.endswith(".py"):
            return

        normalized = _normalize_path(event.src_path)
        current_sig = _py_file_signature(event.src_path)
        if current_sig is None:
            return

        previous_sig = _file_signatures.get(normalized)
        if previous_sig == current_sig:
            # Likely editor focus/read/metadata noise; no real save occurred.
            return

        _file_signatures[normalized] = current_sig
        schedule_restart(event.src_path)


if __name__ == "__main__":
    watch_path = str(ROOT / "clickup_mcp")
    _seed_file_signatures(watch_path)
    start_server()
    event_handler = RestartHandler()
    observer = Observer()
    observer.schedule(event_handler, watch_path, recursive=True)
    observer.start()
    print(f"Watching for changes in clickup_mcp/ (debounce: {DEBOUNCE_SECONDS}s) ...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
        with _restart_lock:
            if _pending_restart is not None:
                _pending_restart.cancel()
        if server_process and server_process.poll() is None:
            server_process.terminate()
        observer.stop()
    observer.join()
