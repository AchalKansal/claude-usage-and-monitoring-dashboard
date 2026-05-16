"""
File-system watcher: monitors ~/.claude/projects/ for new/modified session files
and triggers re-parsing automatically.
"""
import time
import threading
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent

import parser as session_parser
import database as db

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
DEBOUNCE_SECONDS = 3.0


class SessionFileHandler(FileSystemEventHandler):
    def __init__(self):
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def on_modified(self, event):
        if isinstance(event, (FileModifiedEvent, FileCreatedEvent)):
            self._queue(event.src_path)

    def on_created(self, event):
        self._queue(event.src_path)

    def _queue(self, path: str):
        if not path.endswith(".jsonl"):
            return
        with self._lock:
            self._pending[path] = time.time()
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self):
        with self._lock:
            paths = list(self._pending.keys())
            self._pending.clear()

        for path in paths:
            p = Path(path)
            if not p.exists() or not p.suffix == ".jsonl":
                continue
            project_dir = p.parent
            project = session_parser.project_name_from_path(project_dir.name)
            count = session_parser.parse_session_file(project, p)
            if count > 0:
                print(f"[watcher] Parsed {count} new message(s) from {p.name}")


def start_watcher() -> Observer:
    db.init_db()
    handler = SessionFileHandler()
    observer = Observer()
    observer.schedule(handler, str(CLAUDE_PROJECTS_DIR), recursive=True)
    observer.start()
    print(f"[watcher] Watching {CLAUDE_PROJECTS_DIR}")
    return observer
