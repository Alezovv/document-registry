from pathlib import Path

from PySide6.QtCore import Qt, Signal, QTimer, QEvent
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar,
    QMessageBox, QFileDialog, QFrame,
)

from app.import_workers import OCRWorker, ParserWorker
from app.app_paths import get_app_paths


# ============================================================
# IMPORT DIALOG
# ============================================================


class ImportDocumentDialog(QDialog):

    request_ready = Signal(object, str)
    notice = Signal(str, str, str)
    backgrounded = Signal()

    def __init__(self, parent=None, cache_dir=None):
        super().__init__(parent)

        self._review_pending = False
        self._cancel_requested = False
        self._closing = False
        self._backgrounded_by_user = False
        self.cache_dir = Path(cache_dir) if cache_dir else (get_app_paths().data / "parser_cache")

        self.worker = None
        self.parser_worker = None
        self._continue_after_worker = False
        self._result_processed = False
        self._worker_finished = False

        self.pdf_path = None
        self.pdf_queue = []
        self.current_index = 0
        self.total_files = 0
        self.success_count = 0
        self.skipped_count = 0
        self.error_count = 0
        self.processing = False

        self.setup_ui()

    def setup_ui(self):
        self.setWindowTitle("Добавление документов")
        # Keep only the native minimize and close actions.  The maximize button
        # is intentionally absent: this fixed-size workflow should either be
        # visible, hidden in the background, or explicitly cancelled.
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setFixedSize(620, 500)
        self.setAcceptDrops(True)
        self.setStyleSheet(
            """
            QDialog { background: #F4F7FB; }
            QLabel { color: #0F172A; }
            QPushButton {
                border: none;
                border-radius: 9px;
                padding: 10px 18px;
                font-size: 14px;
            }
            QPushButton:disabled { color: #98A2B3; }
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 24)
        root.setSpacing(18)

        header = QHBoxLayout()
        title_layout = QVBoxLayout()
        title_layout.setSpacing(3)
        title = QLabel("Добавление документов")
        title.setStyleSheet("font-size: 24px; font-weight: 750; color:#0F172A;")
        subtitle = QLabel("Выберите один или несколько PDF для распознавания")
        subtitle.setStyleSheet("font-size: 13px; color: #64748B;")
        title_layout.addWidget(title)
        title_layout.addWidget(subtitle)
        header.addLayout(title_layout)
        header.addStretch()
        root.addLayout(header)

        self.drop_area = QFrame()
        self.drop_area.setObjectName("dropArea")
        self.drop_area.setMinimumHeight(270)
        self.drop_area.setStyleSheet(
            """
            QFrame#dropArea { background: white; border: 2px dashed #CBD5E1; border-radius: 16px; }
            QFrame#dropArea:hover { border: 2px dashed #60A5FA; background: #F8FBFF; }
            """
        )

        drop_layout = QVBoxLayout(self.drop_area)
        drop_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_layout.setSpacing(12)

        icon = QLabel("📄")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size: 42px; background:#EFF6FF; border:1px solid #DBEAFE; border-radius:18px; padding:8px;")
        drop_layout.addWidget(icon)

        drop_title = QLabel("Перетащите PDF сюда")
        drop_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_title.setStyleSheet("font-size: 16px; font-weight: 700; color:#1E293B;")
        drop_layout.addWidget(drop_title)

        drop_subtitle = QLabel("или выберите один или несколько файлов")
        drop_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_subtitle.setStyleSheet("font-size: 13px; color: #64748B;")
        drop_layout.addWidget(drop_subtitle)

        self.select_button = QPushButton("Выбрать PDF")
        self.select_button.setFixedHeight(42)
        self.select_button.setStyleSheet(
            """
            QPushButton { background: #2563EB; color: white; border:1px solid #2563EB; border-radius:9px; font-weight: 700; padding: 0 24px; }
            QPushButton:hover { background: #1D4ED8; border-color:#1D4ED8; }
            QPushButton:pressed { background: #1E40AF; }
            """
        )
        self.select_button.clicked.connect(self.select_files)
        drop_layout.addWidget(self.select_button,
                              alignment=Qt.AlignmentFlag.AlignCenter)

        root.addWidget(self.drop_area)

        status_layout = QHBoxLayout()
        self.status_indicator = QLabel("●")
        self.status_indicator.setStyleSheet("color: #12B76A; font-size: 14px;")
        self.status_label = QLabel("Готов к загрузке")
        self.status_label.setStyleSheet("color: #475569; font-size: 12px; font-weight:600;")
        status_layout.addWidget(self.status_indicator)
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        root.addLayout(status_layout)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet(
            """
            QProgressBar { background: #E2E8F0; border: none; border-radius: 3px; }
            QProgressBar::chunk { background: #2563EB; border-radius: 3px; }
            """
        )
        self.progress.hide()
        root.addWidget(self.progress)

        # Backward-compatible internal cancel action.  It is deliberately not
        # added to any layout and stays hidden: users cancel via the native
        # title-bar close button, while older regression tests and integrations
        # can still call cancel_import() safely.
        self.cancel_button = QPushButton(self)
        self.cancel_button.setText("Отмена")
        self.cancel_button.hide()
        self.cancel_button.clicked.connect(self.cancel_import)
        self.cancel_button.setEnabled(True)


    def select_files(self):
        if self._closing or self.processing:
            return
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, "Выберите PDF", "", "PDF (*.pdf)")
        if file_paths:
            self.start_batch([Path(p) for p in file_paths])

    def start_batch(self, paths):
        if self._closing or self.processing:
            return

        valid_paths = [p for p in paths if p.exists(
        ) and p.is_file() and p.suffix.lower() == ".pdf"]

        if not valid_paths:
            QMessageBox.warning(
                self, "Нет PDF", "Не удалось найти подходящие файлы.")
            return

        self.pdf_queue = valid_paths
        self.current_index = 0
        self.total_files = len(valid_paths)
        self.success_count = 0
        self.skipped_count = 0
        self.error_count = 0
        self.processing = True
        self._cancel_requested = False
        self._closing = False
        self._backgrounded_by_user = False

        self.select_button.setEnabled(False)
        self.progress.show()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        self.start_next_pdf()

    def handle_pdf(self, path: Path):
        if self._closing or self.processing:
            return
        if path.suffix.lower() != ".pdf":
            QMessageBox.warning(self, "Ошибка", "Можно загрузить только PDF.")
            return
        self.start_batch([path])

    def start_next_pdf(self):
        if self._closing:
            return
        if self.worker is not None and self.worker.isRunning():
            return
        if self.parser_worker is not None and self.parser_worker.isRunning():
            return

        if self.current_index >= self.total_files:
            self.finish_batch()
            return

        self.pdf_path = self.pdf_queue[self.current_index]
        self.status_indicator.setStyleSheet("color: #F79009; font-size: 14px;")
        self.status_label.setText(
            f"Распознавание {self.current_index + 1} из {self.total_files}: {self.pdf_path.name}")

        self.progress.show()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.start_ocr()

    def dragEnterEvent(self, event: QDragEnterEvent):
        if self._closing:
            return event.ignore()
        if not event.mimeData().hasUrls():
            return event.ignore()

        for url in event.mimeData().urls():
            if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() == ".pdf":
                return event.acceptProposedAction()
        event.ignore()

    def dropEvent(self, event: QDropEvent):
        if self._closing or self.processing:
            return event.ignore()
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls(
        ) if u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() == ".pdf"]
        if paths:
            event.acceptProposedAction()
            self.start_batch(paths)
        else:
            event.ignore()

    def start_ocr(self):
        if self._closing or self.pdf_path is None:
            return
        if self.worker is not None:
            if self.worker.isRunning():
                return
            self.worker.deleteLater()
            self.worker = None

        self._worker_finished = False
        self._result_processed = False
        self._continue_after_worker = False

        self.status_indicator.setStyleSheet("color: #F79009; font-size: 14px;")
        self.progress.show()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        self.worker = OCRWorker(str(self.pdf_path), cache_dir=self.cache_dir)
        self.worker.text_ready.connect(self.ocr_finished)
        self.worker.error.connect(self.ocr_error)
        self.worker.progress.connect(self.ocr_progress)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()

    def ocr_progress(self, value: int, message: str):
        if self._closing:
            return
        self.progress.show()
        self.progress.setValue(max(0, min(100, value)))
        self.status_label.setText(message)

    def ocr_finished(self, ocr_data):  # Заменили text: str на ocr_data
        if self._closing:
            return
        self.progress.setValue(58)
        self.status_label.setText("OCR завершён.")
        self._result_processed = False
        self._continue_after_worker = False
        self.start_parser(ocr_data)

    def start_parser(self, ocr_data):
        if self._closing:
            return
        if self.parser_worker is not None:
            if self.parser_worker.isRunning():
                return
            self.parser_worker.deleteLater()
            self.parser_worker = None

        self.progress.show()
        self.progress.setValue(60)
        self.status_label.setText("Извлечение данных из OCR...")

        # ИЗМЕНЕНО: Достаем имя файла из пути и передаем в воркер
        file_name = self.pdf_path.name if self.pdf_path else ""
        self.parser_worker = ParserWorker(
            ocr_data, file_name, pdf_path=self.pdf_path, cache_dir=self.cache_dir
        )

        self.parser_worker.result_ready.connect(self.parser_finished)
        self.parser_worker.error.connect(self.parser_error)
        self.parser_worker.progress.connect(self.parser_progress)
        self.parser_worker.finished.connect(self.parser_worker_finished)
        self.parser_worker.start()

    def parser_progress(self, value: int, message: str):
        if self._closing:
            return
        self.progress.setValue(max(0, min(100, value)))
        self.status_label.setText(message)

    def parser_finished(self, payload):
        if self._closing:
            return
        self.progress.setValue(90)
        self.status_label.setText("Проверка данных...")
        self._review_pending = True

        if isinstance(payload, dict) and "request" in payload:
            request = payload.get("request")
            diagnostics = payload.get("diagnostics") or {}
        else:
            request = payload
            diagnostics = {}
        try:
            request._parser_diagnostics = diagnostics
        except Exception:
            pass
        self.request_ready.emit(request, str(self.pdf_path))

        # request_ready normally has a direct UI-thread handler. If no handler
        # completed the review, fail safely instead of leaving the batch stuck.
        if self._review_pending:
            self.complete_review("error")

    def complete_review(self, result: str):
        """Finish one parsed item after manual review/save.

        The controller must report one of: saved, skipped, error. This explicit
        handshake replaces the old hidden `current_result` mutation contract.
        """
        if not self._review_pending or self._closing:
            return
        self._review_pending = False

        if result == "saved":
            self.success_count += 1
            self.status_indicator.setStyleSheet("color: #12B76A; font-size: 14px;")
            self.status_label.setText("Документ сохранён")
        elif result == "skipped":
            self.skipped_count += 1
            self.status_indicator.setStyleSheet("color: #667085; font-size: 14px;")
            self.status_label.setText("Документ пропущен")
        else:
            self.error_count += 1
            self.status_indicator.setStyleSheet("color: #F04438; font-size: 14px;")
            self.status_label.setText("Ошибка сохранения")

        self.current_index += 1
        self.progress.setValue(100)
        self._result_processed = True
        self._continue_after_worker = True
        self._try_start_next_pdf()

    def parser_error(self, message: str):
        if self._closing:
            return
        self.progress.setValue(100)
        self.error_count += 1
        self.status_indicator.setStyleSheet("color: #F04438; font-size: 14px;")
        self.status_label.setText(f"Ошибка разбора: {self.pdf_path.name}")

        self.notice.emit(
            "Не удалось извлечь данные",
            f"{self.pdf_path.name}: {message}",
            "warning",
        )

        self.current_index += 1
        self._result_processed = True
        self._continue_after_worker = True
        self._try_start_next_pdf()

    def parser_worker_finished(self):
        if self.parser_worker is None:
            return
        self.parser_worker.deleteLater()
        self.parser_worker = None

        if self._cancel_requested:
            self._maybe_finish_stop()
            return
        self._try_start_next_pdf()

    def ocr_error(self, message: str):
        if self._closing:
            return
        self.progress.hide()
        self.error_count += 1
        self.status_indicator.setStyleSheet("color: #F04438; font-size: 14px;")
        self.status_label.setText(f"Ошибка: {self.pdf_path.name}")

        self.notice.emit(
            "Ошибка OCR",
            f"{self.pdf_path.name}: {message}",
            "error",
        )

        self.current_index += 1
        self._result_processed = True
        self._continue_after_worker = True
        self._try_start_next_pdf()

    def worker_finished(self):
        if self.worker is None:
            return
        self._worker_finished = True
        self.worker.deleteLater()
        self.worker = None

        if self._cancel_requested:
            self._maybe_finish_stop()
            return
        self._try_start_next_pdf()

    def _try_start_next_pdf(self):
        if self._closing or self.parser_worker is not None:
            return
        if self.worker is not None and self.worker.isRunning():
            return
        if not self._result_processed or not self._worker_finished:
            return

        self._result_processed = False
        self._worker_finished = False
        self._continue_after_worker = False

        QTimer.singleShot(0, self.start_next_pdf)

    def _finish_cancel(self):
        self._closing = True
        self._cancel_requested = True
        self.processing = False
        self._continue_after_worker = False
        self.progress.hide()
        self.select_button.setEnabled(False)
        self.pdf_queue.clear()
        self.current_index = 0
        self.total_files = 0
        QDialog.done(self, QDialog.DialogCode.Rejected)

    def finish_batch(self):
        self.processing = False
        self.progress.hide()
        self.select_button.setEnabled(True)
        self.status_indicator.setStyleSheet("color: #12B76A; font-size: 14px;")
        self.status_label.setText(
            f"Готово. Добавлено: {self.success_count}, пропущено: {self.skipped_count}, ошибок: {self.error_count}")
        self.setProperty("batch_finished", True)

        if self.total_files == 1:
            QDialog.done(self, QDialog.DialogCode.Accepted)
            return

        kind = "warning" if self.error_count else "success"
        self.notice.emit(
            "Массовый импорт завершён",
            f"Добавлено: {self.success_count} · пропущено: {self.skipped_count} · ошибок: {self.error_count}",
            kind,
        )
        QDialog.done(self, QDialog.DialogCode.Accepted)


    def begin_manual_review(self):
        """Hide batch progress while the user reviews one parsed request."""
        if self._closing:
            return
        self.hide()

    def end_manual_review(self):
        """Show progress again only when another queued file still remains."""
        if self._closing or self.property("batch_finished"):
            return
        has_next_file = (self.current_index + 1) < self.total_files
        if not has_next_file or self._backgrounded_by_user:
            return
        self.show()
        self.raise_()
        self.activateWindow()

    def send_to_background(self, hide: bool = False):
        """Keep the current queue alive while the import window is in background."""
        if self._closing:
            return
        self._backgrounded_by_user = True
        if hide:
            self.hide()
        else:
            self.showMinimized()
        if self.processing or self.worker is not None or self.parser_worker is not None:
            self.backgrounded.emit()

    def restore_from_background(self):
        if self._closing:
            return
        self._backgrounded_by_user = False
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _has_active_work(self) -> bool:
        return bool(
            self.processing
            or self.worker is not None
            or self.parser_worker is not None
            or self._review_pending
        )

    def _request_stop_and_close(self):
        """Stop workers, discard the remaining queue, then close safely.

        This is intentionally bound to the title-bar close action.  The native
        minimize action keeps the queue alive in background instead.
        """
        if self._cancel_requested:
            return

        self._cancel_requested = True
        self._closing = True
        self.processing = False
        self._continue_after_worker = False
        self._review_pending = False
        self.pdf_queue.clear()
        self.current_index = 0
        self.total_files = 0
        self.progress.hide()
        self.select_button.setEnabled(False)
        self.status_indicator.setStyleSheet("color: #F79009; font-size: 14px;")
        self.status_label.setText("Остановка обработки...")

        # From the user's perspective the window closes immediately.  The
        # dialog object stays alive until its QThreads finish cleanly, so Qt is
        # never asked to destroy a running worker.
        self.hide()

        if self.worker is not None and self.worker.isRunning():
            self.worker.requestInterruption()
        if self.parser_worker is not None and self.parser_worker.isRunning():
            self.parser_worker.requestInterruption()

        self._maybe_finish_stop()

    def _maybe_finish_stop(self):
        # Do not destroy a dialog that still owns a running QThread.  The
        # corresponding finished handlers clear worker references and call us
        # again.
        if not self._cancel_requested:
            return
        if self.worker is not None or self.parser_worker is not None:
            return
        self._finish_cancel()

    def changeEvent(self, event):
        super().changeEvent(event)
        if self._closing:
            return
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            # Native "-" means "hide this window, keep the same import session".
            # This applies even before processing starts, so pressing the main
            # "Прикрепить документ" action can always restore the same dialog.
            if not self._backgrounded_by_user:
                QTimer.singleShot(0, lambda: self.send_to_background(hide=True))

    def cancel_import(self):
        if self._cancel_requested:
            return

        # With no selected PDF there is nothing to stop: close immediately.
        if (
            not self.processing
            and self.worker is None
            and self.parser_worker is None
            and not self._review_pending
        ):
            self._finish_cancel()
            return

        self._cancel_requested = True
        self.processing = False
        self._continue_after_worker = False
        self.select_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Отмена...")
        self.status_indicator.setStyleSheet("color: #F79009; font-size: 14px;")
        self.status_label.setText("Остановка обработки...")
        self.progress.show()

        if self.worker is not None and self.worker.isRunning():
            self.worker.requestInterruption()
        if self.parser_worker is not None and self.parser_worker.isRunning():
            self.parser_worker.requestInterruption()
        if self.worker is None and self.parser_worker is None:
            self._finish_cancel()

    def closeEvent(self, event):
        if self._has_active_work():
            event.ignore()
            self._request_stop_and_close()
            return
        event.accept()

    def reject(self):
        # Esc and the title-bar close action have the same destructive meaning:
        # stop processing and clear the pending queue.
        if self._has_active_work():
            self._request_stop_and_close()
            return
        QDialog.done(self, QDialog.DialogCode.Rejected)

    def accept(self):
        QDialog.done(self, QDialog.DialogCode.Accepted)
