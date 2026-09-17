import logging
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

from app.document_service import DocumentService
from app.request_service import RequestService


logger = logging.getLogger(__name__)


class DocumentController(QObject):
    changed = Signal()
    toast_requested = Signal(str, str, str)

    def __init__(self, document_service: DocumentService, request_service: RequestService, parent=None):
        super().__init__(parent)
        self.document_service = document_service
        self.request_service = request_service
        self.parent_widget = parent

    def open_document(self, path):
        try:
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                QMessageBox.warning(self.parent_widget, "Ошибка", f"Не удалось открыть документ:\n{path}")
        except Exception as error:
            logger.exception("Ошибка открытия документа %s", path)
            QMessageBox.warning(self.parent_widget, "Ошибка", f"Произошла непредвиденная ошибка:\n{error}")

    def delete_document(self, path):
        path = Path(path)
        if not path.exists():
            QMessageBox.warning(self.parent_widget, "Файл не найден", f"Документ уже отсутствует:\n\n{path.name}")
            self.toast_requested.emit("Файл отсутствует", "Запись в реестре не изменялась.", "warning")
            self.changed.emit()
            return

        result = QMessageBox.question(
            self.parent_widget,
            "Удаление документа",
            f"Удалить документ?\n\n{path.name}\n\nСвязанный запрос также будет удалён.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            return

        try:
            related = self.document_service.find_related_request(path)
            if related is not None:
                self.request_service.delete_request(related.id)
                message = "Документ и связанный с ним запрос удалены."
            else:
                self.document_service.delete_untracked_document(path)
                message = "Документ удалён из хранилища."
        except Exception as error:
            logger.exception("Ошибка удаления документа %s", path)
            QMessageBox.critical(self.parent_widget, "Ошибка удаления", f"Не удалось удалить документ:\n\n{error}")
            return

        self.changed.emit()
        self.toast_requested.emit("Документ удалён", message, "warning")
