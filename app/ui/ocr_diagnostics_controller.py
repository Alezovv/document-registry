from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QMessageBox

from app.self_check import perform_ocr_runtime_self_check

logger = logging.getLogger(__name__)


class _OcrDiagnosticThread(QThread):
    completed = Signal(bool, str)

    def run(self) -> None:
        try:
            result = perform_ocr_runtime_self_check()
            details = "\n".join(
                ("OK: " if item.ok else "ERROR: ") + item.details
                for item in result.items
            )
            self.completed.emit(result.ok, details)
        except Exception as error:
            logger.exception("OCR diagnostic failed")
            self.completed.emit(False, str(error) or "Не удалось проверить OCR.")


class OcrDiagnosticsController(QObject):
    """Run the real OCR initialization check without blocking the UI."""

    def __init__(self, toast, parent=None):
        super().__init__(parent)
        self.toast = toast
        self._thread: _OcrDiagnosticThread | None = None

    def run(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            self.toast("OCR", "Проверка OCR уже выполняется.", "info")
            return

        self.toast(
            "OCR",
            "Проверяем модели и запуск PaddleOCR. Программой можно продолжать пользоваться.",
            "info",
        )
        thread = _OcrDiagnosticThread(self)
        thread.completed.connect(self._on_completed)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_thread)
        self._thread = thread
        thread.start()

    def _on_completed(self, ok: bool, details: str) -> None:
        parent = self.parent()
        if parent is None or getattr(parent, "_is_closing", False):
            return
        if ok:
            self.toast("OCR", "OCR работает: PaddleOCR и модели успешно инициализированы.", "success")
            QMessageBox.information(
                parent,
                "Проверка OCR",
                "OCR работает корректно.\n\n" + details,
            )
        else:
            self.toast("OCR", "Проверка OCR выявила ошибку.", "error")
            QMessageBox.critical(
                parent,
                "Ошибка OCR",
                "Не удалось инициализировать OCR.\n\n" + details,
            )

    def _clear_thread(self) -> None:
        self._thread = None
