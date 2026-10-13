from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.parser_cache import ParserCache
from app.background_task_coordinator import COORDINATOR
from app.resource_profile import resource_profile_from_environment

logger = logging.getLogger(__name__)


class SearchIndexBackfillWorker(QThread):
    """Backfill OCR search text from the existing parser cache without re-running OCR."""

    completed = Signal(int, int)

    def __init__(self, request_service, cache_dir: str | Path, parent=None):
        super().__init__(parent)
        self.request_service = request_service
        self.cache_dir = Path(cache_dir)

    def run(self):
        try:
            self.setPriority(QThread.Priority.LowPriority)
        except Exception:
            pass
        indexed = 0
        inspected = 0
        task_started = False
        try:
            profile = resource_profile_from_environment()
            if profile.economy_memory_policy:
                task_started = COORDINATOR.try_start("search_index")
                if not task_started:
                    # -1 tells the controller this was deferred rather than completed.
                    inspected = -1
                    return
            cache = ParserCache(self.cache_dir)
            after_id = 0
            while not self.isInterruptionRequested():
                batch = self.request_service.database.get_requests_missing_ocr_text_page(
                    after_id=after_id, limit=20
                )
                if not batch:
                    break
                for request in batch:
                    if self.isInterruptionRequested():
                        break
                    if request.id is None:
                        continue
                    after_id = max(after_id, int(request.id))
                    path = Path(request.source_document)
                    if not path.is_file():
                        continue
                    inspected += 1
                    try:
                        _key, ocr = cache.load_ocr(path)
                    except OSError:
                        continue
                    if not ocr:
                        continue
                    text = str(ocr.get("text", "") or "").strip()
                    if not text:
                        continue
                    try:
                        self.request_service.database.set_request_ocr_text(request.id, text)
                        indexed += 1
                    except Exception:
                        logger.exception("Не удалось восстановить OCR-поиск для запроса %s", request.id)
                    finally:
                        ocr = None
                # Economy mode intentionally yields between tiny batches. The
                # worker remains low priority and never materializes the archive.
                if profile.economy_memory_policy:
                    self.msleep(25)

            # Fingerprints for pre-v1.27 rows are upgraded in tiny idle batches.
            # Three streamed files per startup is enough to make progress without
            # turning application launch into a disk scan.
            if not self.isInterruptionRequested():
                try:
                    self.request_service.backfill_document_fingerprints(limit=3)
                except Exception:
                    logger.exception("Не удалось постепенно обновить fingerprints документов")
        except Exception:
            logger.exception("Ошибка фонового восстановления полнотекстового индекса")
        finally:
            if task_started:
                COORDINATOR.finish("search_index")
            self.completed.emit(indexed, inspected)
