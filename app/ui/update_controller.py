from __future__ import annotations

import logging
from pathlib import Path
import time
from typing import Callable

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QProgressDialog

from app.db_migrations import DatabaseRollbackComparison
from app.text_encoding import repair_cyrillic_mojibake
from app.ui.dialogs.rollback_dialog import RollbackVersionDialog
from app.update_service import (
    SavedDatabaseBackup,
    UpdateDownloadCancelled,
    UpdateError,
    UpdateInfo,
    UpdateNetworkError,
    UpdateNotConfigured,
    UpdateNotReady,
    UpdateService,
)

logger = logging.getLogger(__name__)


class _CheckUpdateThread(QThread):
    checked = Signal(object)
    failed = Signal(str, bool)

    def __init__(
        self,
        service: UpdateService,
        current_version: str,
        source_dir: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.service = service
        self.current_version = current_version
        self.source_dir = source_dir

    def run(self):
        try:
            info = self.service.check_for_update(self.source_dir, self.current_version)
            self.checked.emit(info)
        except (UpdateNetworkError, UpdateNotReady, UpdateNotConfigured) as error:
            self.failed.emit(str(error), True)
        except UpdateError as error:
            self.failed.emit(str(error), False)
        except Exception as error:
            logger.exception("Неожиданная ошибка проверки обновлений")
            self.failed.emit(str(error) or "Не удалось проверить обновления.", False)


class _CheckRollbackVersionsThread(QThread):
    loaded = Signal(object, str)

    def __init__(
        self,
        service: UpdateService,
        current_version: str,
        source_dir: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.service = service
        self.current_version = current_version
        self.source_dir = source_dir

    def run(self):
        try:
            versions = self.service.list_rollback_versions(
                self.source_dir,
                self.current_version,
            )
            self.loaded.emit(versions, "")
        except Exception as error:
            logger.info("Не удалось загрузить список старых версий из канала обновлений: %s", error)
            self.loaded.emit([], str(error))


class _PrepareUpdateThread(QThread):
    progress = Signal(int, int)
    prepared = Signal(object)
    failed = Signal(str, bool)
    cancelled = Signal(str)

    def __init__(self, service: UpdateService, info: UpdateInfo, parent=None):
        super().__init__(parent)
        self.service = service
        self.info = info

    def run(self):
        try:
            package = self.service.prepare_update(
                self.info,
                progress=lambda done, total: self.progress.emit(done, total),
                should_cancel=self.isInterruptionRequested,
            )
            self.prepared.emit(package)
        except UpdateDownloadCancelled as error:
            self.cancelled.emit(str(error))
        except UpdateNetworkError as error:
            self.failed.emit(str(error), True)
        except Exception as error:
            logger.exception("Не удалось скачать/подготовить обновление")
            self.failed.emit(str(error), False)


class UpdateController(QObject):
    """Non-blocking forward update checks/downloads, version rollback, and hand-off to the updater.

    Main mode is forward updating (`check`). Rolling back to an older version
    (`open_rollback_dialog`) is an additional user-triggered workflow with
    version selection, version description display, database difference warning,
    and automatic backup of the current database before transitioning.
    """

    def __init__(
        self,
        service: UpdateService,
        current_version: str,
        toast: Callable[[str, str, str], None],
        parent=None,
        backup_service=None,
    ):
        super().__init__(parent)
        self.service = service
        self.current_version = current_version
        self.toast = toast
        self.backup_service = backup_service
        self._check_thread: _CheckUpdateThread | None = None
        self._check_manual = False
        self._rollback_check_thread: _CheckRollbackVersionsThread | None = None
        self._prepare_thread: _PrepareUpdateThread | None = None
        self._progress_dialog: QProgressDialog | None = None
        self._pending_info: UpdateInfo | None = None
        self._pending_rollback: bool = False
        self._pending_comparison: DatabaseRollbackComparison | None = None
        self._ready_package: Path | None = None
        self._notified_versions: set[str] = set()
        self._last_check_started_at = 0.0

    @property
    def last_check_started_at(self) -> float:
        return self._last_check_started_at

    # ------------------------------------------------------------------
    # Main mode: Forward update
    # ------------------------------------------------------------------
    def check(
        self,
        source_dir: str | None = None,
        *,
        manual: bool = False,
    ) -> None:
        if self._check_thread is not None and self._check_thread.isRunning():
            if manual:
                self.toast("Обновления", "Проверка обновлений уже выполняется.", "info")
            return

        self._check_manual = manual
        self._last_check_started_at = time.monotonic()
        thread = _CheckUpdateThread(
            self.service,
            self.current_version,
            source_dir=source_dir,
            parent=self,
        )
        thread.checked.connect(self._on_checked)
        thread.failed.connect(self._on_check_failed)
        thread.finished.connect(self._on_check_thread_finished)
        self._check_thread = thread
        thread.start()

    def _on_checked(self, info) -> None:
        manual = self._check_manual
        if info is None:
            if manual:
                self.toast(
                    "Обновления",
                    f"Установлена актуальная версия {self.current_version}.",
                    "success",
                )
            return
        assert isinstance(info, UpdateInfo)
        self._pending_info = info
        self._pending_rollback = False
        self._pending_comparison = None
        self._offer_update(info, manual=manual)

    def _on_check_failed(self, message: str, temporary: bool) -> None:
        if self._check_manual:
            if temporary:
                self.toast("Обновления", message, "warning")
            else:
                QMessageBox.warning(self.parent(), "Обновления", message)
        else:
            # Automatic network failures are intentionally silent; using the app
            # must never depend on Internet access.
            if temporary:
                logger.info("Автопроверка обновлений отложена: %s", message)
            else:
                logger.warning("Ошибка автопроверки обновлений: %s", message)

    def _on_check_thread_finished(self) -> None:
        if self._check_thread is not None:
            self._check_thread.deleteLater()
            self._check_thread = None

    def _offer_update(self, info: UpdateInfo, *, manual: bool) -> None:
        if self._prepare_thread is not None and self._prepare_thread.isRunning():
            return
        if not manual and info.version in self._notified_versions:
            return
        self._notified_versions.add(info.version)

        message = QMessageBox(self.parent())
        message.setIcon(QMessageBox.Icon.Information)
        message.setWindowTitle("Доступно обновление")
        message.setText(
            f"DocumentRegistry {self.current_version} → {info.version}"
        )
        details = repair_cyrillic_mojibake(info.notes) or "Исправления и улучшения приложения."
        size_mb = info.size_bytes / (1024 * 1024)
        message.setInformativeText(
            details
            + f"\n\nРазмер пакета: {size_mb:.1f} МБ."
            + "\nСкачивание идёт в фоне. Во время скачивания программой можно продолжать пользоваться."
        )
        update_button = message.addButton("Скачать обновление", QMessageBox.ButtonRole.AcceptRole)
        message.addButton("Позже", QMessageBox.ButtonRole.RejectRole)
        message.exec()
        if message.clickedButton() is update_button:
            self._begin_prepare(info, rollback=False)

    # ------------------------------------------------------------------
    # Additional mode: Version rollback with selection, description & DB warning
    # ------------------------------------------------------------------
    def open_rollback_dialog(self, source_dir: str | None = None) -> None:
        if self._prepare_thread is not None and self._prepare_thread.isRunning():
            self.toast(
                "Управление версиями",
                "Дождитесь завершения текущей загрузки пакета.",
                "info",
            )
            return
        if self._rollback_check_thread is not None and self._rollback_check_thread.isRunning():
            self.toast(
                "Управление версиями",
                "Список доступных старых версий уже загружается...",
                "info",
            )
            return

        thread = _CheckRollbackVersionsThread(
            self.service,
            self.current_version,
            source_dir=source_dir,
            parent=self,
        )
        thread.loaded.connect(self._on_rollback_versions_loaded)
        thread.finished.connect(self._on_rollback_check_thread_finished)
        self._rollback_check_thread = thread
        thread.start()

    def _on_rollback_check_thread_finished(self) -> None:
        if self._rollback_check_thread is not None:
            self._rollback_check_thread.deleteLater()
            self._rollback_check_thread = None

    def _on_rollback_versions_loaded(self, versions: list[UpdateInfo], error_text: str) -> None:
        dialog = RollbackVersionDialog(
            self.service,
            self.current_version,
            list(versions or []),
            initial_error=error_text,
            backup_service=self.backup_service,
            toast=self.toast,
            parent=self.parent(),
        )
        dialog.database_restored.connect(self._on_database_restored)
        dialog.database_merged.connect(self._on_database_merged)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        info = dialog.selected_update_info()
        if info is None:
            return
        self._confirm_and_start_rollback(info, dialog.current_comparison())

    def _on_database_restored(self) -> None:
        parent = self.parent()
        if parent is not None:
            if hasattr(parent, "_after_backup_restore"):
                parent._after_backup_restore()
            elif hasattr(parent, "load_requests"):
                parent.load_requests()
        if callable(self.toast):
            self.toast("База данных восстановлена", "База данных успешно восстановлена из сохранённого снимка.", "success")

    def _on_database_merged(self) -> None:
        parent = self.parent()
        if parent is not None:
            if hasattr(parent, "_after_backup_restore"):
                parent._after_backup_restore()
            elif hasattr(parent, "load_requests"):
                parent.load_requests()
        if callable(self.toast):
            self.toast("Слияние завершено", "Записи успешно объединены с текущей базой данных без потери данных.", "success")

    def _confirm_and_start_rollback(
        self,
        info: UpdateInfo,
        comparison: DatabaseRollbackComparison | None = None,
    ) -> None:
        if comparison is None:
            try:
                comparison = self.service.check_database_compatibility(info, self.current_version)
            except Exception as error:
                QMessageBox.critical(
                    self.parent(),
                    "Ошибка проверки базы данных",
                    f"Не удалось проверить совместимость базы данных перед откатом:\n{error}",
                )
                return

        if comparison.has_differences:
            warning_box = QMessageBox(self.parent())
            warning_box.setIcon(QMessageBox.Icon.Warning)
            warning_box.setWindowTitle("Предупреждение: различия в базе данных")
            warning_box.setText(
                f"Обнаружены различия в базе данных при переходе {self.current_version} → {info.version}"
            )
            warning_box.setInformativeText(
                comparison.format_warning_text(
                    current_version=self.current_version,
                    target_version=info.version,
                )
            )
            proceed_button = warning_box.addButton(
                "Сохранить текущую БД и продолжить откат",
                QMessageBox.ButtonRole.AcceptRole,
            )
            warning_box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
            warning_box.exec()
            if warning_box.clickedButton() is not proceed_button:
                return

        self._pending_comparison = comparison
        self._begin_prepare(info, rollback=True)

    # ------------------------------------------------------------------
    # Shared background package preparation & installation hand-off
    # ------------------------------------------------------------------
    def _begin_prepare(self, info: UpdateInfo, *, rollback: bool = False) -> None:
        if self._prepare_thread is not None and self._prepare_thread.isRunning():
            return
        self._pending_info = info
        self._pending_rollback = bool(rollback)
        self._ready_package = None

        action_label = "Подготовка пакета отката" if rollback else "Скачивание DocumentRegistry"
        window_title = (
            f"Откат DocumentRegistry до {info.version}"
            if rollback
            else f"Обновление DocumentRegistry до {info.version}"
        )

        dialog = QProgressDialog(
            f"{action_label} {info.version}...",
            "Приостановить",
            0,
            100,
            self.parent(),
        )
        dialog.setWindowTitle(window_title)
        # QProgressDialog defaults to Qt.Dialog.  On Windows a dialog may ignore
        # WindowMinimizeButtonHint and show only the close button.  Make the
        # download progress a normal top-level window so the native minimize
        # button is always available while the background download continues.
        dialog.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setValue(0)
        dialog.canceled.connect(self._cancel_download)
        dialog.show()
        self._progress_dialog = dialog

        thread = _PrepareUpdateThread(self.service, info, self)
        thread.progress.connect(self._on_progress)
        thread.prepared.connect(self._on_prepared)
        thread.failed.connect(self._on_prepare_failed)
        thread.cancelled.connect(self._on_cancelled)
        thread.finished.connect(self._on_prepare_thread_finished)
        self._prepare_thread = thread
        thread.start()

    def _cancel_download(self) -> None:
        if self._prepare_thread is None or not self._prepare_thread.isRunning():
            return
        self._prepare_thread.requestInterruption()
        if self._progress_dialog is not None:
            self._progress_dialog.setLabelText(
                "Приостанавливаем скачивание... Уже загруженная часть будет сохранена."
            )

    def _on_progress(self, done: int, total: int) -> None:
        if self._progress_dialog is None:
            return
        percent = max(0, min(100, int(done * 100 / max(1, total))))
        self._progress_dialog.setValue(percent)
        done_mb = done / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        target_version = self._pending_info.version if self._pending_info is not None else ""
        version_text = f" {target_version}" if target_version else ""
        prefix = "Подготовка отката к DocumentRegistry" if self._pending_rollback else "Скачивание DocumentRegistry"
        self._progress_dialog.setLabelText(
            f"{prefix}{version_text}... {percent}% · "
            f"{done_mb:.1f} из {total_mb:.1f} МБ\n"
            "DocumentRegistry можно продолжать использовать."
        )

    def _on_prepared(self, package) -> None:
        info = self._pending_info
        if info is None:
            self._on_prepare_failed(
                "Не удалось определить версию подготовленного пакета.", False
            )
            return
        self._ready_package = Path(package)
        self._close_progress_dialog()

        message = QMessageBox(self.parent())
        message.setIcon(QMessageBox.Icon.Information)
        if self._pending_rollback:
            message.setWindowTitle("Пакет для отката готов")
            message.setText(f"Пакет старой версии DocumentRegistry {info.version} проверен и готов.")
            extra_db_note = (
                f"Текущая база данных версии {self.current_version} будет автоматически сохранена "
                "в папку резервных копий (backups/rollback/) перед выполнением отката.\n\n"
            )
            message.setInformativeText(
                f"Текущая версия {self.current_version} будет заменена на версию {info.version}.\n\n"
                + extra_db_note
                + "Для установки программа будет закрыта на короткое время."
            )
            install_button = message.addButton("Выполнить откат сейчас", QMessageBox.ButtonRole.AcceptRole)
            message.addButton("Позже", QMessageBox.ButtonRole.RejectRole)
        else:
            message.setWindowTitle("Обновление готово")
            message.setText(f"DocumentRegistry {info.version} скачан и проверен.")
            message.setInformativeText(
                f"Текущая версия {self.current_version} будет обновлена до {info.version}.\n\n"
                "Для установки программа будет закрыта на короткое время. "
                "Можно продолжить работу и установить обновление позже."
            )
            install_button = message.addButton("Установить сейчас", QMessageBox.ButtonRole.AcceptRole)
            message.addButton("Установить позже", QMessageBox.ButtonRole.RejectRole)

        message.exec()
        if message.clickedButton() is install_button:
            self._install_ready_update()
        else:
            title = "Пакет отката готов" if self._pending_rollback else "Обновление готово"
            self.toast(
                title,
                f"Версия {info.version} уже подготовлена в кэше.",
                "success",
            )

    def _save_pre_rollback_database(self, info: UpdateInfo) -> SavedDatabaseBackup:
        comparison = self._pending_comparison
        if comparison is None:
            comparison = self.service.check_database_compatibility(info, self.current_version)
        saved = self.service.save_database_before_rollback(
            current_version=self.current_version,
            target_version=info.version,
            target_schema=info.database_schema,
            comparison=comparison,
        )
        if self.backup_service is not None:
            try:
                zip_backup_path = saved.db_backup_path.with_name(
                    saved.db_backup_path.stem.replace("requests_before_rollback_", "full_backup_before_rollback_")
                    + ".zip"
                )
                self.backup_service.create_backup(zip_backup_path)
            except Exception:
                logger.warning("Не удалось создать дополнительный ZIP-архив при откате", exc_info=True)
        return saved

    def _install_ready_update(self) -> None:
        info = self._pending_info
        package = self._ready_package
        is_rollback = self._pending_rollback
        if info is None or package is None:
            QMessageBox.warning(
                self.parent(),
                "Управление версиями",
                "Подготовленный пакет не найден.",
            )
            return

        saved_backup: SavedDatabaseBackup | None = None
        if is_rollback:
            try:
                saved_backup = self._save_pre_rollback_database(info)
            except Exception as error:
                logger.exception("Не удалось сохранить текущую БД перед откатом")
                QMessageBox.critical(
                    self.parent(),
                    "Откат отменён",
                    f"Не удалось сохранить текущую базу данных перед переходом на старую версию:\n{error}",
                )
                return

        try:
            process = self.service.launch_updater(
                package,
                info,
                rollback=is_rollback,
                current_version=self.current_version,
                pre_saved_backup=saved_backup,
            )
            # Do not close the main application until the standalone updater has
            # positively confirmed that a visible install-status window (or its
            # console fallback) exists.  This prevents invisible installations.
            self.service.wait_for_updater_window(
                process,
                info.version,
                event_pump=QApplication.processEvents,
            )
        except Exception as error:
            logger.exception("Не удалось запустить установку версии")
            extra_saved = (
                f"\n\nКопия текущей базы данных сохранена:\n{saved_backup.db_backup_path}"
                if saved_backup is not None
                else ""
            )
            QMessageBox.critical(
                self.parent(),
                "Откат не выполнен" if is_rollback else "Обновление не установлено",
                str(error) + extra_saved,
            )
            return
        QApplication.instance().quit()

    def _on_prepare_failed(self, message: str, temporary: bool) -> None:
        self._close_progress_dialog()
        if temporary:
            QMessageBox.information(
                self.parent(),
                "Скачивание приостановлено",
                (message or "Не удалось продолжить скачивание.")
                + "\n\nПрограммой можно продолжать пользоваться. "
                "При следующей попытке загрузка продолжится с сохранённой части.",
            )
        else:
            QMessageBox.critical(
                self.parent(),
                "Пакет не подготовлен",
                message or "Не удалось подготовить пакет версии.",
            )

    def _on_cancelled(self, message: str) -> None:
        self._close_progress_dialog()
        self.toast(
            "Управление версиями",
            message or "Скачивание приостановлено. Программа продолжает работать.",
            "info",
        )

    def _close_progress_dialog(self) -> None:
        if self._progress_dialog is not None:
            try:
                self._progress_dialog.canceled.disconnect(self._cancel_download)
            except RuntimeError:
                pass
            self._progress_dialog.close()
            self._progress_dialog.deleteLater()
            self._progress_dialog = None

    def _on_prepare_thread_finished(self) -> None:
        if self._prepare_thread is not None:
            self._prepare_thread.deleteLater()
            self._prepare_thread = None
