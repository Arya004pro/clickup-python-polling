import time
import os
import subprocess
import sys
import threading
from pathlib import Path
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
        if event.src_path.endswith(".py"):
            schedule_restart(event.src_path)


if __name__ == "__main__":
    watch_path = str(ROOT / "clickup_mcp")
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