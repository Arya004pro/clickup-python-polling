import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import subprocess
import os


class RestartHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if event.src_path.endswith(".py"):
            print(f"File changed: {event.src_path} → restarting server")
            os.system("taskkill /f /im python.exe >nul 2>&1")  # stop previous
            time.sleep(1)
            subprocess.Popen(["python", "-m", "app.mcp.mcp_server"])


if __name__ == "__main__":
    path = "app"
    event_handler = RestartHandler()
    observer = Observer()
    observer.schedule(event_handler, path, recursive=True)
    observer.start()
    print("Watching for changes in app/ ...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
