from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QThread, Signal


def _lower_worker_priority() -> None:
    try:
        QThread.currentThread().setPriority(QThread.Priority.LowPriority)
    except Exception:
        pass


class OCRWorker(QThread):
    text_ready = Signal(object)
    error = Signal(str)
    progress = Signal(int, str)

    def __init__(self, pdf_path: str, cache_dir: str | Path | None = None):
        super().__init__()
        self.pdf_path = pdf_path
        self.cache_dir = Path(cache_dir) if cache_dir else None

    def run(self):
        _lower_worker_priority()
        try:
            from app.ocr import OCR, OCRError
            from app.parser_cache import ParserCache

            if self.isInterruptionRequested():
                return

            cache = ParserCache(self.cache_dir) if self.cache_dir else None
            if cache is not None:
                self.progress.emit(2, "Проверка кэша распознавания...")
                try:
                    cache_key, cached_ocr = cache.load_ocr(self.pdf_path)
                except OSError:
                    cache_key, cached_ocr = "", None
                if cached_ocr is not None:
                    self.progress.emit(55, "OCR взят из локального кэша.")
                    self.text_ready.emit(cached_ocr)
                    return
            else:
                cache_key = ""

            self.progress.emit(2, "Подготовка OCR...")
            ocr = OCR()
            if self.isInterruptionRequested():
                return
            ocr_data = ocr.recognize_pdf(
                self.pdf_path,
                progress_callback=self.progress.emit,
            )
            if self.isInterruptionRequested():
                return
            if not ocr_data.get("text", "").strip():
                raise OCRError("Не удалось распознать текст документа.")

            if cache is not None:
                try:
                    cache_key = cache.save_ocr(
                        self.pdf_path, ocr_data, cache_key=cache_key or None
                    )
                    ocr_data = dict(ocr_data)
                    ocr_data["_cache_key"] = cache_key
                except OSError:
                    pass

            self.progress.emit(55, "OCR завершён.")
            self.text_ready.emit(ocr_data)
        except Exception as error:
            if not self.isInterruptionRequested():
                self.error.emit(str(error))


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
            from app.document_parser import DocumentParser, PARSER_VERSION
            from app.parser_cache import ParserCache

            if self.isInterruptionRequested():
                return

            cache = ParserCache(self.cache_dir) if self.cache_dir and self.pdf_path else None
            cache_key = ""
            if isinstance(self.ocr_data, dict):
                cache_key = str(self.ocr_data.get("_cache_key", "") or "")

            if cache is not None:
                try:
                    cached = cache.load_parse(
                        self.pdf_path,
                        parser_version=PARSER_VERSION,
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
            parser = DocumentParser()
            request = parser.parse(self.ocr_data, file_name=self.file_name)
            if self.isInterruptionRequested():
                return
            self.progress.emit(88, "Проверка результата...")
            diagnostics = asdict(parser.last_diagnostics)

            if cache is not None:
                try:
                    if not cache_key:
                        cache_key = cache.save_ocr(self.pdf_path, self.ocr_data)
                    cache.save_parse(
                        self.pdf_path,
                        request,
                        diagnostics,
                        parser_version=PARSER_VERSION,
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
