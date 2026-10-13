from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QTimer

from app.search_index_worker import SearchIndexBackfillWorker

logger = logging.getLogger(__name__)


class SearchIndexController(QObject):
    """Start a low-priority OCR-index backfill after UI startup."""

    def __init__(self, request_service, cache_dir: str | Path, parent=None):
        super().__init__(parent)
        self.request_service = request_service
        self.cache_dir = Path(cache_dir)
        self.worker: SearchIndexBackfillWorker | None = None
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)
        QTimer.singleShot(9000, self.start)

    def start(self):
        if self.worker is not None:
            return
        worker = SearchIndexBackfillWorker(self.request_service, self.cache_dir, parent=self)
        self.worker = worker
        worker.completed.connect(self._finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _finished(self, indexed: int, inspected: int):
        if indexed:
            logger.info(
                "Полнотекстовый поиск дополнен из OCR-cache: %s из %s документов",
                indexed,
                inspected,
            )
        self.worker = None
        if inspected < 0:
            # Economy mode postpones maintenance while OCR/backup is active.
            QTimer.singleShot(60000, self.start)

    def shutdown(self):
        worker = self.worker
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
            worker.wait(1500)
