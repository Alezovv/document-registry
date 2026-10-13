from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication

from app import __version__
from app.diagnostics_service import DiagnosticsService
from app.update_service import UpdateService
from app.ui.diagnostics_dialog import DiagnosticsDialog

logger = logging.getLogger(__name__)


class DiagnosticsController:
    """UI actions for diagnostics, kept out of the MainWindow application shell."""

    def __init__(
        self,
        service: DiagnosticsService,
        logs_dir: str | Path,
        toast: Callable[[str, str, str], None],
        settings_page_provider: Callable[[], object | None],
    ) -> None:
        self.service = service
        self.logs_dir = Path(logs_dir)
        self.toast = toast
        self.settings_page_provider = settings_page_provider

    def _set_message(self, message: str, ok: bool) -> None:
        page = self.settings_page_provider()
        if page is not None:
            page.set_diagnostics_message(message, ok)


    def open_center(self) -> None:
        page = self.settings_page_provider()
        dialog = DiagnosticsDialog(self.service, parent=page)
        dialog.check_requested.connect(self.check_state)
        dialog.check_requested.connect(dialog.refresh)
        dialog.package_requested.connect(self.create_package)
        dialog.open_logs_requested.connect(self.open_logs_folder)
        dialog.open_data_requested.connect(self.open_data_folder)
        dialog.copy_requested.connect(self.copy_info)
        dialog.install_ui_test_requested.connect(self.test_install_window)
        dialog.rebuild_search_requested.connect(self.rebuild_search_index)
        dialog.duplicate_scan_requested.connect(self.scan_duplicates)
        dialog.exec()

    def rebuild_search_index(self) -> None:
        try:
            result = self.service.rebuild_search_index()
            self._set_message(result.summary, result.ok)
            self.toast("Полнотекстовый поиск", result.summary, "success")
        except Exception as error:
            logger.exception("Не удалось перестроить поисковый индекс")
            self.toast("Полнотекстовый поиск", f"Ошибка: {error}", "warning")

    def scan_duplicates(self) -> None:
        try:
            result = self.service.scan_duplicates()
            self._set_message(result.summary, result.ok)
            self.toast("Дубликаты документов", result.summary, "success" if result.ok else "warning")
        except Exception as error:
            logger.exception("Не удалось проверить дубликаты")
            self.toast("Дубликаты документов", f"Ошибка: {error}", "warning")

    def check_state(self) -> None:
        try:
            result = self.service.check_state()
            self._set_message(result.summary, result.ok)
            self.toast(
                "Проверка состояния",
                result.summary,
                "success" if result.ok else "warning",
            )
        except Exception as error:
            logger.exception("Ошибка проверки состояния")
            message = f"Проверка не выполнена: {error}"
            self._set_message(message, False)
            self.toast("Диагностика", message, "warning")

    def create_package(self) -> None:
        try:
            result = self.service.create_package()
            self._set_message(result.summary, True)
            self.toast("Диагностический пакет", result.summary, "success")
        except Exception as error:
            logger.exception("Ошибка создания диагностического пакета")
            message = f"Не удалось создать пакет: {error}"
            self._set_message(message, False)
            self.toast("Диагностика", message, "warning")

    def open_data_folder(self) -> None:
        data_dir = Path(self.service.paths.data)
        data_dir.mkdir(parents=True, exist_ok=True)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(data_dir))):
            self.toast("Данные", f"Папка данных: {data_dir}", "warning")

    def open_logs_folder(self) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.logs_dir))):
            self.toast("Логи", f"Папка логов: {self.logs_dir}", "warning")


    def test_install_window(self) -> None:
        """Launch the standalone updater UI in a no-installation test mode."""
        try:
            UpdateService(self.service.paths).launch_updater_ui_test(__version__)
            self.toast(
                "Окно установки",
                "Запущен безопасный тест окна установки. Файлы программы не изменяются.",
                "info",
            )
        except Exception as error:
            logger.exception("Не удалось запустить тест окна установки")
            self.toast(
                "Окно установки",
                f"Не удалось запустить тест: {error}",
                "warning",
            )

    def copy_info(self) -> None:
        try:
            QApplication.clipboard().setText(self.service.copyable_summary())
            message = "Технические сведения скопированы в буфер обмена."
            self._set_message(message, True)
            self.toast("Диагностика", "Технические сведения скопированы.", "success")
        except Exception as error:
            logger.exception("Не удалось скопировать диагностические сведения")
            self.toast(
                "Диагностика",
                f"Не удалось скопировать сведения: {error}",
                "warning",
            )
