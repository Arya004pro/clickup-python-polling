import time
import os
import subprocess
import sys
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

server_process = None


def start_server():
    global server_process
    print("Starting MCP server...")
    server_process = subprocess.Popen(SERVER_CMD, cwd=str(ROOT))


def restart_server():
    global server_process
    print("Restarting MCP server...")
    if server_process and server_process.poll() is None:
        server_process.terminate()
        server_process.wait()
    time.sleep(1)
    start_server()


class RestartHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if event.src_path.endswith(".py"):
            print(f"File changed: {event.src_path} → restarting server")
            restart_server()


if __name__ == "__main__":
    watch_path = str(ROOT / "clickup_mcp")
    start_server()
    event_handler = RestartHandler()
    observer = Observer()
    observer.schedule(event_handler, watch_path, recursive=True)
    observer.start()
    print(f"Watching for changes in clickup_mcp/ ...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
        if server_process and server_process.poll() is None:
            server_process.terminate()
        observer.stop()
    observer.join()
