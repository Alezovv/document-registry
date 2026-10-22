from __future__ import annotations

from pathlib import Path
import logging

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox, QProgressDialog

from app.legacy_import_service import LegacyImportError, LegacyImportService
from app.background_task_coordinator import COORDINATOR
from app.resource_profile import resource_profile_from_environment

logger = logging.getLogger(__name__)


class LegacyImportWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, service: LegacyImportService, source_db: str, documents_dir: str | None):
        super().__init__()
        self.service = service
        self.source_db = source_db
        self.documents_dir = documents_dir

    def run(self):
        task_started = False
        try:
            if resource_profile_from_environment().economy_memory_policy:
                task_started = COORDINATOR.wait_start("legacy_import", timeout=30.0)
                if not task_started:
                    raise LegacyImportError("Экономный режим: дождитесь завершения OCR/backup и повторите импорт старой базы.")
            result = self.service.migrate(
                self.source_db,
                self.documents_dir,
                should_cancel=self.isInterruptionRequested,
            )
            self.succeeded.emit(result)
        except Exception as error:
            logger.exception("Ошибка переноса старой базы")
            self.failed.emit(str(error))
        finally:
            if task_started:
                COORDINATOR.finish("legacy_import")


class LegacyImportController:
    """Qt orchestration for the one-shot legacy migration.

    The worker and progress dialog are created only after the user confirms an
    import, so this controller has no background resource use while idle.
    """

    def __init__(self, service: LegacyImportService, parent, on_success):
        self.service = service
        self.parent = parent
        self.on_success = on_success
        self.worker: LegacyImportWorker | None = None
        self.progress: QProgressDialog | None = None

    def start(self):
        if self.worker is not None and self.worker.isRunning():
            return

        source, _ = QFileDialog.getOpenFileName(
            self.parent,
            "Выберите старую базу данных",
            "",
            "SQLite database (*.db *.sqlite *.sqlite3);;Все файлы (*)",
        )
        if not source:
            return

        try:
            inspection = self.service.inspect(source)
        except LegacyImportError as error:
            QMessageBox.critical(self.parent, "Старая база не поддерживается", str(error))
            return

        documents_dir: str | None = None
        if inspection.document_references and inspection.documents_missing:
            suggested = (
                str(inspection.suggested_documents_dir)
                if inspection.suggested_documents_dir
                else str(Path(source).parent)
            )
            answer = QMessageBox.question(
                self.parent,
                "Документы старой версии",
                f"В базе найдено ссылок на PDF: {inspection.document_references}.\n"
                f"Автоматически найдено файлов: {inspection.documents_found}.\n"
                f"Не найдено: {inspection.documents_missing}.\n\n"
                "Хотите указать папку со старыми PDF?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return
            if answer == QMessageBox.StandardButton.Yes:
                selected = QFileDialog.getExistingDirectory(
                    self.parent,
                    "Папка со старыми PDF",
                    suggested,
                )
                if not selected:
                    return
                documents_dir = selected
                try:
                    inspection = self.service.inspect(source, documents_dir)
                except LegacyImportError as error:
                    QMessageBox.critical(self.parent, "Ошибка проверки", str(error))
                    return

        details = (
            f"Записей: {inspection.record_count}\n"
            f"Ссылок на PDF: {inspection.document_references}\n"
            f"PDF найдено: {inspection.documents_found}\n"
            f"PDF не найдено: {inspection.documents_missing}\n\n"
            "Текущий реестр будет автоматически сохранён в отдельную резервную копию, "
            "после чего заменён перенесёнными данными.\n\nПродолжить?"
        )
        answer = QMessageBox.warning(
            self.parent,
            "Импорт старой базы",
            details,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.progress = QProgressDialog(
            "Перенос старой базы и документов…",
            "",
            0,
            0,
            self.parent,
        )
        self.progress.setWindowTitle("Импорт старой базы")
        self.progress.setCancelButton(None)
        self.progress.setMinimumDuration(0)
        self.progress.setAutoClose(False)
        self.progress.setAutoReset(False)
        self.progress.show()

        self.worker = LegacyImportWorker(self.service, source, documents_dir)
        self.worker.succeeded.connect(self._success)
        self.worker.failed.connect(self._failure)
        self.worker.finished.connect(self._finished)
        self.worker.start(QThread.Priority.LowPriority)

    def stop(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.wait(2000)
        if self.progress is not None:
            self.progress.close()
            self.progress = None

    def _success(self, result):
        if self.parent is not None and getattr(self.parent, "_is_closing", False):
            return
        if callable(self.on_success):
            self.on_success()
        message = (
            f"Перенесено записей: {result.imported_records}\n"
            f"Скопировано PDF: {result.copied_documents}\n"
            f"Не найдено PDF: {result.missing_documents}\n\n"
            f"Резервная копия прежних данных:\n{result.backup_path}"
        )
        QMessageBox.information(self.parent, "Перенос завершён", message)

    def _failure(self, message: str):
        if self.parent is not None and getattr(self.parent, "_is_closing", False):
            return
        QMessageBox.critical(self.parent, "Ошибка переноса", message)

    def _finished(self):
        if self.progress is not None:
            self.progress.close()
            self.progress.deleteLater()
            self.progress = None
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.deleteLater()
