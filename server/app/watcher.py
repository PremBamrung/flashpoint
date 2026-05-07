import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from .database import SessionLocal
from .indexer import index_paths

DEBOUNCE_SECONDS = 10


class _Handler(FileSystemEventHandler):
    def __init__(self) -> None:
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(event.dest_path)

    def _schedule(self, path: str) -> None:
        with self._lock:
            self._pending.add(path)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            paths = list(self._pending)
            self._pending.clear()
        if not paths:
            return
        db = SessionLocal()
        try:
            index_paths(paths, db)
        finally:
            db.close()


_observer: Observer | None = None


def start_watcher(roots: list[str]) -> None:
    global _observer
    if not roots:
        return
    handler = _Handler()
    _observer = Observer()
    for root in roots:
        if Path(root).exists():
            _observer.schedule(handler, root, recursive=True)
    _observer.start()


def stop_watcher() -> None:
    global _observer
    if _observer:
        _observer.stop()
        _observer.join()
        _observer = None
