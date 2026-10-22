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
        self.active_dialog: RequestDialog | None = None
        self._review_context: dict | None = None
        self._is_closing: bool = False

    def close_active_dialogs(self) -> None:
        self._is_closing = True
        if self.review_dialog is not None:
            dlg = self.review_dialog
            self.review_dialog = None
            if hasattr(dlg, "force_close"):
                dlg.force_close()
            else:
                dlg.reject()
        if self.active_dialog is not None:
            dlg = self.active_dialog
            self.active_dialog = None
            if hasattr(dlg, "force_close"):
                dlg.force_close()
            else:
                dlg.reject()
        if self.dialog is not None:
            dlg = self.dialog
            self.dialog = None
            if hasattr(dlg, "stop_workers_and_wait"):
                dlg.stop_workers_and_wait(3000)
            else:
                dlg._closing = True
                dlg._cancel_requested = True
                dlg._request_stop_and_close()
            dlg.reject()

    def open(self):
        if self._is_closing or (self.parent_widget is not None and getattr(self.parent_widget, "_is_closing", False)):
            return
        try:
            from app.import_workers import warmup_ocr_engine_async
            warmup_ocr_engine_async()
        except Exception:
            pass

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

        from app.app_paths import get_app_paths
        cache_dir = get_app_paths().data / "parser_cache"
        dialog = ImportDocumentDialog(
            self.parent_widget,
            cache_dir=cache_dir,
            preflight_callback=self._preflight_document,
        )
        self.dialog = dialog
        if self.parent_widget is not None and hasattr(self.parent_widget, "register_dialog"):
            self.parent_widget.register_dialog(dialog)
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
        if self.dialog is not None and self.parent_widget is not None and hasattr(self.parent_widget, "unregister_dialog"):
            self.parent_widget.unregister_dialog(self.dialog)
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

    def _preflight_document(self, pdf_path: str) -> tuple[str, str | None]:
        """Detect duplicates before OCR so repeated PDFs cost almost no RAM/CPU."""
        fp = self.request_service.fingerprints.fingerprint(pdf_path)
        sha256 = fp.sha256 if fp is not None else None
        duplicate = self.request_service.find_duplicate_document(pdf_path)
        if duplicate is None:
            return "continue", sha256
        action = self._choose_duplicate_action(duplicate, initial=True)
        return action, sha256

    def _open_existing_request(self, request) -> None:
        request_to_open = request
        force_dirty_on_reopen = False
        while request_to_open is not None:
            if self._is_closing or (self.parent_widget is not None and getattr(self.parent_widget, "_is_closing", False)):
                return
            try:
                request_id = int(request_to_open.id)
                diagnostics = self.request_service.get_parser_metadata(request_id) if request_id else {}
                dup_info = self.request_service.get_request_duplicate_info(request_id) if request_id else None
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
                    self.request_service.delete_request(request_id)
                    if hasattr(self.parent_widget, "load_requests"):
                        self.parent_widget.load_requests()
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
                            if hasattr(self.parent_widget, "load_requests"):
                                self.parent_widget.load_requests()
                        except Exception as error:
                            logger.exception("Не удалось сохранить запрос перед переключением")
                            QMessageBox.critical(
                                self.parent_widget,
                                "Ошибка сохранения",
                                f"Не удалось сохранить запрос #{request_id}. Вы можете повторить сохранение.\n\n{error}",
                            )
                            request_to_open = updated
                            force_dirty_on_reopen = True
                            continue
                    target = self.request_service.get_request(int(target_id))
                    if target is None:
                        QMessageBox.warning(
                            self.parent_widget,
                            "Запрос не найден",
                            f"Связанный запрос #{target_id} больше не существует.",
                        )
                        request_to_open = self.request_service.get_request(request_id)
                        continue
                    request_to_open = target
                    continue
                if result == QDialog.DialogCode.Accepted:
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
                        if hasattr(self.parent_widget, "load_requests"):
                            self.parent_widget.load_requests()
                        self.toast_requested.emit("Запрос обновлён", f"Запись #{updated.id} сохранена.", "success")
                        return
                    except Exception as error:
                        logger.exception("Ошибка обновления запроса из диалога дубликата")
                        QMessageBox.critical(self.parent_widget, "Ошибка сохранения", f"Не удалось обновить запись:\n\n{error}")
                        request_to_open = updated
                        force_dirty_on_reopen = True
                        continue
                return
            except Exception as error:
                logger.exception("Не удалось открыть существующую запись-дубликат")
                QMessageBox.warning(self.parent_widget, "Дубликат", f"Не удалось открыть запись:\n{error}")
                return

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
        if self._is_closing or (self.parent_widget is not None and getattr(self.parent_widget, "_is_closing", False)):
            logger.info("Пропуск review: приложение закрывается")
            if self.dialog is not None:
                self.dialog.complete_review("skipped")
            return

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
                request_service=self.request_service,
            )
            review.setWindowModality(Qt.WindowModality.NonModal)
            review.setModal(False)
            if self.parent_widget is not None:
                review.setWindowIcon(self.parent_widget.windowIcon())
                if hasattr(self.parent_widget, "register_dialog"):
                    self.parent_widget.register_dialog(review)

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

        if review is not None and self.parent_widget is not None and hasattr(self.parent_widget, "unregister_dialog"):
            self.parent_widget.unregister_dialog(review)

        if self._is_closing or (self.parent_widget is not None and getattr(self.parent_widget, "_is_closing", False)):
            if review is not None:
                review.deleteLater()
            return

        try:
            if review is not None and result == RequestDialog.SWITCH_REQUEST_RESULT:
                if getattr(review, "switch_save_current", False):
                    self._save_reviewed_request(review, context)
                    result_code = "saved"
                target_id = getattr(review, "switch_target_request_id", None)
                if target_id is not None:
                    orig = self.request_service.get_request(int(target_id))
                    if orig is not None:
                        self._open_existing_request(orig)
                return

            if review is None or result != QDialog.DialogCode.Accepted:
                return

            self._save_reviewed_request(review, context)
            result_code = "saved"
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

    def _save_reviewed_request(self, review, context: dict) -> None:
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

        self.toast_requested.emit(
            "Запрос создан", "Документ успешно добавлен в реестр.", "success"
        )
        logger.info("Импортирован документ %s", request.source_document)
