from __future__ import annotations

import logging

from PySide6.QtCore import QTimer

from app import __version__
from app.update_config import UPDATE_CHECK_INTERVAL_SECONDS, UPDATE_STARTUP_DELAY_MS
from app.update_service import UpdateService
from app.ui.update_controller import UpdateController
from app.ui.ocr_diagnostics_controller import OcrDiagnosticsController

logger = logging.getLogger(__name__)


class UpdateWindowMixin:
    """Keep update-specific wiring out of MainWindow."""

    def _setup_update_system(self) -> None:
        self.update_service = UpdateService(self.paths)
        self.update_controller = UpdateController(
            self.update_service,
            __version__,
            toast=self.show_toast,
            parent=self,
            backup_service=getattr(self, "backup_service", None),
        )
        self.ocr_diagnostics_controller = OcrDiagnosticsController(
            toast=self.show_toast,
            parent=self,
        )

        # First check is delayed so application startup, DB opening and the
        # registry page are never blocked by a slow or unavailable Internet.
        QTimer.singleShot(1800, self._show_last_update_result)
        QTimer.singleShot(3500, self._finalize_pending_updater)
        QTimer.singleShot(UPDATE_STARTUP_DELAY_MS, self._check_updates_on_startup)

        # While the program is open, check once an hour. If Windows sleeps for
        # a long time, Qt fires the overdue timer after the event loop resumes.
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(UPDATE_CHECK_INTERVAL_SECONDS * 1000)
        self._update_timer.timeout.connect(self._check_updates_periodically)
        self._update_timer.start()

    def _bind_update_settings_page(self, widget) -> None:
        widget.update_check_requested.connect(
            lambda source="": self.update_controller.check(
                source_dir=source or None,
                manual=True,
            )
        )
        if hasattr(widget, "rollback_requested"):
            widget.rollback_requested.connect(
                lambda source="": self.update_controller.open_rollback_dialog(
                    source_dir=source or None,
                )
            )
        widget.ocr_check_requested.connect(self.ocr_diagnostics_controller.run)

    def _show_last_update_result(self) -> None:
        try:
            status = self.update_service.consume_last_install_status()
        except Exception:
            logger.exception("Не удалось прочитать итог последнего обновления")
            return
        if not status:
            return
        state = str(status.get("state") or "")
        version = str(status.get("version") or "").strip()
        message = str(status.get("message") or "").strip()
        is_rollback = bool(status.get("rollback"))
        if state == "success":
            if is_rollback:
                text = (
                    f"Выполнен откат на версию {version}. Прежняя БД сохранена в резервной копии."
                    if version
                    else "Откат на предыдущую версию успешно завершён."
                )
                self.show_toast("Откат завершён", text, "success")
            else:
                text = f"Версия {version} успешно установлена." if version else "Обновление успешно установлено."
                self.show_toast("Обновление завершено", text, "success")
        elif state == "failed":
            self.show_toast(
                "Откат не выполнен" if is_rollback else "Обновление не установлено",
                message or "Предыдущая версия восстановлена.",
                "warning",
            )

    def _finalize_pending_updater(self) -> None:
        try:
            self.update_service.finalize_pending_updater()
        except Exception:
            logger.exception("Не удалось завершить обновление updater")

    def _check_updates_on_startup(self) -> None:
        try:
            self.update_controller.check(manual=False)
        except Exception:
            logger.exception("Не удалось проверить обновления при запуске")

    def _check_updates_periodically(self) -> None:
        try:
            self.update_controller.check(manual=False)
        except Exception:
            logger.exception("Не удалось выполнить периодическую проверку обновлений")
