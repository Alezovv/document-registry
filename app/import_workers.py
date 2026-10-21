from dataclasses import asdict
from pathlib import Path

from app.background_task_coordinator import COORDINATOR
from app.memory_governor import MemoryGovernor
from app.resource_profile import resource_profile_from_environment

from PySide6.QtCore import QThread, Signal


def _lower_worker_priority() -> None:
    try:
        QThread.currentThread().setPriority(QThread.Priority.LowPriority)
    except Exception:
        pass


def release_shared_ocr_engine() -> bool:
    """Release the process-wide OCR engine without coupling UI code to OCR internals.

    Importing the heavy OCR module stays inside the worker/service layer.  Besides
    preserving the UI architecture boundary, this makes the memory-release action
    safe when OCR dependencies are unavailable during lightweight diagnostics/tests.
    """
    try:
        from app.ocr import OCR

        return bool(OCR.reset_shared_engine())
    except Exception:
        return False


def apply_post_document_memory_policy():
    """Release transient memory after one document without slowing normal mode."""
    governor = MemoryGovernor(resource_profile_from_environment())
    return governor.after_document(release_shared_ocr_engine)


def _release_all_inference_engines() -> bool:
    ocr_released = release_shared_ocr_engine()
    ner_released = False
    try:
        from app.ner_runtime import release_default_ner_engine

        ner_released = bool(release_default_ner_engine())
    except Exception:
        pass
    return ocr_released or ner_released


def warmup_ocr_engine_async() -> None:
    """Pre-warm the shared ONNX OCR session and NER in the background when dialog opens."""
    import threading

    def _task() -> None:
        try:
            governor = MemoryGovernor(resource_profile_from_environment())
            decision = governor.sample()
            if not decision.allow_heavy_task:
                return
            from app.ocr import OCR
            if OCR._shared_ocr is None:
                OCR()._get_engine()
            try:
                from app.ner_runtime import get_default_ner_engine
                get_default_ner_engine()
            except Exception:
                pass
        except Exception:
            pass

    threading.Thread(target=_task, name="ocr-warmup", daemon=True).start()


def finalize_ocr_batch_memory():
    """Unload OCR/NER and return the process to a stable idle footprint after a queue."""
    governor = MemoryGovernor(resource_profile_from_environment())
    return governor.finalize_batch(_release_all_inference_engines)


def prepare_ocr_memory_budget():
    """Create headroom before loading/running OCR in the strict economy profile."""
    governor = MemoryGovernor(resource_profile_from_environment())
    return governor.prepare_heavy_task(release_shared_ocr_engine)


class OCRWorker(QThread):
    text_ready = Signal(object)
    error = Signal(str)
    progress = Signal(int, str)

    def __init__(
        self,
        pdf_path: str,
        cache_dir: str | Path | None = None,
        sha256: str | None = None,
    ):
        super().__init__()
        self.pdf_path = pdf_path
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.sha256 = str(sha256).strip() if sha256 else None

    def run(self):
        _lower_worker_priority()
        profile = resource_profile_from_environment()
        task_started = False
        try:
            if profile.economy_memory_policy:
                self.progress.emit(1, "Ожидание свободного тяжёлого слота...")
                task_started = COORDINATOR.wait_start("ocr", timeout=30.0, poll=0.2)
                if not task_started:
                    self.error.emit(
                        "Экономный режим: другая тяжёлая операция выполняется слишком долго. "
                        "Повторите импорт после её завершения."
                    )
                    return
                self.progress.emit(2, "Проверка запаса памяти...")
                decision = prepare_ocr_memory_budget()
                if not decision.allow_heavy_task:
                    current_mb = decision.current_bytes / (1024 * 1024)
                    self.error.emit(
                        f"Недостаточно свободной памяти для безопасного OCR: приложение использует {current_mb:.0f} МБ. "
                        "Закройте лишние окна/программы и повторите импорт."
                    )
                    return

            from app.ocr import OCR, OCRError
            from app.parser_cache import ParserCache

            if self.isInterruptionRequested():
                return

            cache = ParserCache(self.cache_dir) if self.cache_dir else None
            cache_key = self.sha256 or ""
            if cache is not None:
                self.progress.emit(2, "Проверка кэша распознавания...")
                try:
                    cache_key, cached_ocr = cache.load_ocr(
                        self.pdf_path, cache_key=self.sha256
                    )
                except OSError:
                    cache_key, cached_ocr = self.sha256 or "", None
                if cached_ocr is not None:
                    self.progress.emit(55, "OCR взят из локального кэша.")
                    self.text_ready.emit(cached_ocr)
                    return
            else:
                cache_key = self.sha256 or ""

            from app.document_parser import DocumentParser

            self.progress.emit(2, "Подготовка OCR...")
            ocr = OCR()
            if self.isInterruptionRequested():
                return

            def _progress_callback(val: int, msg: str) -> None:
                if self.isInterruptionRequested():
                    raise InterruptedError("OCR interrupted")
                self.progress.emit(val, msg)

            ocr_data = ocr.recognize_pdf(
                self.pdf_path,
                progress_callback=_progress_callback,
                sufficiency_check=DocumentParser.is_extraction_sufficient,
            )
            if self.isInterruptionRequested():
                return
            if not ocr_data.get("text", "").strip():
                raise OCRError("Не удалось распознать текст документа.")

            if cache is not None:
                try:
                    cache_key = cache.save_ocr(
                        self.pdf_path,
                        ocr_data,
                        cache_key=cache_key or self.sha256 or None,
                    )
                    ocr_data = dict(ocr_data)
                    ocr_data["_cache_key"] = cache_key
                except OSError:
                    pass

            self.progress.emit(55, "OCR завершён.")
            self.text_ready.emit(ocr_data)
        except (InterruptedError, Exception) as error:
            if not self.isInterruptionRequested():
                self.error.emit(str(error))
        finally:
            if task_started:
                COORDINATOR.finish("ocr")


class ParserWorker(QThread):
    result_ready = Signal(object)
    error = Signal(str)
    progress = Signal(int, str)

    def __init__(
        self,
        ocr_data,
        file_name: str = "",
        pdf_path: str | Path | None = None,
        cache_dir: str | Path | None = None,
    ):
        super().__init__()
        self.ocr_data = ocr_data
        self.file_name = file_name
        self.pdf_path = str(pdf_path) if pdf_path else ""
        self.cache_dir = Path(cache_dir) if cache_dir else None

    def run(self):
        _lower_worker_priority()
        try:
            from app.document_parser import DocumentParser, PARSER_VERSION, get_parser_cache_version
            from app.parser_cache import ParserCache
            from app.parser_correction_memory import ParserCorrectionMemory
            from app.parser_review import compact_parser_diagnostics

            if self.isInterruptionRequested():
                return

            cache = ParserCache(self.cache_dir) if self.cache_dir and self.pdf_path else None
            correction_memory = None
            if self.cache_dir is not None:
                correction_memory = ParserCorrectionMemory.for_data_dir(self.cache_dir.parent)
            parser_cache_version = get_parser_cache_version(
                correction_memory.cache_key() if correction_memory else None
            )
            cache_key = ""
            if isinstance(self.ocr_data, dict):
                cache_key = str(self.ocr_data.get("_cache_key", "") or "")

            if cache is not None:
                try:
                    cached = cache.load_parse(
                        self.pdf_path,
                        parser_version=parser_cache_version,
                        cache_key=cache_key or None,
                    )
                except OSError:
                    cached = None
                if cached is not None:
                    request, diagnostics = cached
                    self.progress.emit(88, "Поля взяты из кэша parser-а.")
                    self.result_ready.emit({
                        "request": request,
                        "diagnostics": diagnostics,
                        "cache_hit": True,
                    })
                    return

            self.progress.emit(60, "Извлечение данных из OCR...")
            parser = DocumentParser(correction_memory=correction_memory)
            request = parser.parse(self.ocr_data, file_name=self.file_name)
            if self.isInterruptionRequested():
                return
            self.progress.emit(88, "Проверка результата...")
            diagnostics = compact_parser_diagnostics(asdict(parser.last_diagnostics))

            if cache is not None:
                try:
                    if not cache_key:
                        cache_key = cache.save_ocr(self.pdf_path, self.ocr_data)
                    cache.save_parse(
                        self.pdf_path,
                        request,
                        diagnostics,
                        parser_version=parser_cache_version,
                        cache_key=cache_key,
                    )
                except OSError:
                    pass

            if self.isInterruptionRequested():
                return
            self.result_ready.emit({
                "request": request,
                "diagnostics": diagnostics,
                "cache_hit": False,
            })
        except Exception as error:
            if not self.isInterruptionRequested():
                self.error.emit(str(error))
        finally:
            # OCR blocks can be large. The UI stores only compact diagnostics and
            # text needed for search, so release the worker's reference immediately.
            self.ocr_data = None
