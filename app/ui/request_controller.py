import logging
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QDialog, QMessageBox

from app.request_service import RequestService
from app.ui.dialogs.delete_request_dialog import DeleteRequestDialog
from app.ui.dialogs.request_dialog import RequestDialog


logger = logging.getLogger(__name__)


class RequestController(QObject):
    changed = Signal()
    toast_requested = Signal(str, str, str)

    def __init__(self, request_service: RequestService, documents_dir_getter, parent=None):
        super().__init__(parent)
        self.request_service = request_service
        self.documents_dir_getter = documents_dir_getter
        self.parent_widget = parent

    def add_request(self):
        dialog = RequestDialog(
            self.parent_widget,
            documents_dir=self.documents_dir_getter(),
            suggestions=self.request_service.get_form_suggestions(),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.request_service.create_request(dialog.get_request())
        except Exception as error:
            logger.exception("Ошибка при создании запроса")
            QMessageBox.critical(self.parent_widget, "Ошибка сохранения", f"Не удалось создать запрос:\n\n{error}")
            return
        self.changed.emit()
        self.toast_requested.emit("Запрос создан", "Новый запрос успешно добавлен в реестр.", "success")

    def edit_request(self, request):
        if request is None:
            return
        diagnostics = self.request_service.get_parser_metadata(request.id) if request.id else {}
        dup_info = self.request_service.get_request_duplicate_info(request.id) if request.id else None
        presented_request = replace(request)
        dialog = RequestDialog(
            self.parent_widget,
            request=request,
            documents_dir=self.documents_dir_getter(),
            suggestions=self.request_service.get_form_suggestions(),
            parser_diagnostics=diagnostics,
            duplicate_info=dup_info,
        )
        result = dialog.exec()
        if result == RequestDialog.DELETE_RESULT:
            try:
                self.request_service.delete_request(request.id)
            except Exception as error:
                logger.exception("Ошибка удаления запроса из карточки")
                QMessageBox.critical(self.parent_widget, "Ошибка удаления", f"Не удалось удалить запрос:\n\n{error}")
                return
            self.changed.emit()
            self.toast_requested.emit("Запрос удалён", "Запрос успешно удалён из реестра.", "warning")
            return
        if result != QDialog.DialogCode.Accepted:
            return
        try:
            updated = dialog.get_request()
            self.request_service.update_request(updated)
            self.request_service.learn_parser_corrections(
                presented_request, updated, context=diagnostics
            )
        except Exception as error:
            logger.exception("Ошибка обновления запроса")
            QMessageBox.critical(self.parent_widget, "Ошибка сохранения", f"Не удалось обновить запрос:\n\n{error}")
            return
        self.changed.emit()
        self.toast_requested.emit("Запрос обновлён", "Изменения успешно сохранены.", "success")

    def delete_requests(self, requests, confirm: bool = True):
        requests = list(requests)
        if not requests:
            self.toast_requested.emit(
                "Запросы не выбраны",
                "Выберите одну или несколько записей в таблице.",
                "info",
            )
            return
        if confirm and DeleteRequestDialog(requests, self.parent_widget).exec() != QDialog.DialogCode.Accepted:
            return
        deleted_count = 0
        try:
            for request in requests:
                if self.request_service.delete_request(request.id):
                    deleted_count += 1
        except Exception as error:
            logger.exception("Ошибка удаления запросов")
            if deleted_count:
                self.changed.emit()
            QMessageBox.critical(
                self.parent_widget,
                "Ошибка удаления",
                f"Удалено до ошибки: {deleted_count}.\n\nНе удалось завершить удаление:\n{error}",
            )
            return
        self.changed.emit()
        self.toast_requested.emit("Запись удалена", f"Удалено: {deleted_count} (доступно в Истории)", "warning")
    def apply_bulk_action(self, action: str, requests, confirm_delete: bool = True):
        requests = list(requests)
        if not requests:
            return
        if action == "delete":
            self.delete_requests(requests, confirm=confirm_delete)
            return

        request_ids = [request.id for request in requests if request.id is not None]
        if not request_ids:
            return

        try:
            if action.startswith("status:"):
                status_map = {
                    "new": "Новый",
                    "progress": "В работе",
                    "done": "Исполнен",
                    "rejected": "Отклонен",
                }
                status = status_map.get(action.split(":", 1)[1])
                if status is None:
                    return
                changed = self.request_service.bulk_update_requests(request_ids, status=status)
                message = f"Статус «{status}» установлен для записей: {changed}"
            elif action.startswith("due:"):
                mode = action.split(":", 1)[1]
                changed = self.request_service.bulk_update_requests(request_ids, due_mode=mode)
                labels = {
                    "today": "сегодня",
                    "+7": "+7 дней",
                    "+14": "+14 дней",
                    "+30": "+30 дней",
                    "clear": "очищен",
                }
                message = f"Срок ({labels.get(mode, mode)}) применён к записям: {changed}"
            elif action.startswith("pin:"):
                pinned = action.endswith(":on")
                changed = self.request_service.bulk_update_requests(request_ids, pinned=pinned)
                message = ("Закреплено" if pinned else "Откреплено") + f" записей: {changed}"
            else:
                return
        except Exception as error:
            logger.exception("Ошибка массовой операции %s", action)
            QMessageBox.critical(
                self.parent_widget,
                "Ошибка массовой операции",
                f"Не удалось применить действие:\n\n{error}",
            )
            return

        self.changed.emit()
        self.toast_requested.emit("Массовое действие", message, "success")

