import logging
from dataclasses import replace

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
        self.active_dialog: RequestDialog | None = None
        self._is_closing: bool = False

    def close_active_dialog(self) -> None:
        self._is_closing = True
        dialog = self.active_dialog
        if dialog is not None:
            self.active_dialog = None
            if hasattr(dialog, "force_close"):
                dialog.force_close()
            else:
                dialog.reject()

    def add_request(self):
        draft_request = None
        force_dirty_on_reopen = False
        while True:
            if self._is_closing or (self.parent_widget is not None and getattr(self.parent_widget, "_is_closing", False)):
                return
            dialog = RequestDialog(
                self.parent_widget,
                request=draft_request,
                documents_dir=self.documents_dir_getter(),
                suggestions=self.request_service.get_form_suggestions(),
                request_service=self.request_service,
            )
            if force_dirty_on_reopen:
                dialog._is_dirty = True
                force_dirty_on_reopen = False
            self.active_dialog = dialog
            if self.parent_widget is not None and hasattr(self.parent_widget, "register_dialog"):
                self.parent_widget.register_dialog(dialog)
            try:
                res = dialog.exec()
                switch_target_id = dialog.switch_target_request_id
                switch_save_current = bool(dialog.switch_save_current)
                dialog_request = None
                dialog_request_error = None
                if res == QDialog.DialogCode.Accepted or (
                    res == RequestDialog.SWITCH_REQUEST_RESULT and switch_save_current
                ):
                    try:
                        dialog_request = dialog.get_request()
                    except Exception as error:
                        dialog_request_error = error
            finally:
                self.active_dialog = None
                if self.parent_widget is not None and hasattr(self.parent_widget, "unregister_dialog"):
                    self.parent_widget.unregister_dialog(dialog)
                dialog.deleteLater()
            if self._is_closing or (self.parent_widget is not None and getattr(self.parent_widget, "_is_closing", False)):
                return
            if res == RequestDialog.SWITCH_REQUEST_RESULT:
                if switch_save_current:
                    if dialog_request_error is not None:
                        QMessageBox.critical(
                            self.parent_widget,
                            "Ошибка формы",
                            f"Не удалось прочитать данные запроса:\n\n{dialog_request_error}",
                        )
                        return
                    draft = dialog_request
                    try:
                        self.request_service.create_request(draft)
                        self.changed.emit()
                        self.toast_requested.emit("Запрос создан", "Новый запрос успешно добавлен в реестр.", "success")
                    except Exception as error:
                        logger.exception("Ошибка при создании запроса перед переходом")
                        QMessageBox.critical(self.parent_widget, "Ошибка сохранения", f"Не удалось создать запрос:\n\n{error}")
                        draft_request = draft
                        force_dirty_on_reopen = True
                        continue
                target_id = switch_target_id
                if target_id is not None:
                    target = self.request_service.get_request(int(target_id))
                    if target is not None:
                        self.edit_request(target)
                return
            if res != QDialog.DialogCode.Accepted:
                return
            if dialog_request_error is not None:
                QMessageBox.critical(
                    self.parent_widget,
                    "Ошибка формы",
                    f"Не удалось прочитать данные запроса:\n\n{dialog_request_error}",
                )
                return
            try:
                self.request_service.create_request(dialog_request)
            except Exception as error:
                logger.exception("Ошибка при создании запроса")
                QMessageBox.critical(self.parent_widget, "Ошибка сохранения", f"Не удалось создать запрос:\n\n{error}")
                draft_request = dialog_request
                force_dirty_on_reopen = True
                continue
            self.changed.emit()
            self.toast_requested.emit("Запрос создан", "Новый запрос успешно добавлен в реестр.", "success")
            return

    def edit_request(self, request):
        """Open request dialogs in one controller loop, avoiding nested RequestDialog.exec()."""
        if request is None or request.id is None:
            return
        request_to_open = self.request_service.get_request(int(request.id)) or request
        force_dirty_on_reopen = False

        while request_to_open is not None:
            if self._is_closing or (self.parent_widget is not None and getattr(self.parent_widget, "_is_closing", False)):
                return
            current_id = int(request_to_open.id)
            diagnostics = self.request_service.get_parser_metadata(current_id) if current_id else {}
            dup_info = self.request_service.get_request_duplicate_info(current_id) if current_id else None
            presented_request = replace(request_to_open)
            dialog = RequestDialog(
                self.parent_widget,
                request=request_to_open,
                documents_dir=self.documents_dir_getter(),
                suggestions=self.request_service.get_form_suggestions(),
                parser_diagnostics=diagnostics,
                duplicate_info=dup_info,
                request_service=self.request_service,
            )
            if force_dirty_on_reopen:
                dialog._is_dirty = True
                force_dirty_on_reopen = False

            self.active_dialog = dialog
            if self.parent_widget is not None and hasattr(self.parent_widget, "register_dialog"):
                self.parent_widget.register_dialog(dialog)
            try:
                result = dialog.exec()
                switch_target_id = dialog.switch_target_request_id
                switch_save_current = bool(dialog.switch_save_current)
                dialog_request = None
                dialog_request_error = None
                if result == QDialog.DialogCode.Accepted or (
                    result == RequestDialog.SWITCH_REQUEST_RESULT and switch_save_current
                ):
                    try:
                        dialog_request = dialog.get_request()
                    except Exception as error:
                        dialog_request_error = error
            finally:
                self.active_dialog = None
                if self.parent_widget is not None and hasattr(self.parent_widget, "unregister_dialog"):
                    self.parent_widget.unregister_dialog(dialog)
                dialog.deleteLater()

            if self._is_closing or (self.parent_widget is not None and getattr(self.parent_widget, "_is_closing", False)):
                return
            if result == RequestDialog.DELETE_RESULT:
                try:
                    self.request_service.delete_request(current_id)
                except Exception as error:
                    logger.exception("Ошибка удаления запроса из карточки")
                    QMessageBox.critical(self.parent_widget, "Ошибка удаления", f"Не удалось удалить запрос:\n\n{error}")
                    return
                self.changed.emit()
                self.toast_requested.emit("Запрос удалён", "Запрос успешно удалён из реестра.", "warning")
                return

            if result == RequestDialog.SWITCH_REQUEST_RESULT:
                target_id = switch_target_id
                if target_id is None:
                    return
                if switch_save_current:
                    if dialog_request_error is not None:
                        QMessageBox.critical(
                            self.parent_widget,
                            "Ошибка формы",
                            f"Не удалось прочитать данные запроса:\n\n{dialog_request_error}",
                        )
                        return
                    updated = dialog_request
                    try:
                        self.request_service.update_request(updated)
                        self.request_service.learn_parser_corrections(
                            presented_request, updated, context=diagnostics
                        )
                        self.changed.emit()
                    except Exception as error:
                        logger.exception("Не удалось сохранить запрос перед переключением")
                        QMessageBox.critical(
                            self.parent_widget,
                            "Ошибка сохранения",
                            f"Не удалось сохранить запрос #{current_id}. Вы можете повторить сохранение.\n\n{error}",
                        )
                        # Keep the user's edits in the next dialog instead of dropping them.
                        request_to_open = updated
                        force_dirty_on_reopen = True
                        continue
                target = self.request_service.get_request(int(target_id))
                if target is None:
                    QMessageBox.warning(self.parent_widget, "Запрос не найден", f"Связанный запрос #{target_id} больше не существует.")
                    request_to_open = self.request_service.get_request(current_id)
                    continue
                request_to_open = target
                continue

            if result != QDialog.DialogCode.Accepted:
                return
            if dialog_request_error is not None:
                QMessageBox.critical(
                    self.parent_widget,
                    "Ошибка формы",
                    f"Не удалось прочитать данные запроса:\n\n{dialog_request_error}",
                )
                return
            try:
                updated = dialog_request
                self.request_service.update_request(updated)
                self.request_service.learn_parser_corrections(
                    presented_request, updated, context=diagnostics
                )
            except Exception as error:
                logger.exception("Ошибка обновления запроса")
                QMessageBox.critical(self.parent_widget, "Ошибка сохранения", f"Не удалось обновить запрос:\n\n{error}")
                request_to_open = dialog_request if dialog_request is not None else request_to_open
                force_dirty_on_reopen = True
                continue
            self.changed.emit()
            self.toast_requested.emit("Запрос обновлён", "Изменения успешно сохранены.", "success")
            return

    def delete_requests(self, requests, confirm: bool = True):
        requests = list(requests)
        if not requests:
            self.toast_requested.emit(
                "Запросы не выбраны",
                "Выберите одну или несколько записей в таблице.",
                "info",
            )
            return
        if confirm:
            dialog = DeleteRequestDialog(requests, self.parent_widget)
            try:
                result = dialog.exec()
            finally:
                dialog.deleteLater()
            if result != QDialog.DialogCode.Accepted:
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

