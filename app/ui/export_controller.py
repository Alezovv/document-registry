from __future__ import annotations

from datetime import date
import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox

from app.export_service import ExportError, ExportService


logger = logging.getLogger(__name__)


class ExportController(QObject):
    toast_requested = Signal(str, str, str)

    def __init__(self, export_service: ExportService | None = None, parent=None):
        super().__init__(parent)
        self.export_service = export_service or ExportService()
        self.parent_widget = parent

    def export_requests(self, requests):
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
        try:
            result = self.export_service.export_xlsx(requests, path)
            self.toast_requested.emit(
                "Экспорт завершён",
                f"Сохранено записей: {result.row_count}",
                "success",
            )
        except ExportError as error:
            logger.exception("Ошибка экспорта Excel")
            QMessageBox.critical(self.parent_widget, "Ошибка экспорта", str(error))
