import logging
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QDialog, QMessageBox

from app.document_service import DocumentService
from app.import_dialog import ImportDocumentDialog
from app.request_service import RequestService
from app.parser_postprocess import correct_request_with_known_values
from app.ui.dialogs.request_dialog import RequestDialog


logger = logging.getLogger(__name__)


class ImportController(QObject):
    finished = Signal()
    toast_requested = Signal(str, str, str)

    def __init__(
        self,
        request_service: RequestService,
        document_service: DocumentService,
        documents_dir_getter,
        parent=None,
    ):
        super().__init__(parent)
        self.request_service = request_service
        self.document_service = document_service
        self.documents_dir_getter = documents_dir_getter
        self.parent_widget = parent
        self.dialog: ImportDocumentDialog | None = None

    def open(self):
        if self.dialog is not None:
            self.dialog.restore_from_background()
            return

        cache_dir = Path(self.documents_dir_getter()).parent / "parser_cache"
        dialog = ImportDocumentDialog(self.parent_widget, cache_dir=cache_dir)
        self.dialog = dialog
        dialog.request_ready.connect(self._review_and_save)
        dialog.notice.connect(self.toast_requested.emit)
        dialog.backgrounded.connect(
            lambda: self.toast_requested.emit(
                "OCR работает в фоне",
                "Очередь документов продолжает обрабатываться. Откройте импорт ещё раз, чтобы вернуть окно.",
                "info",
            )
        )
        dialog.finished.connect(self._on_dialog_finished)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_dialog_finished(self, _result):
        self.dialog = None
        self.finished.emit()

    def _choose_duplicate_action(self, duplicate, *, initial: bool) -> str:
        box = QMessageBox(self.parent_widget)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Документ уже существует")
        box.setText("Этот документ уже есть в реестре." if initial else "Такой файл уже есть в реестре.")
        box.setInformativeText(f"Файл: {Path(duplicate.source_document).name}\n\nЧто сделать?")
        open_button = box.addButton("Открыть существующий", QMessageBox.ButtonRole.AcceptRole)
        skip_button = box.addButton("Пропустить файл" if initial else "Пропустить", QMessageBox.ButtonRole.RejectRole)
        replace_button = box.addButton("Оставить и заменить старый" if initial else "Заменить старый", QMessageBox.ButtonRole.ActionRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked == open_button:
            self._open_document(duplicate.source_document)
            return "open"
        if clicked == replace_button:
            return "replace"
        if clicked == skip_button:
            return "skip"
        return "skip"

    def _open_document(self, path: str | Path):
        try:
            url = QUrl.fromLocalFile(str(path))
            if not QDesktopServices.openUrl(url):
                QMessageBox.warning(self.parent_widget, "Ошибка", f"Не удалось открыть документ:\n{path}")
        except Exception as error:
            logger.exception("Ошибка открытия документа %s", path)
            QMessageBox.warning(self.parent_widget, "Ошибка", f"Произошла непредвиденная ошибка:\n{error}")

    def _review_and_save(self, request, pdf_path: str):
        result_code = "error"
        manual_review_started = False
        diagnostics = getattr(request, "_parser_diagnostics", {}) or {}
        try:
            duplicate = self.document_service.find_duplicate_document(pdf_path)
            replace_existing = False

            if duplicate is not None:
                action = self._choose_duplicate_action(duplicate, initial=True)
                if action in {"open", "skip"}:
                    result_code = "skipped"
                    return
                request.id = duplicate.id
                request.status = duplicate.status or request.status
                replace_existing = True

            request.source_document = pdf_path

            if self.dialog is not None:
                self.dialog.begin_manual_review()
                manual_review_started = True

            # Apply local, user-confirmed parser corrections before the normal
            # registry-value canonicalization.  Keep exactly what the user sees
            # so only their explicit edits are learned after a successful save.
            request = self.request_service.apply_parser_corrections(request)
            suggestions = self.request_service.get_form_suggestions()
            request = correct_request_with_known_values(request, suggestions)
            presented_request = replace(request)
            review = RequestDialog(
                self.parent_widget,
                request=request,
                documents_dir=self.documents_dir_getter(),
                suggestions=suggestions,
                parser_diagnostics=diagnostics or {},
            )
            if review.exec() != QDialog.DialogCode.Accepted:
                result_code = "skipped"
                return

            request = review.get_request()
            source_changed = bool(request.source_document) and (
                self.document_service.normalize_path(request.source_document)
                != self.document_service.normalize_path(pdf_path)
            )
            if source_changed:
                final_duplicate = self.document_service.find_duplicate_document(
                    request.source_document,
                    exclude_request_id=request.id,
                )
                if final_duplicate is not None:
                    action = self._choose_duplicate_action(final_duplicate, initial=False)
                    if action in {"open", "skip"}:
                        result_code = "skipped"
                        return
                    request.id = final_duplicate.id
                    request.status = final_duplicate.status or request.status
                    replace_existing = True

            if replace_existing and request.id is not None:
                self.request_service.update_request(request)
                title = "Запрос обновлён"
                message = "Документ заменён, данные запроса сохранены."
            else:
                self.request_service.create_request(request)
                title = "Запрос создан"
                message = "Документ успешно добавлен в реестр."

            learned = self.request_service.learn_parser_corrections(
                presented_request, request
            )
            if learned:
                logger.info("Сохранено исправлений parser: %s", learned)

            result_code = "saved"
            self.toast_requested.emit(title, message, "success")
            logger.info("Импортирован документ %s", request.source_document)
        except Exception as error:
            logger.exception("Ошибка сохранения импортированного документа")
            QMessageBox.critical(
                self.parent_widget,
                "Ошибка сохранения",
                f"Не удалось сохранить документ:\n\n{error}",
            )
        finally:
            dialog = self.dialog
            if manual_review_started and dialog is not None:
                dialog.end_manual_review()
            if dialog is not None:
                dialog.complete_review(result_code)

