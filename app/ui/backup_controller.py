from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox

from app.backup_service import BackupError, BackupService
from app.background_task_coordinator import COORDINATOR
from app.resource_profile import resource_profile_from_environment


logger = logging.getLogger(__name__)


class BackupWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, service: BackupService, operation: str, path: str):
        super().__init__()
        self.service = service
        self.operation = operation
        self.path = path

    def run(self):
        try:
            self.setPriority(QThread.Priority.LowPriority)
        except Exception:
            pass
        task_started = False
        try:
            profile = resource_profile_from_environment()
            if profile.economy_memory_policy:
                task_started = COORDINATOR.wait_start("backup", timeout=30.0)
                if not task_started:
                    raise BackupError("Экономный режим: дождитесь завершения OCR или другой тяжёлой операции и повторите backup.")
            if self.operation in {"create", "auto"}:
                result = self.service.create_backup(self.path)
            elif self.operation == "restore":
                self.service.restore_backup(self.path)
                result = Path(self.path)
            else:
                raise BackupError("Неизвестная операция резервного копирования.")
            self.succeeded.emit(result)
        except Exception as error:
            logger.exception("Ошибка операции backup: %s", self.operation)
            self.failed.emit(str(error))
        finally:
            if task_started:
                COORDINATOR.finish("backup")


class BackupController(QObject):
    toast_requested = Signal(str, str, str)
    restored = Signal()
    busy_changed = Signal(bool)

    def __init__(self, service: BackupService, parent=None):
        super().__init__(parent)
        self.service = service
        self.parent_widget = parent
        self.worker: BackupWorker | None = None

    def create_backup(self):
        if self._is_busy():
            return
        default_name = datetime.now().strftime("registry_backup_%Y-%m-%d_%H-%M.zip")
        path, _ = QFileDialog.getSaveFileName(
            self.parent_widget,
            "Создать резервную копию",
            default_name,
            "Резервная копия (*.zip)",
        )
        if path:
            self._start("create", path)

    def maybe_create_auto_backup(self, path: str | Path, interval_days: int = 7) -> bool:
        """Refresh a single rolling backup when it is older than the interval.

        The existing backup is replaced only after the new archive is fully
        created, because BackupService writes via a temporary file + replace.
        """
        if self._is_busy() or not self.service.is_backup_due(path, interval_days):
            return False
        if resource_profile_from_environment().economy_memory_policy and COORDINATOR.is_busy():
            return False
        self._start("auto", str(path))
        return True

    def restore_backup(self):
        if self._is_busy():
            return
        path, _ = QFileDialog.getOpenFileName(
            self.parent_widget,
            "Восстановить резервную копию",
            "",
            "Резервная копия (*.zip)",
        )
        if not path:
            return
        try:
            manifest = self.service.inspect_backup(path)
        except BackupError as error:
            QMessageBox.critical(self.parent_widget, "Некорректная копия", str(error))
            return

        created_at = manifest.get("created_at", "неизвестно")
        documents = manifest.get("document_count", 0)
        answer = QMessageBox.warning(
            self.parent_widget,
            "Восстановление данных",
            "Текущая база данных и документы будут заменены данными из резервной копии.\n\n"
            f"Дата копии: {created_at}\nДокументов: {documents}\n\n"
            "Перед восстановлением рекомендуется создать свежую резервную копию. Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start("restore", path)

    def _is_busy(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def _start(self, operation: str, path: str):
        self.worker = BackupWorker(self.service, operation, path)
        self.worker.succeeded.connect(lambda result: self._success(operation, result))
        self.worker.failed.connect(self._failure)
        self.worker.finished.connect(self._finished)
        self.busy_changed.emit(True)
        if operation == "restore" and self.parent_widget is not None:
            self.parent_widget.setEnabled(False)
        self.worker.start(QThread.Priority.LowPriority)

    def stop(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.wait(2000)

    def _success(self, operation: str, result):
        if self.parent_widget is not None and getattr(self.parent_widget, "_is_closing", False):
            return
        if operation == "create":
            self.toast_requested.emit(
                "Резервная копия создана",
                f"Сохранено и проверено: {Path(result.path).name}",
                "success",
            )
        elif operation == "auto":
            self.toast_requested.emit(
                "Автобэкап обновлён",
                "Еженедельная резервная копия успешно заменена и проверена.",
                "success",
            )
        else:
            self.restored.emit()
            self.toast_requested.emit(
                "Данные восстановлены",
                "База и документы восстановлены из резервной копии.",
                "success",
            )

    def _failure(self, message: str):
        if self.parent_widget is not None and getattr(self.parent_widget, "_is_closing", False):
            return
        QMessageBox.critical(self.parent_widget, "Ошибка резервной копии", message)

    def _finished(self):
        if self.parent_widget is not None:
            self.parent_widget.setEnabled(True)
        self.busy_changed.emit(False)
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.deleteLater()
