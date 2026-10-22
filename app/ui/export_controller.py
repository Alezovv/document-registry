from __future__ import annotations

from dataclasses import replace
from datetime import date
import logging

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox

from app.export_service import ExportError, ExportService


logger = logging.getLogger(__name__)


class ExportWorker(QThread):
    """Generate an XLSX away from the Qt GUI thread."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, export_service: ExportService, requests, destination: str, parent=None):
        super().__init__(parent)
        self.export_service = export_service
        self.requests = requests
        self.destination = destination

    def run(self):
        try:
            self.setPriority(QThread.Priority.LowPriority)
        except Exception:
            pass
        try:
            # The controller must provide a materialized snapshot. Calling an
            # arbitrary data-source function here could touch GUI objects or a
            # SQLite connection created in the main thread.
            if callable(self.requests):
                raise ExportError("Перед запуском экспорта необходимо подготовить снимок записей в GUI-потоке.")
            items = self.requests
            if not items:
                raise ExportError("В текущей выборке нет записей для экспорта.")
            result = self.export_service.export_xlsx(items, self.destination)
        except Exception as error:
            logger.exception("Ошибка фонового экспорта реестра в Excel")
            self.failed.emit(str(error))
        else:
            self.succeeded.emit(result)


class ExportController(QObject):
    toast_requested = Signal(str, str, str)

    def __init__(self, export_service: ExportService | None = None, parent=None):
        super().__init__(parent)
        self.export_service = export_service or ExportService()
        self.parent_widget = parent
        self.worker: ExportWorker | None = None

    def export_requests(self, requests):
        if self.worker is not None and self.worker.isRunning():
            self.toast_requested.emit(
                "Экспорт уже выполняется",
                "Дождитесь завершения текущего экспорта.",
                "info",
            )
            return

        is_callable = callable(requests)
        if not is_callable:
            requests = list(requests or [])
            if not requests:
                self.toast_requested.emit(
                    "Экспорт Excel",
                    "В текущей выборке нет записей для экспорта.",
                    "info",
                )
                return

        default_name = f"registry_{date.today().isoformat()}.xlsx"
        path, _ = QFileDialog.getSaveFileName(
            self.parent_widget,
            "Экспорт реестра в Excel",
            default_name,
            "Excel (*.xlsx)",
        )
        if not path:
            return

        # Resolve callable sources on the GUI thread, before QThread starts, so
        # SQLite connections and any UI-backed query function stay thread-affine.
        if is_callable:
            try:
                requests = list(requests())
            except Exception as error:
                logger.exception("Не удалось получить данные для экспорта")
                QMessageBox.critical(
                    self.parent_widget,
                    "Ошибка экспорта",
                    f"Не удалось подготовить данные для экспорта: {error}",
                )
                return
            if not requests:
                self.toast_requested.emit(
                    "Экспорт Excel",
                    "В текущей выборке нет записей для экспорта.",
                    "info",
                )
                return

        try:
            snapshot = [replace(request) for request in requests]
        except (TypeError, ValueError):
            # Some callers may export mapping/tuple-like rows rather than Request
            # dataclasses. Still freeze the iterable in the calling thread.
            snapshot = list(requests)

        self.worker = ExportWorker(self.export_service, snapshot, path, parent=self)

        self.worker.succeeded.connect(self._on_export_succeeded)
        self.worker.failed.connect(self._on_export_failed)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start(QThread.Priority.LowPriority)
        self.toast_requested.emit(
            "Экспорт Excel",
            "Файл формируется в фоновом режиме. Можно продолжать работу.",
            "info",
        )

    def _on_export_succeeded(self, result):
        self.toast_requested.emit(
            "Экспорт завершён",
            f"Сохранено записей: {result.row_count}",
            "success",
        )

    def _on_export_failed(self, message: str):
        QMessageBox.critical(self.parent_widget, "Ошибка экспорта", message)

    def _on_worker_finished(self):
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.deleteLater()
