from pathlib import Path
from time import monotonic

from PySide6.QtCore import Qt, Signal, QTimer, QEvent
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar,
    QMessageBox, QFileDialog, QFrame, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView,
)

from app.import_workers import (
    OCRWorker, ParserWorker,
    apply_post_document_memory_policy, finalize_ocr_batch_memory,
)
from app.app_paths import get_app_paths


# ============================================================
# IMPORT DIALOG
# ============================================================


class ImportDocumentDialog(QDialog):

    request_ready = Signal(object, str)
    notice = Signal(str, str, str)
    backgrounded = Signal()

    def __init__(self, parent=None, cache_dir=None, preflight_callback=None):
        super().__init__(parent)

        self._review_pending = False
        self._manual_review_active = False
        self._cancel_requested = False
        self._closing = False
        self._backgrounded_by_user = False
        self._paused = False
        self._current_ocr_data = None
        self.cache_dir = Path(cache_dir) if cache_dir else (get_app_paths().data / "parser_cache")
        self.preflight_callback = preflight_callback
        self._preflight_done_index = -1
        self._force_duplicate_current = False
        self._current_started_at = 0.0
        self._failed_paths: list[Path] = []

        self.worker = None
        self.parser_worker = None
        self._continue_after_worker = False
        self._result_processed = False
        self._worker_finished = False

        self.pdf_path = None
        self.pdf_queue = []
        self._current_sha256 = None
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
        self.setFixedSize(760, 620)
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

        self.queue_panel = QFrame()
        self.queue_panel.setObjectName("queuePanel")
        self.queue_panel.setStyleSheet(
            "QFrame#queuePanel { background:white; border:1px solid #E2E8F0; border-radius:14px; }"
        )
        queue_layout = QVBoxLayout(self.queue_panel)
        queue_layout.setContentsMargins(14, 12, 14, 12)
        queue_layout.setSpacing(9)

        queue_header = QHBoxLayout()
        self.queue_summary = QLabel("Очередь документов")
        self.queue_summary.setStyleSheet("font-size:13px; font-weight:700; color:#0F172A;")
        self.pause_button = QPushButton("Пауза после текущего")
        self.pause_button.setFixedHeight(34)
        self.pause_button.setStyleSheet(
            "QPushButton { background:#FFFFFF; color:#344054; border:1px solid #CBD5E1; "
            "border-radius:8px; padding:0 12px; font-size:12px; font-weight:650; } "
            "QPushButton:hover { background:#F8FAFC; border-color:#94A3B8; }"
        )
        self.pause_button.clicked.connect(self.toggle_pause)
        self.cancel_pending_button = QPushButton("Отменить ожидающие")
        self.cancel_pending_button.setFixedHeight(34)
        self.cancel_pending_button.setStyleSheet(self.pause_button.styleSheet())
        self.cancel_pending_button.clicked.connect(self.cancel_pending)
        self.retry_errors_button = QPushButton("Повторить ошибки")
        self.retry_errors_button.setFixedHeight(34)
        self.retry_errors_button.setStyleSheet(self.pause_button.styleSheet())
        self.retry_errors_button.clicked.connect(self.retry_failed)
        self.retry_errors_button.hide()
        self.add_more_button = QPushButton("Добавить ещё PDF")
        self.add_more_button.setFixedHeight(34)
        self.add_more_button.setStyleSheet(self.pause_button.styleSheet())
        self.add_more_button.clicked.connect(self.select_files)
        self.add_more_button.hide()
        queue_header.addWidget(self.queue_summary)
        queue_header.addStretch()
        queue_header.addWidget(self.retry_errors_button)
        queue_header.addWidget(self.add_more_button)
        queue_header.addWidget(self.cancel_pending_button)
        queue_header.addWidget(self.pause_button)
        queue_layout.addLayout(queue_header)

        self.queue_table = QTableWidget()
        self.queue_table.setColumnCount(4)
        self.queue_table.setHorizontalHeaderLabels(["#", "Файл", "Состояние", "Время"])
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.queue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue_table.setAlternatingRowColors(True)
        self.queue_table.setShowGrid(False)
        self.queue_table.setMinimumHeight(250)
        self.queue_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.queue_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.queue_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.queue_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.queue_table.setStyleSheet(
            "QTableWidget { border:1px solid #EEF2F6; border-radius:8px; background:#FFFFFF; "
            "alternate-background-color:#F8FAFC; } "
            "QTableWidget::item { padding:6px 8px; border-bottom:1px solid #F1F5F9; } "
            "QHeaderView::section { background:#F8FAFC; color:#475569; border:none; "
            "border-bottom:1px solid #E2E8F0; padding:7px; font-size:11px; font-weight:700; }"
        )
        queue_layout.addWidget(self.queue_table)

        overall_row = QHBoxLayout()
        overall_label = QLabel("Общий прогресс")
        overall_label.setStyleSheet("color:#64748B; font-size:11px; font-weight:600;")
        self.overall_progress = QProgressBar()
        self.overall_progress.setRange(0, 100)
        self.overall_progress.setValue(0)
        self.overall_progress.setTextVisible(True)
        self.overall_progress.setFixedHeight(20)
        overall_row.addWidget(overall_label)
        overall_row.addWidget(self.overall_progress, 1)
        queue_layout.addLayout(overall_row)
        self.queue_panel.hide()
        root.addWidget(self.queue_panel, 1)

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

        self.setProperty("batch_finished", False)
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
        self._paused = False
        self._review_pending = False
        self._manual_review_active = False
        self._current_ocr_data = None
        self._preflight_done_index = -1
        self._force_duplicate_current = False
        self._current_started_at = 0.0
        self._failed_paths = []
        self._result_processed = False
        self._worker_finished = False
        self._continue_after_worker = False

        self._populate_queue()
        self.drop_area.hide()
        self.queue_panel.show()
        self.pause_button.show()
        self.cancel_pending_button.show()
        self.pause_button.setEnabled(self.total_files > 1)
        self.pause_button.setText("Пауза после текущего")
        self.cancel_pending_button.setEnabled(self.total_files > 1)
        self.retry_errors_button.hide()
        self.add_more_button.hide()
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
        if self._paused:
            self.status_indicator.setStyleSheet("color: #667085; font-size: 14px;")
            self.status_label.setText("Очередь приостановлена. Нажмите «Продолжить».")
            self._update_queue_row(self.current_index, "Пауза")
            return

        self.pdf_path = self.pdf_queue[self.current_index]
        self._current_ocr_data = None
        self._force_duplicate_current = False
        self._current_sha256 = None
        if self._preflight_done_index != self.current_index and callable(self.preflight_callback):
            self._update_queue_row(self.current_index, "Проверка дубликата")
            try:
                res = self.preflight_callback(str(self.pdf_path))
                if isinstance(res, tuple):
                    action = str(res[0] or "continue")
                    sha256 = res[1] if len(res) > 1 else None
                elif isinstance(res, dict):
                    action = str(res.get("action", "continue"))
                    sha256 = res.get("sha256")
                else:
                    action = str(res or "continue")
                    sha256 = None
            except Exception as error:
                action, sha256 = "continue", None
                self.notice.emit("Проверка дубликата", str(error), "warning")
            self._preflight_done_index = self.current_index
            self._current_sha256 = sha256
            if action in {"skip", "open"}:
                self.skipped_count += 1
                self._update_queue_row(self.current_index, "Пропущен: дубликат")
                self._set_queue_elapsed(self.current_index, 0.0)
                self.current_index += 1
                QTimer.singleShot(0, self.start_next_pdf)
                return
            self._force_duplicate_current = action == "force"

        self._current_started_at = monotonic()
        self._update_queue_row(self.current_index, "OCR")
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
        if self._closing or self._cancel_requested or self.pdf_path is None:
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

        self.worker = OCRWorker(
            str(self.pdf_path),
            cache_dir=self.cache_dir,
            sha256=self._current_sha256,
        )
        self.worker.text_ready.connect(self.ocr_finished)
        self.worker.error.connect(self.ocr_error)
        self.worker.progress.connect(self.ocr_progress)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()

    def ocr_progress(self, value: int, message: str):
        if self._closing or self._cancel_requested:
            return
        self.progress.show()
        value = max(0, min(100, value))
        self.progress.setValue(value)
        self._update_overall_progress(value)
        self._update_queue_row(self.current_index, message)
        self.status_label.setText(message)

    def ocr_finished(self, ocr_data):  # Заменили text: str на ocr_data
        if self._closing or self._cancel_requested:
            return
        self._current_ocr_data = ocr_data
        self.progress.setValue(58)
        self._update_overall_progress(58)
        self._update_queue_row(self.current_index, "Извлечение данных")
        self.status_label.setText("OCR завершён.")
        self._result_processed = False
        self._continue_after_worker = False
        self.start_parser(ocr_data)

    def start_parser(self, ocr_data):
        if self._closing or self._cancel_requested:
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
        if self._closing or self._cancel_requested:
            return
        value = max(0, min(100, value))
        self.progress.setValue(value)
        self._update_overall_progress(value)
        self._update_queue_row(self.current_index, message)
        self.status_label.setText(message)

    def parser_finished(self, payload):
        if self._closing or self._cancel_requested:
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
            request._allow_duplicate = bool(self._force_duplicate_current)
            if isinstance(self._current_ocr_data, dict):
                request._ocr_text = self._current_ocr_data.get("text", "") or ""
        except Exception:
            pass
        # OCR blocks can be large. The parser diagnostics/cache already contain
        # provenance, so release the full OCR payload before manual review.
        self._current_ocr_data = None
        self._update_queue_row(self.current_index, "Проверка оператором")
        self.request_ready.emit(request, str(self.pdf_path))

        # The review can now be asynchronous/non-modal. The controller marks
        # begin_manual_review() during signal delivery; only fail-safe when no
        # review handler took ownership of the parsed request.
        if self._review_pending and not self._manual_review_active:
            self.complete_review("error")

    def complete_review(self, result: str):
        """Finish one parsed item after manual review/save.

        The controller must report one of: saved, skipped, error. This explicit
        handshake replaces the old hidden `current_result` mutation contract.
        """
        if not self._review_pending or self._closing:
            return
        self._review_pending = False
        self._manual_review_active = False

        if result == "saved":
            self.success_count += 1
            self.status_indicator.setStyleSheet("color: #12B76A; font-size: 14px;")
            self.status_label.setText("Документ сохранён")
            self._update_queue_row(self.current_index, "✓ Сохранён")
        elif result == "skipped":
            self.skipped_count += 1
            self.status_indicator.setStyleSheet("color: #667085; font-size: 14px;")
            self.status_label.setText("Документ пропущен")
            self._update_queue_row(self.current_index, "Пропущен")
        else:
            self.error_count += 1
            if self.pdf_path is not None:
                self._failed_paths.append(Path(self.pdf_path))
            self.status_indicator.setStyleSheet("color: #F04438; font-size: 14px;")
            self.status_label.setText("Ошибка сохранения")
            self._update_queue_row(self.current_index, "Ошибка сохранения")

        self._set_queue_elapsed(self.current_index, monotonic() - self._current_started_at if self._current_started_at else 0.0)
        self._update_overall_progress(100)
        self.current_index += 1
        self.progress.setValue(100)
        self._result_processed = True
        self._continue_after_worker = True
        self._try_start_next_pdf()

    def parser_error(self, message: str):
        if self._closing or self._cancel_requested:
            return
        self.progress.setValue(100)
        self.error_count += 1
        if self.pdf_path is not None:
            self._failed_paths.append(Path(self.pdf_path))
        self._set_queue_elapsed(self.current_index, monotonic() - self._current_started_at if self._current_started_at else 0.0)
        self.status_indicator.setStyleSheet("color: #F04438; font-size: 14px;")
        self.status_label.setText(f"Ошибка разбора: {self.pdf_path.name}")
        self._update_queue_row(self.current_index, "Ошибка разбора")
        self._update_overall_progress(100)

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
        if self._closing or self._cancel_requested:
            return
        self.progress.hide()
        self.error_count += 1
        if self.pdf_path is not None:
            self._failed_paths.append(Path(self.pdf_path))
        self._set_queue_elapsed(self.current_index, monotonic() - self._current_started_at if self._current_started_at else 0.0)
        self.status_indicator.setStyleSheet("color: #F04438; font-size: 14px;")
        self.status_label.setText(f"Ошибка: {self.pdf_path.name}")
        self._update_queue_row(self.current_index, "Ошибка OCR")
        self._update_overall_progress(100)

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

        # Economy mode samples RAM only at safe document boundaries. This keeps
        # the fast path untouched while allowing the OCR engine to be unloaded
        # before the next document if the process approaches the 1 GB budget.
        memory_decision = apply_post_document_memory_policy()
        if memory_decision.action not in {"normal", "cleanup"}:
            self.status_label.setText(
                f"Экономия памяти: {memory_decision.current_bytes / (1024 * 1024):.0f} МБ. Подготовка следующего документа..."
            )

        if self._paused:
            self.status_indicator.setStyleSheet("color: #667085; font-size: 14px;")
            self.status_label.setText("Очередь приостановлена. Нажмите «Продолжить».")
            if self.current_index < self.total_files:
                self._update_queue_row(self.current_index, "Пауза")
            return
        QTimer.singleShot(0, self.start_next_pdf)

    def _finish_cancel(self):
        self._closing = True
        self._cancel_requested = True
        self.processing = False
        self._continue_after_worker = False
        self.progress.hide()
        self.select_button.setEnabled(False)
        self.pdf_queue.clear()
        self._current_ocr_data = None
        self.current_index = 0
        self.total_files = 0
        QDialog.done(self, QDialog.DialogCode.Rejected)

    def finish_batch(self):
        self.processing = False
        self._paused = False
        self.overall_progress.setValue(100)
        self.pause_button.setEnabled(False)
        self.pause_button.setText("Пауза после текущего")
        self.progress.hide()
        self.select_button.setEnabled(True)
        self.status_indicator.setStyleSheet("color: #12B76A; font-size: 14px;")
        self.status_label.setText(
            f"Готово. Добавлено: {self.success_count}, пропущено: {self.skipped_count}, ошибок: {self.error_count}")
        self.setProperty("batch_finished", True)

        self.cancel_pending_button.setEnabled(False)
        self.pause_button.hide()
        self.cancel_pending_button.hide()
        self.add_more_button.show()
        self.retry_errors_button.setVisible(bool(self._failed_paths))
        # Release the OCR runtime at a safe queue boundary and, in economy
        # mode, trim reclaimable working-set pages once. This prevents long
        # sessions from carrying the peak OCR footprint into idle time.
        finalize_ocr_batch_memory()

        if self.total_files == 1 and not self._failed_paths:
            QDialog.done(self, QDialog.DialogCode.Accepted)
            return

        kind = "warning" if self.error_count else "success"
        self.notice.emit(
            "Массовый импорт завершён",
            f"Добавлено: {self.success_count} · пропущено: {self.skipped_count} · ошибок: {self.error_count}",
            kind,
        )
        self.status_label.setText(
            f"Готово. Добавлено: {self.success_count}, пропущено: {self.skipped_count}, ошибок: {self.error_count}. "
            "Можно повторить ошибки или закрыть окно."
        )


    def _populate_queue(self):
        self.queue_table.setRowCount(self.total_files)
        for index, path in enumerate(self.pdf_queue):
            number = QTableWidgetItem(str(index + 1))
            name = QTableWidgetItem(path.name)
            name.setToolTip(str(path))
            state = QTableWidgetItem("Ожидает")
            elapsed = QTableWidgetItem("—")
            self.queue_table.setItem(index, 0, number)
            self.queue_table.setItem(index, 1, name)
            self.queue_table.setItem(index, 2, state)
            self.queue_table.setItem(index, 3, elapsed)
        self.queue_summary.setText(f"Очередь: {self.total_files} документов")
        self.overall_progress.setValue(0)

    def _update_queue_row(self, index: int, state: str):
        if index < 0 or index >= self.queue_table.rowCount():
            return
        item = self.queue_table.item(index, 2)
        if item is None:
            item = QTableWidgetItem()
            self.queue_table.setItem(index, 2, item)
        text = str(state or "").strip()
        if len(text) > 46:
            text = text[:43] + "..."
        item.setText(text)
        self.queue_table.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)

    def _update_overall_progress(self, current_file_progress: int):
        if self.total_files <= 0:
            self.overall_progress.setValue(0)
            return
        completed = min(self.current_index, self.total_files)
        current = max(0, min(100, int(current_file_progress)))
        value = int(((completed * 100) + current) / self.total_files)
        self.overall_progress.setValue(max(0, min(100, value)))
        self.queue_summary.setText(
            f"Очередь: {min(self.current_index + 1, self.total_files)} из {self.total_files}"
        )

    def _set_queue_elapsed(self, index: int, seconds: float):
        if index < 0 or index >= self.queue_table.rowCount():
            return
        item = self.queue_table.item(index, 3)
        if item is None:
            item = QTableWidgetItem()
            self.queue_table.setItem(index, 3, item)
        item.setText(f"{max(0.0, float(seconds)):.1f} с" if seconds else "—")

    def cancel_pending(self):
        """Cancel only files that have not started; keep the active document safe."""
        if not self.processing:
            return
        keep = min(self.current_index + 1, self.total_files)
        for index in range(keep, self.total_files):
            self._update_queue_row(index, "Отменён до запуска")
        removed = max(0, self.total_files - keep)
        if removed:
            self.skipped_count += removed
            self.pdf_queue = self.pdf_queue[:keep]
            self.total_files = keep
            self.queue_summary.setText(f"Очередь сокращена до {keep} документов")
        self.cancel_pending_button.setEnabled(False)

    def prepare_for_new_batch(self):
        """Return a completed dialog to the normal file-selection state."""
        if self.processing or self.worker is not None or self.parser_worker is not None:
            return
        self.setProperty("batch_finished", False)
        self._backgrounded_by_user = False
        self._paused = False
        self._cancel_requested = False
        self._closing = False
        self._review_pending = False
        self._manual_review_active = False
        self._current_ocr_data = None
        self._preflight_done_index = -1
        self._force_duplicate_current = False
        self._current_started_at = 0.0
        self._result_processed = False
        self._worker_finished = False
        self._continue_after_worker = False
        self.pdf_path = None
        self.pdf_queue = []
        self._failed_paths = []
        self.current_index = 0
        self.total_files = 0
        self.success_count = 0
        self.skipped_count = 0
        self.error_count = 0
        self.queue_table.setRowCount(0)
        self.queue_panel.hide()
        self.drop_area.show()
        self.select_button.setEnabled(True)
        self.retry_errors_button.hide()
        self.add_more_button.hide()
        self.pause_button.show()
        self.cancel_pending_button.show()
        self.progress.hide()
        self.overall_progress.setValue(0)
        self.status_indicator.setStyleSheet("color: #12B76A; font-size: 14px;")
        self.status_label.setText("Готов к загрузке")

    def retry_failed(self):
        if self.processing or not self._failed_paths:
            return
        paths = [path for path in self._failed_paths if path.is_file()]
        if not paths:
            self.retry_errors_button.hide()
            return
        self.setProperty("batch_finished", False)
        self.drop_area.show()
        self.queue_panel.hide()
        self.start_batch(paths)

    def toggle_pause(self):
        if not self.processing or self.total_files <= 1:
            return
        self._paused = not self._paused
        if self._paused:
            self.pause_button.setText("Продолжить")
            if self.worker is not None or self.parser_worker is not None or self._review_pending:
                self.status_label.setText("Пауза включена: остановимся после текущего документа.")
            else:
                self.status_label.setText("Очередь приостановлена. Нажмите «Продолжить».")
                if self.current_index < self.total_files:
                    self._update_queue_row(self.current_index, "Пауза")
        else:
            self.pause_button.setText("Пауза после текущего")
            self.status_label.setText("Продолжаем обработку очереди...")
            if self.current_index < self.total_files:
                self._update_queue_row(self.current_index, "Ожидает запуска")
            if self.worker is None and self.parser_worker is None and not self._review_pending:
                QTimer.singleShot(0, self.start_next_pdf)

    def begin_manual_review(self):
        """Hand the current item to a non-modal review window.

        The import queue stays paused on this item, while the main application
        remains fully usable until the operator saves or rejects the review.
        """
        if self._closing:
            return
        self._manual_review_active = True
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
        self._manual_review_active = False
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

    def stop_workers_and_wait(self, timeout_ms: int = 3000) -> None:
        """Safely request cancellation and wait for background OCR/parser threads."""
        self._closing = True
        self._cancel_requested = True
        self.processing = False
        self.pdf_queue.clear()
        if self.worker is not None and self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.wait(timeout_ms)
        if self.parser_worker is not None and self.parser_worker.isRunning():
            self.parser_worker.requestInterruption()
            self.parser_worker.wait(timeout_ms)

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
