from pathlib import Path
import logging
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QMainWindow, QMessageBox, QStackedWidget, QWidget
from app.app_paths import get_app_paths
from app.backup_service import BackupService
from app.database import Database
from app.data_health_monitor import DataHealthMonitor
from app.export_service import ExportService
from app.document_service import DocumentService
from app.diagnostics_service import DiagnosticsService
from app.legacy_import_service import LegacyImportService
from app.request_service import RequestService
from app.performance_mode_service import apply_performance_mode
from app.portable_paths import absolute_without_resolving
from app.performance_diagnostics import PerformanceDiagnostics
from app.settings_service import AppSettings, SettingsService
from app.ui.search_index_controller import SearchIndexController
from app.ui.backup_controller import BackupController
from app.ui.document_controller import DocumentController
from app.ui.diagnostics_controller import DiagnosticsController
from app.ui.export_controller import ExportController
from app.ui.documents_page import DocumentsPage
from app.ui.history_page import HistoryPage
from app.ui.shahmatka_page import ShahmatkaPage

from app.ui.import_controller import ImportController
from app.ui.legacy_import_controller import LegacyImportController
from app.ui.navigation_sidebar import NavigationSidebar
from app.ui.registry_page import RegistryPage
from app.ui.request_controller import RequestController
from app.ui.settings_page import SettingsPage
from app.ui.styles import APP_STYLE
from app.ui.toast import ToastNotification
from app.ui.update_window_mixin import UpdateWindowMixin
logger = logging.getLogger(__name__)
class MainWindow(UpdateWindowMixin, QMainWindow):
    """Application shell.
    Pages own presentation state; controllers own user actions; services own
    business/filesystem/database behavior. MainWindow only wires them together.
    """
    def __init__(self):
        super().__init__()
        self.paths = get_app_paths()
        self.database = Database()
        self.base_dir = self.paths.runtime
        self.data_dir = self.paths.data
        self.auto_backup_path = self.paths.backups / "auto_registry_backup.zip"
        self.auto_backup_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_service = SettingsService(
            self.paths.settings,
            self.paths.documents,
            runtime_root=self.paths.runtime,
        )
        self.settings = self.settings_service.load()
        self.resource_profile = apply_performance_mode(self.settings.performance_mode).profile
        self.confirm_delete_enabled = self.settings.confirm_delete
        self.documents_dir = absolute_without_resolving(self.settings.documents_dir)
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        self.document_service = DocumentService(self.database, self.documents_dir)
        repaired_paths = self.document_service.repair_stored_document_paths(preserve_runtime_spelling=True)
        if repaired_paths:
            logger.info("После переноса приложения восстановлено ссылок на PDF: %s", repaired_paths)
        self.request_service = RequestService(self.database, self.document_service)
        self.backup_service = BackupService(self.database, self.document_service)
        self.legacy_import_service = LegacyImportService(
            self.database, self.document_service, self.backup_service
        )
        self.export_service = ExportService()
        self.performance_diagnostics = PerformanceDiagnostics(self.resource_profile)
        self.diagnostics_service = DiagnosticsService(
            self.database,
            self.document_service,
            self.performance_diagnostics,
            paths=self.paths,
        )
        self.diagnostics_controller = DiagnosticsController(
            self.diagnostics_service,
            self.paths.logs,
            self.show_toast,
            lambda: self.settings_page,
        )
        self.current_page = "registry"
        self.page_widgets: dict[str, QWidget] = {}
        self.registry_page: RegistryPage | None = None
        self.documents_page: DocumentsPage | None = None
        self.history_page: HistoryPage | None = None
        self.settings_page: SettingsPage | None = None
        self._create_controllers()
        self.data_health_monitor = DataHealthMonitor(self.database, self.backup_service, self)
        self.data_health_monitor.warning.connect(self.show_toast)
        self._setup_update_system()
        self._build_ui()
        self.setWindowTitle("Реестр запросов")
        self.setMinimumSize(1100, 650)
        self.setStyleSheet(APP_STYLE)
        self.show_page(self.current_page)
        QTimer.singleShot(900, self._show_deadline_summary)
        QTimer.singleShot(2500, self._maybe_auto_backup)
        self.search_index_controller = SearchIndexController(self.request_service, self.paths.data / "parser_cache", self)
    def _create_controllers(self):
        documents_dir = lambda: self.documents_dir
        self.request_controller = RequestController(
            self.request_service,
            documents_dir,
            parent=self,
        )
        self.request_controller.changed.connect(self.load_requests)
        self.request_controller.toast_requested.connect(self.show_toast)
        self.document_controller = DocumentController(
            self.document_service,
            self.request_service,
            parent=self,
        )
        self.document_controller.changed.connect(self._refresh_after_document_change)
        self.document_controller.toast_requested.connect(self.show_toast)
        self.import_controller = ImportController(
            self.request_service,
            self.document_service,
            documents_dir,
            parent=self,
        )
        self.import_controller.finished.connect(lambda: self.show_page("registry"))
        self.import_controller.toast_requested.connect(self.show_toast)
        self.export_controller = ExportController(self.export_service, parent=self)
        self.export_controller.toast_requested.connect(self.show_toast)

        self.backup_controller = BackupController(self.backup_service, parent=self)
        self.backup_controller.toast_requested.connect(self.show_toast)
        self.backup_controller.restored.connect(self._after_backup_restore)
        self.backup_controller.busy_changed.connect(self._backup_busy_changed)

        self.legacy_import_controller = LegacyImportController(
            self.legacy_import_service,
            parent=self,
            on_success=self._after_legacy_import,
        )
    def _build_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = NavigationSidebar(self)
        self.sidebar.page_requested.connect(self.show_page)
        layout.addWidget(self.sidebar)

        self.page_stack = QStackedWidget(self)
        self.page_stack.setContentsMargins(28, 24, 28, 24)
        layout.addWidget(self.page_stack, 1)

    def show_toast(self, title: str, message: str, kind: str = "success"):
        ToastNotification(self, title, message, kind).show_toast()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_toast_notifications"):
            ToastNotification._reposition(self)

    def show_page(self, page: str):
        self.current_page = page
        self.sidebar.set_active(page)
        self._persist_last_page(page)

        created = page not in self.page_widgets
        if created:
            page_widget = self._create_page(page)
            self.page_widgets[page] = page_widget
            self.page_stack.addWidget(page_widget)

        self.page_stack.setCurrentWidget(self.page_widgets[page])
        if not created:
            self._refresh_page(page)

    def _persist_last_page(self, page: str):
        if self.settings.last_page == page:
            return
        self.settings.last_page = page
        try:
            self.settings_service.save(self.settings)
        except OSError:
            logger.exception("Не удалось сохранить последнюю открытую страницу")

    def _create_page(self, page: str) -> QWidget:
        if page == "registry":
            widget = RegistryPage(
                self.request_service,
                parent=self,
            )
            widget.create_requested.connect(self.request_controller.add_request)
            widget.edit_requested.connect(self._edit_selected_request)
            widget.export_requested.connect(self.export_controller.export_requests)
            widget.bulk_action_requested.connect(self._handle_bulk_action)
            widget.document_requested.connect(self.document_controller.open_document)
            self.registry_page = widget
            return widget

        if page == "documents":
            widget = DocumentsPage(self.document_service, parent=self)
            widget.add_requested.connect(self.import_controller.open)
            widget.open_requested.connect(self.document_controller.open_document)
            widget.delete_requested.connect(self.document_controller.delete_document)
            self.documents_page = widget
            return widget

        if page == "history":
            widget = HistoryPage(self.request_service, parent=self)
            widget.request_restored.connect(self.load_requests)
            widget.toast_requested.connect(self.show_toast)
            self.history_page = widget
            return widget

        if page == "settings":
            widget = SettingsPage(
                self.documents_dir,
                self.confirm_delete_enabled,
                request_service=self.request_service,
                performance_summary=self.resource_profile.summary,
                performance_diagnostics=self.performance_diagnostics,
                performance_mode=self.settings.performance_mode,
                auto_backup_path=self.auto_backup_path,
                update_source_dir=self.settings.update_source_dir,
                parent=self,
            )
            widget.save_requested.connect(self.save_settings)
            widget.backup_requested.connect(self.backup_controller.create_backup)
            widget.restore_requested.connect(self.backup_controller.restore_backup)
            widget.legacy_import_requested.connect(self.legacy_import_controller.start)
            widget.diagnostics_center_requested.connect(self.diagnostics_controller.open_center)
            widget.state_check_requested.connect(self.diagnostics_controller.check_state)
            widget.diagnostic_package_requested.connect(self.diagnostics_controller.create_package)
            widget.open_logs_requested.connect(self.diagnostics_controller.open_logs_folder)
            widget.copy_info_requested.connect(self.diagnostics_controller.copy_info)
            self._bind_update_settings_page(widget)
            self.settings_page = widget
            return widget

        
        if page == "shahmatka":
            widget = ShahmatkaPage(parent=self)
            self.shahmatka_page = widget
            return widget

        raise ValueError(f"Неизвестная страница: {page}")

    def _refresh_page(self, page: str):
        if page == "registry" and self.registry_page is not None:
            self.registry_page.refresh()
        elif page == "documents" and self.documents_page is not None:
            self.documents_page.refresh()
        elif page == "history" and self.history_page is not None:
            self.history_page.load_history()
        elif page == "settings" and self.settings_page is not None:
            self.settings_page.set_values(
                self.documents_dir,
                self.confirm_delete_enabled,
                self.settings.performance_mode,
                self.settings.update_source_dir,
            )

    def _edit_selected_request(self):
        if self.registry_page is None:
            return
        self.request_controller.edit_request(self.registry_page.get_selected_request())

    def _handle_bulk_action(self, action: str, requests):
        if action == "export":
            self.export_controller.export_requests(list(requests))
            return
        self.request_controller.apply_bulk_action(
            action,
            requests,
            confirm_delete=self.confirm_delete_enabled,
        )

    def _maybe_auto_backup(self):
        try:
            started = self.backup_controller.maybe_create_auto_backup(
                self.auto_backup_path,
                interval_days=7,
            )
            if started:
                logger.info("Запущен еженедельный автобэкап: %s", self.auto_backup_path)
        except Exception:
            logger.exception("Не удалось запустить автоматический backup")

    def _refresh_after_document_change(self):
        self.load_requests()
        if self.documents_page is not None:
            self.documents_page.refresh()

    def load_requests(self):
        if self.registry_page is not None:
            # RegistryPage owns paged/lazy loading; do not materialize the entire
            # requests table in MainWindow after every edit/import.
            self.registry_page.refresh()
        if self.history_page is not None and self.current_page == "history":
            self.history_page.load_history()

    def _show_deadline_summary(self):
        try:
            overdue, due_today = self.request_service.get_deadline_summary()
        except Exception:
            logger.exception("Не удалось получить сводку по срокам")
            return
        if overdue:
            message = f"Просрочено запросов: {overdue}"
            if due_today:
                message += f" · Срок сегодня: {due_today}"
            self.show_toast("Контроль сроков", message, "warning")
        elif due_today:
            self.show_toast(
                "Контроль сроков",
                f"Сегодня истекает срок у запросов: {due_today}",
                "warning",
            )

    def _backup_busy_changed(self, busy: bool):
        if not busy and self.settings_page is not None:
            self.settings_page.refresh_storage_info()

    def _after_backup_restore(self):
        self.document_service.clear_caches()
        self.document_service.repair_stored_document_paths()
        self.load_requests()
        if self.documents_page is not None:
            self.documents_page.refresh()
        if self.history_page is not None:
            self.history_page.load_history()
        if self.settings_page is not None:
            self.settings_page.refresh_storage_info()

    def _after_legacy_import(self):
        self.document_service.clear_caches()
        self.document_service.repair_stored_document_paths()
        self.load_requests()
        if self.documents_page is not None:
            self.documents_page.refresh()
        if self.history_page is not None:
            self.history_page.load_history()
        if self.settings_page is not None:
            self.settings_page.refresh_storage_info()
        self.show_page("registry")

    def save_settings(
        self,
        folder: str,
        confirm_delete: bool,
        performance_mode: str,
        update_source_dir: str = "",
    ):
        folder = (folder or "").strip()
        if not folder:
            QMessageBox.warning(self, "Ошибка", "Укажите папку документов.")
            return

        try:
            documents_dir = absolute_without_resolving(folder)
            documents_dir.mkdir(parents=True, exist_ok=True)
            previous_mode = self.settings.performance_mode
            settings = AppSettings(
                documents_dir=str(documents_dir),
                confirm_delete=bool(confirm_delete),
                last_page=self.current_page,
                performance_mode=performance_mode,
                update_source_dir=str(update_source_dir or "").strip(),
            )
            self.settings_service.save(settings)
            self.settings = settings
            performance_result = apply_performance_mode(
                settings.performance_mode,
                current_profile=self.resource_profile,
                diagnostics=self.performance_diagnostics,
            )
            self.resource_profile = performance_result.profile
            model_reset = performance_result.model_reset
            active_profile = performance_result.profile
            if self.settings_page is not None:
                self.settings_page.performance_summary = active_profile.summary
                self.settings_page.refresh_performance_info()
            self.documents_dir = documents_dir
            self.confirm_delete_enabled = settings.confirm_delete
            self.request_service.set_documents_dir(documents_dir)

            if self.settings_page is not None:
                self.settings_page.set_values(
                    documents_dir,
                    self.confirm_delete_enabled,
                    settings.performance_mode,
                    settings.update_source_dir,
                )
            if self.documents_page is not None:
                self.documents_page.refresh()

            if settings.performance_mode != previous_mode:
                message = (
                    f"{active_profile.performance_mode_label}: OCR {active_profile.worker_threads} поток(а), "
                    f"{active_profile.ocr_dpi}/{active_profile.retry_dpi} DPI. Режим уже активен."
                )
                if model_reset:
                    message += " OCR-модель будет один раз загружена заново при следующем скане."
                self.show_toast("Режим применён", message, "success")
            else:
                self.show_toast("Настройки сохранены", "Изменения применены.")
        except Exception as error:
            logger.exception("Ошибка сохранения настроек")
            QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить настройки:\n{error}")
