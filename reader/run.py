import sys
import os

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
import uvicorn
import webbrowser
import threading
import time


print(sys.executable)


def open_browser():
    time.sleep(1.0)
    webbrowser.open("http://localhost:8765")


if __name__ == "__main__":
    import os
    import sys

    # Force the PROJECT ROOT to be the absolute first entry in the path
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if project_root in sys.path:
        sys.path.remove(project_root)
    sys.path.insert(0, project_root)

    # Remove the 'reader' subfolder from path if it was added automatically
    if os.path.join(project_root, "reader") in sys.path:
        sys.path.remove(os.path.join(project_root, "reader"))

    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run("reader.server:app", host="127.0.0.1", port=8765, reload=False)
