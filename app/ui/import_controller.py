import logging
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Qt
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
        self.review_dialog: RequestDialog | None = None
        self._review_context: dict | None = None

    def open(self):
        if self.review_dialog is not None:
            # One parsed document is waiting for an operator decision. Keep the
            # queue paused, but never block navigation in the main window.
            self.review_dialog.showNormal()
            self.review_dialog.raise_()
            self.review_dialog.activateWindow()
            return
        if self.dialog is not None:
            if self.dialog.property("batch_finished") and not self.dialog.processing:
                self.dialog.prepare_for_new_batch()
            self.dialog.restore_from_background()
            return

        cache_dir = Path(self.documents_dir_getter()).parent / "parser_cache"
        dialog = ImportDocumentDialog(
            self.parent_widget,
            cache_dir=cache_dir,
            preflight_callback=self._preflight_document,
        )
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
        box.setText("Этот PDF уже зарегистрирован в реестре.")
        box.setInformativeText(
            f"Запись: #{duplicate.id}\n"
            f"Файл: {Path(duplicate.source_document).name}\n\n"
            "Проверка выполнена по SHA-256 содержимого, поэтому переименование файла не скрывает дубликат."
        )
        open_button = box.addButton("Открыть существующую запись", QMessageBox.ButtonRole.AcceptRole)
        force_button = box.addButton("Импортировать копию", QMessageBox.ButtonRole.ActionRole)
        skip_button = box.addButton("Пропустить", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked == open_button:
            self._open_existing_request(duplicate)
            return "open"
        if clicked == force_button:
            return "force"
        if clicked == skip_button:
            return "skip"
        return "skip"

    def _preflight_document(self, pdf_path: str) -> str:
        """Detect duplicates before OCR so repeated PDFs cost almost no RAM/CPU."""
        duplicate = self.request_service.find_duplicate_document(pdf_path)
        if duplicate is None:
            return "continue"
        return self._choose_duplicate_action(duplicate, initial=True)

    def _open_existing_request(self, request) -> None:
        try:
            diagnostics = self.request_service.get_parser_metadata(request.id) if request.id else {}
            dialog = RequestDialog(
                self.parent_widget,
                request=request,
                documents_dir=self.documents_dir_getter(),
                suggestions=self.request_service.get_form_suggestions(),
                parser_diagnostics=diagnostics,
            )
            if dialog.exec() == QDialog.DialogCode.Accepted:
                updated = dialog.get_request()
                self.request_service.update_request(updated)
                self.request_service.learn_parser_corrections(request, updated, context=diagnostics)
                self.toast_requested.emit("Запрос обновлён", f"Запись #{updated.id} сохранена.", "success")
        except Exception as error:
            logger.exception("Не удалось открыть существующую запись-дубликат")
            QMessageBox.warning(self.parent_widget, "Дубликат", f"Не удалось открыть запись:\n{error}")

    def _open_document(self, path: str | Path):
        try:
            url = QUrl.fromLocalFile(str(path))
            if not QDesktopServices.openUrl(url):
                QMessageBox.warning(self.parent_widget, "Ошибка", f"Не удалось открыть документ:\n{path}")
        except Exception as error:
            logger.exception("Ошибка открытия документа %s", path)
            QMessageBox.warning(self.parent_widget, "Ошибка", f"Произошла непредвиденная ошибка:\n{error}")

    def _review_and_save(self, request, pdf_path: str):
        """Open parsed-document review without blocking the main window."""
        if self.review_dialog is not None:
            logger.error("Получен второй запрос на review, пока предыдущий ещё открыт")
            if self.dialog is not None:
                self.dialog.complete_review("error")
            return

        diagnostics = getattr(request, "_parser_diagnostics", {}) or {}
        ocr_text = str(getattr(request, "_ocr_text", "") or "")
        allow_duplicate = bool(getattr(request, "_allow_duplicate", False))
        manual_review_started = False
        try:
            if not allow_duplicate:
                duplicate = self.request_service.find_duplicate_document(pdf_path)
                if duplicate is not None:
                    action = self._choose_duplicate_action(duplicate, initial=True)
                    if action in {"open", "skip"}:
                        if self.dialog is not None:
                            self.dialog.complete_review("skipped")
                        return
                    allow_duplicate = action == "force"

            request.source_document = pdf_path
            if self.dialog is not None:
                self.dialog.begin_manual_review()
                manual_review_started = True

            request = self.request_service.apply_parser_corrections(request, context=diagnostics)
            suggestions = self.request_service.get_form_suggestions()
            request = correct_request_with_known_values(request, suggestions)
            if ocr_text:
                request._ocr_text = ocr_text
            presented_request = replace(request)

            # This window is deliberately parentless and non-modal. On Windows
            # an owned/modal dialog can minimize or disable its owner. A true
            # top-level review window lets the operator use Registry/Search and
            # other tabs while the import queue waits for this decision.
            review = RequestDialog(
                None,
                request=request,
                documents_dir=self.documents_dir_getter(),
                suggestions=suggestions,
                parser_diagnostics=diagnostics or {},
            )
            review.setWindowModality(Qt.WindowModality.NonModal)
            review.setModal(False)
            if self.parent_widget is not None:
                review.setWindowIcon(self.parent_widget.windowIcon())

            self.review_dialog = review
            self._review_context = {
                "pdf_path": str(pdf_path),
                "diagnostics": diagnostics,
                "ocr_text": ocr_text,
                "allow_duplicate": allow_duplicate,
                "presented_request": presented_request,
                "manual_review_started": manual_review_started,
            }
            review.finished.connect(self._on_review_finished)
            review.show()
            review.raise_()
            review.activateWindow()
        except Exception as error:
            logger.exception("Не удалось открыть проверку импортированного документа")
            QMessageBox.critical(
                self.parent_widget,
                "Ошибка проверки",
                f"Не удалось открыть документ для проверки:\n\n{error}",
            )
            if manual_review_started and self.dialog is not None:
                self.dialog.end_manual_review()
                self.dialog.complete_review("error")

    def _on_review_finished(self, result: int):
        review = self.review_dialog
        context = self._review_context or {}
        self.review_dialog = None
        self._review_context = None
        result_code = "skipped"

        try:
            if review is None or result != QDialog.DialogCode.Accepted:
                return

            request = review.get_request()
            diagnostics = context.get("diagnostics") or {}
            ocr_text = str(context.get("ocr_text") or "")
            allow_duplicate = bool(context.get("allow_duplicate", False))
            pdf_path = str(context.get("pdf_path") or "")
            presented_request = context.get("presented_request")

            request._parser_diagnostics = diagnostics
            request._allow_duplicate = allow_duplicate
            if ocr_text:
                request._ocr_text = ocr_text

            source_changed = bool(request.source_document) and (
                self.document_service.normalize_path(request.source_document)
                != self.document_service.normalize_path(pdf_path)
            )
            if source_changed and not allow_duplicate:
                final_duplicate = self.request_service.find_duplicate_document(
                    request.source_document, exclude_request_id=request.id
                )
                if final_duplicate is not None:
                    action = self._choose_duplicate_action(final_duplicate, initial=False)
                    if action in {"open", "skip"}:
                        return
                    allow_duplicate = action == "force"
                    request._allow_duplicate = allow_duplicate

            self.request_service.create_request(request)
            learned = self.request_service.learn_parser_corrections(
                presented_request, request, context=diagnostics
            )
            if learned:
                logger.info("Сохранено исправлений parser: %s", learned)

            result_code = "saved"
            self.toast_requested.emit(
                "Запрос создан", "Документ успешно добавлен в реестр.", "success"
            )
            logger.info("Импортирован документ %s", request.source_document)
        except Exception as error:
            result_code = "error"
            logger.exception("Ошибка сохранения импортированного документа")
            QMessageBox.critical(
                self.parent_widget,
                "Ошибка сохранения",
                f"Не удалось сохранить документ:\n\n{error}",
            )
        finally:
            if review is not None:
                review.deleteLater()
            dialog = self.dialog
            if context.get("manual_review_started") and dialog is not None:
                dialog.end_manual_review()
            if dialog is not None:
                dialog.complete_review(result_code)
