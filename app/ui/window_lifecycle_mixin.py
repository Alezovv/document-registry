from __future__ import annotations

import logging
from PySide6.QtWidgets import QApplication, QDialog
from shiboken6 import isValid

logger = logging.getLogger(__name__)


class WindowLifecycleMixin:
    """Mixin providing child dialog tracking and clean shutdown logic for MainWindow."""

    def _init_lifecycle(self) -> None:
        self._is_closing: bool = False
        self._active_dialogs: set[QDialog] = set()

    def register_dialog(self, dialog: QDialog) -> None:
        """Register an active child or top-level dialog with MainWindow."""
        if dialog is not None:
            self._active_dialogs.add(dialog)
            try:
                dialog.finished.connect(lambda *_: self.unregister_dialog(dialog))
            except Exception:
                pass
            try:
                dialog.destroyed.connect(lambda *_: self._active_dialogs.discard(dialog))
            except Exception:
                pass

    def unregister_dialog(self, dialog: QDialog) -> None:
        """Unregister a finished dialog."""
        self._active_dialogs.discard(dialog)

    def get_active_dialogs(self) -> list[QDialog]:
        """Return currently living and visible registered dialogs."""
        valid_dialogs = []
        for d in list(self._active_dialogs):
            try:
                if isValid(d) and d.isVisible():
                    valid_dialogs.append(d)
            except Exception:
                pass
        return valid_dialogs

    def closeEvent(self, event):
        """Cleanly close all dialogs and terminate/interrupt background tasks."""
        self._is_closing = True

        # 1. Close import controller dialogs and stop OCR workers
        if hasattr(self, "import_controller") and self.import_controller is not None:
            try:
                self.import_controller.close_active_dialogs()
            except Exception:
                logger.exception("Ошибка при закрытии диалогов import_controller")

        # 2. Close request controller active dialog
        if hasattr(self, "request_controller") and self.request_controller is not None:
            try:
                self.request_controller.close_active_dialog()
            except Exception:
                logger.exception("Ошибка при закрытии диалогов request_controller")

        # 3. Stop OCR diagnostics thread if running
        if hasattr(self, "ocr_diagnostics_controller") and self.ocr_diagnostics_controller is not None:
            try:
                thread = getattr(self.ocr_diagnostics_controller, "_thread", None)
                if thread is not None and thread.isRunning():
                    thread.requestInterruption()
                    thread.wait(2000)
            except Exception:
                pass

        # 4. Stop backup worker if running
        if hasattr(self, "backup_controller") and self.backup_controller is not None:
            try:
                if hasattr(self.backup_controller, "stop"):
                    self.backup_controller.stop()
                else:
                    worker = getattr(self.backup_controller, "worker", None)
                    if worker is not None and worker.isRunning():
                        worker.requestInterruption()
                        worker.wait(2000)
            except Exception:
                pass

        # 5. Stop legacy import worker if running
        if hasattr(self, "legacy_import_controller") and self.legacy_import_controller is not None:
            try:
                if hasattr(self.legacy_import_controller, "stop"):
                    self.legacy_import_controller.stop()
                else:
                    worker = getattr(self.legacy_import_controller, "worker", None)
                    if worker is not None and worker.isRunning():
                        worker.requestInterruption()
                        worker.wait(2000)
            except Exception:
                pass

        # 6. Stop search index backfill worker if running
        if hasattr(self, "search_index_controller") and self.search_index_controller is not None:
            try:
                self.search_index_controller.shutdown()
            except Exception:
                pass

        # 7. Stop periodic update timers / threads
        if hasattr(self, "_update_timer") and self._update_timer is not None:
            try:
                self._update_timer.stop()
            except Exception:
                pass
        if hasattr(self, "update_controller") and self.update_controller is not None:
            try:
                if hasattr(self.update_controller, "stop"):
                    self.update_controller.stop()
                else:
                    for t in (
                        getattr(self.update_controller, "_check_thread", None),
                        getattr(self.update_controller, "_rollback_check_thread", None),
                        getattr(self.update_controller, "_prepare_thread", None),
                    ):
                        if t is not None and t.isRunning():
                            t.requestInterruption()
                            t.wait(1500)
                    if hasattr(self.update_controller, "_close_progress_dialog"):
                        self.update_controller._close_progress_dialog()
            except Exception:
                pass

        # 8. Close any remaining registered dialogs
        for dialog in list(self._active_dialogs):
            try:
                if isValid(dialog) and dialog.isVisible():
                    if hasattr(dialog, "force_close"):
                        dialog.force_close()
                    else:
                        dialog.reject()
            except Exception:
                pass
        self._active_dialogs.clear()

        # 9. Also check all top-level QDialogs in the application to ensure clean exit
        app = QApplication.instance()
        if app is not None:
            for widget in app.topLevelWidgets():
                try:
                    if widget is not self and isinstance(widget, QDialog) and isValid(widget) and widget.isVisible():
                        if hasattr(widget, "force_close"):
                            widget.force_close()
                        else:
                            widget.reject()
                except Exception:
                    pass

        event.accept()
