from __future__ import annotations

from contextlib import contextmanager
import threading
import time


class BackgroundTaskCoordinator:
    """Serialize heavyweight maintenance/OCR work in economy mode.

    The coordinator does not prevent lightweight UI/database reads. It only
    protects tasks that may scan many files, compress backups or load OCR models.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: set[str] = set()

    def is_busy(self, *, exclude: str | None = None) -> bool:
        with self._lock:
            return any(name != exclude for name in self._active)

    def try_start(self, name: str) -> bool:
        with self._lock:
            if self._active:
                return False
            self._active.add(name)
            return True

    def finish(self, name: str) -> None:
        with self._lock:
            self._active.discard(name)

    def wait_start(self, name: str, *, timeout: float = 30.0, poll: float = 0.2) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if self.try_start(name):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(max(0.05, float(poll)))

    @contextmanager
    def task(self, name: str):
        if not self.try_start(name):
            yield False
            return
        try:
            yield True
        finally:
            self.finish(name)


COORDINATOR = BackgroundTaskCoordinator()
