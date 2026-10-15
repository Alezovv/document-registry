from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QCheckBox,
    QCompleter,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QMessageBox,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.ui.date_controls import NoWheelComboBox, SmartDateEdit
from app.parser_cache import ParserCache
from app.pdf_highlight_service import PdfHighlightPreview
from app.parser_review import FIELD_LABELS, REVIEWABLE_FIELDS, field_review_info, needs_review
from app.ui.pdf_viewer_mixin import PdfViewerMixin
from app.ui.request_form_mixin import RequestFormMixin
from app.ui.styles import APP_STYLE, DIALOG_STYLE, STATUSES


class RequestDialog(RequestFormMixin, PdfViewerMixin, QDialog):
    DELETE_RESULT = 2

    def __init__(
        self,
        parent=None,
        request=None,
        documents_dir=None,
        parsed=False,
        suggestions=None,
        parser_diagnostics=None,
        duplicate_info=None,
    ):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self._is_dirty = False
        self.main_window = parent
        self.request = request
        self.documents_dir = Path(documents_dir) if documents_dir else None
        self.selected_pdf = None
        self.detach_pdf_requested = False
        self.parsed = parsed
        self.suggestions = suggestions or {}
        self.parser_diagnostics = parser_diagnostics or {}
        self.duplicate_info = duplicate_info or {}
        self.parser_cache = (
            ParserCache(self.documents_dir.parent / "parser_cache")
            if self.documents_dir is not None
            else None
        )
        self.pdf_highlight_preview = PdfHighlightPreview()
        self._pending_highlight_page = None
        self._highlight_field = None
        self._review_buttons = {}

        self.pdf_document = QPdfDocument(self)
        self.current_pdf_path = None

        self.setStyleSheet(APP_STYLE + DIALOG_STYLE)
        self.setWindowTitle(
            "Новый запрос" if request is None else "Редактирование запроса")

        # Сделали стартовый размер меньше и убрали жесткое ограничение в 760px!
        self.resize(720, 760)
        self.setMinimumSize(500, 600)

        self.setStyleSheet(
            APP_STYLE + DIALOG_STYLE + """
            QDialog { background-color: #F8FAFC; }
            QLabel { color: #344054; }
            QLineEdit, QDateEdit, QComboBox, QTextEdit {
                background-color: #FFFFFF; border: 1px solid #D0D5DD; border-radius: 9px;
                padding: 9px 11px; color: #101828; font-size: 13px; selection-background-color: #DCEBFF;
            }
            QLineEdit:hover, QDateEdit:hover, QComboBox:hover, QTextEdit:hover { border-color: #98A2B3; }
            QLineEdit:focus, QDateEdit:focus, QComboBox:focus, QTextEdit:focus {
                border: 2px solid #84ADFF; padding: 8px 10px; background-color: #FFFFFF;
            }
            QTextEdit { padding: 10px; }
            QPushButton { min-height: 38px; padding: 8px 16px; border-radius: 9px; font-size: 13px; font-weight: 600; }
            QComboBox { padding-right: 30px; }
            QDateEdit { min-height: 20px; }
            """
        )

        self.setup_ui()
        self.finished.connect(lambda _result: self._cleanup_parser_preview())
        if request is not None:
            self.load_request()
            self.highlight_empty_fields()
        self._apply_parser_review_styles()

    def _review_widget(self, field: str):
        return {
            "received_date": getattr(self, "received_date", None),
            "from_department": getattr(self, "from_department", None),
            "from_position": getattr(self, "from_position", None),
            "from_person": getattr(self, "from_person", None),
            "to_person": getattr(self, "to_person", None),
            "request_subject": getattr(self, "request_subject", None),
        }.get(field)

    @staticmethod
    def _confidence_text(value: float) -> str:
        return f"{max(0, min(100, round(float(value or 0.0) * 100)))}%"

    def _build_parser_review_bar(self, layout, parent) -> None:
        diagnostics = self.parser_diagnostics if isinstance(self.parser_diagnostics, dict) else {}
        results = diagnostics.get("field_results") or {}
        if not isinstance(results, dict) or not results:
            return
        bar = QFrame(parent)
        bar.setObjectName("parserReviewBar")
        bar.setStyleSheet(
            "QFrame#parserReviewBar { background:#F8FAFC; border:1px solid #E2E8F0; "
            "border-radius:9px; }"
        )
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(5)
        caption = QLabel("Распознавание:", bar)
        caption.setStyleSheet("color:#64748B; font-size:11px; font-weight:700;")
        outer.addWidget(caption)

        # Confidence badges used to live in one QHBoxLayout. At the dialog's
        # normal width that forced Qt to squeeze every button until long Russian
        # labels were clipped. Keep the bar compact, but wrap badges into a
        # three-column grid and size each button from its actual text.
        badges = QGridLayout()
        badges.setContentsMargins(0, 0, 0, 0)
        badges.setHorizontalSpacing(5)
        badges.setVerticalSpacing(5)
        badge_index = 0
        for field in REVIEWABLE_FIELDS:
            info = field_review_info(diagnostics, field)
            value = str(info.get("value", "") or "").strip()
            confidence = float(info.get("confidence", 0.0) or 0.0)
            if not value and confidence <= 0:
                continue
            decision = str(info.get("decision", "") or "")
            warning = decision in {"review", "suggestion", "low_confidence"}
            button = QPushButton(
                f"{FIELD_LABELS.get(field, field)} {self._confidence_text(confidence)}",
                bar,
            )
            button.setFixedHeight(29)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setMinimumWidth(button.fontMetrics().horizontalAdvance(button.text()) + 24)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if warning:
                button.setStyleSheet(
                    "QPushButton { background:#FFFAEB; color:#B54708; border:1px solid #FEDF89; "
                    "border-radius:7px; padding:0 8px; font-size:10px; font-weight:700; } "
                    "QPushButton:hover { background:#FEF0C7; }"
                )
            else:
                button.setStyleSheet(
                    "QPushButton { background:#F0FDF4; color:#166534; border:1px solid #BBF7D0; "
                    "border-radius:7px; padding:0 8px; font-size:10px; font-weight:700; } "
                    "QPushButton:hover { background:#DCFCE7; }"
                )
            source = str(info.get("source", "") or (diagnostics.get("field_sources") or {}).get(field, ""))
            button.setToolTip(
                f"Уверенность: {self._confidence_text(confidence)}\n"
                f"Решение: {decision or '—'}\nИсточник: {source or '—'}\n"
                "Нажмите для вариантов и перехода к месту в PDF."
            )
            button.clicked.connect(lambda _checked=False, f=field, b=button: self._show_field_review_menu(f, b))
            self._review_buttons[field] = button
            grid_row, grid_col = divmod(badge_index, 3)
            badges.addWidget(button, grid_row, grid_col)
            badge_index += 1

        for column in range(3):
            badges.setColumnStretch(column, 1)
        outer.addLayout(badges)
        layout.addWidget(bar)

    def _set_review_field_value(self, field: str, value: str) -> None:
        widget = self._review_widget(field)
        if widget is None:
            return
        value = str(value or "")
        if field == "received_date":
            parsed = QDate.fromString(value, "dd.MM.yyyy")
            if parsed.isValid():
                widget.setDate(parsed)
        elif isinstance(widget, QTextEdit):
            widget.setPlainText(value)
        elif isinstance(widget, QLineEdit):
            widget.setText(value)
        self._is_dirty = True

    def _show_field_review_menu(self, field: str, button: QPushButton) -> None:
        info = field_review_info(self.parser_diagnostics, field)
        menu = QMenu(button)
        current = str(info.get("value", "") or "").strip()
        if current:
            header = menu.addAction(f"Текущее: {current}")
            header.setEnabled(False)
        alternatives = info.get("alternatives") or []
        if alternatives:
            menu.addSeparator()
            for item in alternatives[:3]:
                if not isinstance(item, dict):
                    continue
                value = str(item.get("value", "") or "").strip()
                if not value:
                    continue
                conf = self._confidence_text(float(item.get("confidence", 0.0) or 0.0))
                source = str(item.get("source", "") or "")
                action = menu.addAction(f"Использовать: {value}  · {conf}")
                action.setToolTip(f"Источник: {source or '—'}")
                action.triggered.connect(lambda _checked=False, f=field, v=value: self._set_review_field_value(f, v))
        regions = info.get("source_blocks") or (self.parser_diagnostics.get("field_regions") or {}).get(field, [])
        if regions:
            menu.addSeparator()
            source_action = menu.addAction("Показать источник в PDF")
            source_action.triggered.connect(lambda _checked=False, f=field: self.focus_parser_source(f))
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _resolve_pdf_path(self) -> Path | None:
        if getattr(self, "selected_pdf", None):
            candidate = Path(self.selected_pdf)
            if candidate.is_file():
                return candidate
        if self.request and self.request.source_document:
            candidate = Path(self.request.source_document)
            if candidate.is_file():
                return candidate
            if self.documents_dir:
                fallback = self.documents_dir / candidate.name
                if fallback.is_file():
                    return fallback
        return None

    def focus_parser_source(self, field: str) -> None:
        pdf_path = self._resolve_pdf_path()
        if pdf_path is None:
            QMessageBox.information(self, "Источник поля", "PDF-файл не найден.")
            return
        info = field_review_info(self.parser_diagnostics, field)
        regions = info.get("source_blocks") or (self.parser_diagnostics.get("field_regions") or {}).get(field, [])
        if not regions:
            QMessageBox.information(self, "Источник поля", "Для этого поля координаты источника не сохранены.")
            return
        self._highlight_field = field
        pages = [int(item.get("page_index", 0) or 0) for item in regions if isinstance(item, dict)]
        self._pending_highlight_page = min(pages) if pages else 0
        self.toggle_pdf_viewer(True, pdf_path)

    def _apply_parser_review_styles(self) -> None:
        if self.request is not None:
            return
        for field in REVIEWABLE_FIELDS:
            widget = self._review_widget(field)
            if widget is None:
                continue
            info = field_review_info(self.parser_diagnostics, field)
            confidence = float(info.get("confidence", 0.0) or 0.0)
            decision = str(info.get("decision", "") or "")
            if confidence <= 0 and not info.get("value"):
                continue
            widget.setToolTip(
                (widget.toolTip() + "\n" if widget.toolTip() else "")
                + f"Parser: {self._confidence_text(confidence)}, {decision or 'без решения'}"
            )
            if needs_review(self.parser_diagnostics, field):
                widget.setStyleSheet("border: 1px solid #F79009; background-color: #FFFAEB;")

    def _cleanup_parser_preview(self):
        try:
            self.pdf_document.close()
        except Exception:
            pass
        try:
            self.pdf_highlight_preview.close()
        except Exception:
            pass

    def _show_original_info(self):
        if not self.duplicate_info:
            return
        orig_id = self.duplicate_info.get("original_id")
        parts = [
            f"Номер запроса: #{orig_id}",
            f"Дата поступления: {self.duplicate_info.get('original_date', '—')}",
            f"Статус: {self.duplicate_info.get('original_status', '—')}",
            f"Отдел: {self.duplicate_info.get('original_department', '—')}",
            f"Должность: {self.duplicate_info.get('original_position', '—')}",
            f"ФИО: {self.duplicate_info.get('original_person', '—')}",
            f"На кого: {self.duplicate_info.get('original_to_person', '—')}",
            f"Суть запроса: {self.duplicate_info.get('original_subject', '—')}",
        ]
        QMessageBox.information(
            self,
            f"Оригинал: Запрос #{orig_id}",
            "\n".join(parts),
        )

    def _install_suggestions(self, field: QLineEdit, key: str) -> None:
        values = [str(value).strip() for value in self.suggestions.get(key, []) if str(value).strip()]
        if not values:
            return
        completer = QCompleter(values, field)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        field.setCompleter(completer)

    def _create_quick_date_field(
        self,
        label_text: str,
        edit: SmartDateEdit,
        parent,
        actions,
        *,
        base_getter,
        allow_clear: bool = False,
    ):
        box = QWidget(parent)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        label = QLabel(label_text, box)
        label.setObjectName("fieldLabel")
        layout.addWidget(label)
        layout.addWidget(edit)

        quick = QHBoxLayout()
        quick.setSpacing(5)
        quick_buttons = []
        for text, days in actions:
            button = QPushButton(text, box)
            button.setFixedHeight(27)
            button.setStyleSheet(
                "QPushButton { background:#F8FAFC; color:#475569; border:1px solid #E2E8F0; "
                "border-radius:7px; padding:2px 9px; font-size:11px; font-weight:600; } "
                "QPushButton:hover { background:#EFF6FF; color:#1D4ED8; border-color:#BFDBFE; }"
            )
            button.clicked.connect(
                lambda _checked=False, offset=days, target=edit: target.set_days_from(base_getter(), offset)
            )
            button.setEnabled(edit.isEnabled())
            quick_buttons.append(button)
            quick.addWidget(button)
        if allow_clear:
            clear = QPushButton("Очистить", box)
            clear.setFixedHeight(27)
            clear.setStyleSheet(
                "QPushButton { background:transparent; color:#667085; border:none; padding:2px 6px; font-size:11px; } "
                "QPushButton:hover { color:#B42318; }"
            )
            clear.clicked.connect(edit.clear_date)
            clear.setEnabled(edit.isEnabled())
            quick_buttons.append(clear)
            quick.addWidget(clear)
        edit._quick_date_buttons = quick_buttons
        quick.addStretch()
        layout.addLayout(quick)
        return box

    def setup_ui(self):
        self.setStyleSheet(APP_STYLE + DIALOG_STYLE)

        # Главный горизонтальный контейнер окна (форма слева, просмотрщик справа)
        if self.layout() is not None:
            root_h_layout = self.layout()
        else:
            root_h_layout = QHBoxLayout(self)
        root_h_layout.setContentsMargins(0, 0, 0, 0)
        root_h_layout.setSpacing(0)
        self.root_h_layout = root_h_layout

        # ============================================================
        # ЛЕВАЯ КОЛОНКА: Форма редактирования
        # ============================================================
        self.left_widget = QWidget(self)
        self.left_widget.setObjectName('leftWidget')
        self.left_widget.setStyleSheet('QWidget#leftWidget { background-color: transparent; }')
        self.left_widget.setMinimumWidth(600)
        main_layout = QVBoxLayout(self.left_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 1. Шапка окна (фиксированная высота)
        header_widget = QWidget(self.left_widget)
        header_widget.setObjectName("dialogHeader")
        header_widget.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        header_layout = QVBoxLayout(header_widget)
        header_layout.setContentsMargins(30, 20, 30, 16)
        header_layout.setSpacing(4)

        # Строка заголовка с бейджем ID
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title_row.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        title = QLabel(
            "Редактирование запроса" if self.request else "Новый запрос",
            header_widget,
        )
        title.setObjectName("dialogTitle")
        title_row.addWidget(title)

        if self.request and getattr(self.request, "id", None):
            id_badge = QLabel(f"#{self.request.id}", header_widget)
            id_badge.setFixedHeight(24)
            id_badge.setStyleSheet("""
                QLabel {
                    background-color: #EFF6FF;
                    color: #1D4ED8;
                    border: 1px solid #BFDBFE;
                    font-size: 12px;
                    font-weight: 700;
                    padding: 0px 10px;
                    border-radius: 6px;
                }
            """)
            id_badge.setToolTip("Уникальный номер записи в базе")
            title_row.addWidget(id_badge)

        if self.duplicate_info and self.duplicate_info.get("is_duplicate"):
            dup_badge = QLabel("⚠️ Дубликат", header_widget)
            dup_badge.setFixedHeight(24)
            dup_badge.setStyleSheet("""
                QLabel {
                    background-color: #FEF3F2;
                    color: #B42318;
                    border: 1px solid #FECDCA;
                    font-size: 11px;
                    font-weight: 700;
                    padding: 0px 8px;
                    border-radius: 6px;
                }
            """)
            orig_id = self.duplicate_info.get("original_id")
            dup_badge.setToolTip(f"Прикреплённый документ совпадает с запросом #{orig_id}")
            title_row.addWidget(dup_badge)

        header_layout.addLayout(title_row)

        subtitle = QLabel(
            "Проверьте и при необходимости измените данные входящего запроса",
            header_widget,
        )
        subtitle.setObjectName("dialogSubtitle")
        header_layout.addWidget(subtitle)

        if self.duplicate_info and self.duplicate_info.get("is_duplicate"):
            orig_id = self.duplicate_info.get("original_id")
            banner = QFrame(header_widget)
            banner.setObjectName("duplicateBanner")
            banner.setStyleSheet("""
                QFrame#duplicateBanner {
                    background-color: #FFF7ED;
                    border: 1px solid #FED7AA;
                    border-radius: 9px;
                }
            """)
            banner_layout = QHBoxLayout(banner)
            banner_layout.setContentsMargins(12, 8, 12, 8)
            banner_layout.setSpacing(10)

            icon_lbl = QLabel("📑", banner)
            icon_lbl.setStyleSheet("font-size: 16px; border: none; background: transparent;")
            banner_layout.addWidget(icon_lbl)

            info_layout = QVBoxLayout()
            info_layout.setSpacing(2)
            top_lbl = QLabel(f"Прикреплённый документ — дубликат (Оригинал: Запрос #{orig_id})", banner)
            top_lbl.setStyleSheet("color: #9A3412; font-weight: 700; font-size: 12px; border: none; background: transparent;")
            info_layout.addWidget(top_lbl)

            desc_parts = []
            if self.duplicate_info.get("original_date"):
                desc_parts.append(f"Дата: {self.duplicate_info['original_date']}")
            if self.duplicate_info.get("original_department"):
                desc_parts.append(f"Отдел: {self.duplicate_info['original_department']}")
            if self.duplicate_info.get("original_person"):
                desc_parts.append(f"ФИО: {self.duplicate_info['original_person']}")
            if self.duplicate_info.get("original_subject"):
                desc_parts.append(f"Суть: «{self.duplicate_info['original_subject']}»")
            sub_lbl = QLabel(" · ".join(desc_parts) if desc_parts else "Файл идентичен первичному документу", banner)
            sub_lbl.setStyleSheet("color: #C2410C; font-size: 11px; border: none; background: transparent;")
            sub_lbl.setWordWrap(True)
            info_layout.addWidget(sub_lbl)
            banner_layout.addLayout(info_layout, 1)

            btn_view = QPushButton(f"Посмотреть оригинал #{orig_id}", banner)
            btn_view.setFixedHeight(28)
            btn_view.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_view.setStyleSheet("""
                QPushButton {
                    background-color: #FFFFFF;
                    color: #C2410C;
                    border: 1px solid #FDBA74;
                    border-radius: 6px;
                    font-size: 11px;
                    font-weight: 600;
                    padding: 0 10px;
                }
                QPushButton:hover {
                    background-color: #FFEDD5;
                    border-color: #FB923C;
                }
            """)
            btn_view.clicked.connect(self._show_original_info)
            banner_layout.addWidget(btn_view)

            header_layout.addWidget(banner)

        # "Распознавание" скрыто в режиме редактирования запроса по требованию
        if not self.request:
            self._build_parser_review_bar(header_layout, header_widget)

        main_layout.addWidget(header_widget)

        # 2. Форма с карточками (ScrollArea)
        self.form_container = QWidget()
        self.form_container.setObjectName('formContainer')
        self.form_container.setStyleSheet('QWidget#formContainer { background-color: transparent; }')
        form_layout = QVBoxLayout(self.form_container)
        form_layout.setContentsMargins(30, 16, 30, 22)
        form_layout.setSpacing(14)

        def create_field(label_text, widget, parent=None):
            box = QWidget(parent)
            layout = QVBoxLayout(box)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(6)
            lbl = QLabel(label_text, box)
            lbl.setObjectName("fieldLabel")
            layout.addWidget(lbl)
            layout.addWidget(widget)
            return box

        # Поля ввода. SmartDateEdit защищает от случайного изменения даты колесом
        # и поддерживает Ctrl+T / Delete + контекстные быстрые даты.
        self.received_date = SmartDateEdit(initial_date=QDate.currentDate())
        self.due_date = SmartDateEdit(optional=True, empty_text="Не указан")
        self.completed_date = SmartDateEdit(optional=True, empty_text="Не исполнен")
        self.completed_date.setEnabled(False)

        self.from_department = QLineEdit()
        self.from_department.setPlaceholderText("Например: Отдел кадров")
        self.from_position = QLineEdit()
        self.from_position.setPlaceholderText("Например: начальник отдела")
        self.from_person = QLineEdit()
        self.from_person.setPlaceholderText("Фамилия Имя Отчество")
        self.to_person = QLineEdit()
        self.to_person.setPlaceholderText("Фамилия Имя Отчество")

        self._install_suggestions(self.from_department, "from_department")
        self._install_suggestions(self.from_position, "from_position")
        self._install_suggestions(self.from_person, "from_person")
        self._install_suggestions(self.to_person, "to_person")

        self.request_subject = QTextEdit()
        self.request_subject.setPlaceholderText(
            "Краткое содержание запроса...")
        self.status = NoWheelComboBox()
        self.status.addItems(STATUSES)
        self.note = QTextEdit()
        self.note.setPlaceholderText("Примечание к запросу...")

        def mark_dirty(widget):
            self._clear_field_warning(widget)
            if getattr(self, "_loaded", False):
                self._is_dirty = True

        self._mark_dirty = mark_dirty

        for w in (
            self.from_department,
            self.from_position,
            self.from_person,
            self.to_person,
            self.status,
            self.received_date,
            self.due_date,
            self.completed_date,
        ):
            w.setMinimumHeight(40)
            if hasattr(w, "textChanged"):
                w.textChanged.connect(lambda _, widget=w: mark_dirty(widget))
            elif hasattr(w, "currentTextChanged"):
                w.currentTextChanged.connect(
                    lambda _, widget=w: mark_dirty(widget)
                )
            elif hasattr(w, "dateChanged"):
                w.dateChanged.connect(lambda _, widget=w: mark_dirty(widget))

        def sync_completed_date(status_text: str):
            completed = status_text == "Исполнен"
            self.completed_date.setEnabled(completed)
            for button in getattr(self.completed_date, "_quick_date_buttons", []):
                button.setEnabled(completed)
            if completed and self.completed_date.date() == self.completed_date.minimumDate():
                self.completed_date.setDate(QDate.currentDate())
            elif not completed:
                self.completed_date.setDate(self.completed_date.minimumDate())

        self.status.currentTextChanged.connect(sync_completed_date)

        # The subject may contain a meaningful multi-sentence paragraph. Show
        # more of it during review while keeping the note field compact.
        self.request_subject.setMinimumHeight(120)
        self.request_subject.setMaximumHeight(190)
        self.note.setMinimumHeight(80)
        self.note.setMaximumHeight(110)
        for w in (self.request_subject, self.note):
            w.textChanged.connect(lambda widget=w: mark_dirty(widget))

        # --- Карточка 1: Регистрация ---
        card1 = QFrame(self.form_container)
        card1.setObjectName("card")
        card1_layout = QVBoxLayout(card1)
        card1_layout.setContentsMargins(20, 18, 20, 18)
        card1_layout.setSpacing(14)
        c1_title = QLabel("Регистрация", card1)
        c1_title.setObjectName("sectionTitle")
        card1_layout.addWidget(c1_title)

        row1 = QHBoxLayout()
        row1.setSpacing(14)
        row1.setAlignment(Qt.AlignmentFlag.AlignTop)
        received_field = self._create_quick_date_field(
            "Дата поступления",
            self.received_date,
            card1,
            (("Сегодня", 0),),
            base_getter=QDate.currentDate,
        )
        status_field = create_field("Статус", self.status, card1)
        row1.addWidget(received_field, 1, Qt.AlignmentFlag.AlignTop)
        row1.addWidget(status_field, 1, Qt.AlignmentFlag.AlignTop)
        card1_layout.addLayout(row1)

        row1_dates = QHBoxLayout()
        row1_dates.setSpacing(14)
        row1_dates.addWidget(
            self._create_quick_date_field(
                "Срок исполнения",
                self.due_date,
                card1,
                (("+7", 7), ("+14", 14), ("+30", 30)),
                base_getter=lambda: self.received_date.date(),
                allow_clear=True,
            ),
            1,
        )
        row1_dates.addWidget(
            self._create_quick_date_field(
                "Дата исполнения",
                self.completed_date,
                card1,
                (("Сегодня", 0),),
                base_getter=QDate.currentDate,
                allow_clear=True,
            ),
            1,
        )
        card1_layout.addLayout(row1_dates)
        form_layout.addWidget(card1)

        # --- Карточка 2: Стороны запроса ---
        card2 = QFrame(self.form_container)
        card2.setObjectName("card")
        card2_layout = QVBoxLayout(card2)
        card2_layout.setContentsMargins(20, 18, 20, 18)
        card2_layout.setSpacing(14)
        c2_title = QLabel("Стороны запроса", card2)
        c2_title.setObjectName("sectionTitle")
        card2_layout.addWidget(c2_title)

        row2_1 = QHBoxLayout()
        row2_1.setSpacing(14)
        row2_1.addWidget(
            create_field("Отдел", self.from_department, card2), 1
        )
        row2_1.addWidget(
            create_field("Должность", self.from_position, card2), 1
        )

        row2_2 = QHBoxLayout()
        row2_2.setSpacing(14)
        row2_2.addWidget(
            create_field("От кого", self.from_person, card2), 1
        )
        row2_2.addWidget(create_field("На кого", self.to_person, card2), 1)

        card2_layout.addLayout(row2_1)
        card2_layout.addLayout(row2_2)
        form_layout.addWidget(card2)

        # --- Карточка 3: Содержание ---
        card3 = QFrame(self.form_container)
        card3.setObjectName("card")
        card3_layout = QVBoxLayout(card3)
        card3_layout.setContentsMargins(20, 18, 20, 18)
        card3_layout.setSpacing(14)
        c3_title = QLabel("Содержание", card3)
        c3_title.setObjectName("sectionTitle")
        card3_layout.addWidget(c3_title)

        card3_layout.addWidget(
            create_field("Суть запроса", self.request_subject, card3)
        )
        card3_layout.addWidget(create_field("Примечание", self.note, card3))

        # Акцентная плашка "Важный запрос"
        pin_banner = QFrame(card3)
        pin_banner.setStyleSheet("""
            QFrame {
                background-color: #FFFBEB;
                border: 1px solid #FDE68A;
                border-radius: 8px;
            }
        """)
        pin_layout = QHBoxLayout(pin_banner)
        pin_layout.setContentsMargins(12, 8, 12, 8)
        pin_layout.setSpacing(10)

        pin_icon = QLabel("📌", pin_banner)
        pin_icon.setStyleSheet(
            "font-size: 14px; border: none; background: transparent;"
        )

        self.pin_checkbox = QCheckBox(
            "Закрепить запрос как приоритетный / важный", pin_banner
        )
        self.pin_checkbox.setChecked(
            bool(self.request and self.request.is_pinned)
        )
        self.pin_checkbox.toggled.connect(
            lambda: self._mark_dirty(self.pin_checkbox)
        )
        self.pin_checkbox.setStyleSheet("""
            QCheckBox {
                font-size: 12px;
                font-weight: 600;
                color: #92400E;
                border: none;
                background: transparent;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 1px solid #CA8A04;
                background: white;
            }
            QCheckBox::indicator:checked {
                background-color: #EAB308;
                border-color: #CA8A04;
            }
        """)

        pin_layout.addWidget(pin_icon)
        pin_layout.addWidget(self.pin_checkbox, 1)
        card3_layout.addWidget(pin_banner)

        form_layout.addWidget(card3)

        # --- Карточка 4: Прикрепленный файл ---
        card4 = QFrame(self.form_container)
        card4.setObjectName("card")
        card4_layout = QVBoxLayout(card4)
        card4_layout.setContentsMargins(20, 18, 20, 18)
        card4_layout.setSpacing(14)
        c4_title = QLabel("Прикрепленный файл", card4)
        c4_title.setObjectName("sectionTitle")
        card4_layout.addWidget(c4_title)

        doc_row = QHBoxLayout()
        doc_row.setSpacing(10)

        self.document_label = QLabel("PDF не прикреплён", card4)
        self.document_label.setObjectName("fieldLabel")
        self.document_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        btn_attach = QPushButton("Прикрепить PDF", card4)
        btn_attach.setObjectName("btnAttach")
        btn_attach.setFixedHeight(36)
        btn_attach.clicked.connect(self.attach_pdf)

        self.btn_detach = QPushButton("Открепить", card4)
        self.btn_detach.setObjectName("btnDetach")
        self.btn_detach.setFixedHeight(36)
        self.btn_detach.clicked.connect(self.detach_pdf)

        self.btn_view = QPushButton("Открыть", card4)
        self.btn_view.setObjectName("btnView")
        self.btn_view.setFixedHeight(36)
        self.btn_view.clicked.connect(self.open_pdf)

        doc_row.addWidget(self.document_label, 1)
        doc_row.addWidget(self.btn_view)
        doc_row.addWidget(btn_attach)
        doc_row.addWidget(self.btn_detach)

        card4_layout.addLayout(doc_row)
        form_layout.addWidget(card4)

        # Скролл-зона карточек
        scroll = QScrollArea(self.left_widget)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form_container)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        main_layout.addWidget(scroll, 1)

        # 3. Подвал окна (кнопки действий)
        footer_widget = QWidget(self.left_widget)
        footer_widget.setObjectName("dialogFooter")
        footer_widget.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        footer_layout = QHBoxLayout(footer_widget)
        footer_layout.setContentsMargins(30, 13, 30, 13)
        footer_layout.setSpacing(12)

        btn_del = QPushButton("Удалить запрос", footer_widget)
        btn_del.setObjectName("btnDanger")
        btn_del.clicked.connect(self.delete_request)

        btn_save = QPushButton("Сохранить", footer_widget)
        btn_save.setObjectName("btnPrimary")
        btn_save.clicked.connect(self.validate_and_accept)

        for btn in (btn_del, btn_save):
            btn.setFixedHeight(40)

        footer_layout.addWidget(btn_del)
        footer_layout.addStretch()
        footer_layout.addWidget(btn_save)

        if self.request is None:
            btn_del.hide()

        main_layout.addWidget(footer_widget)
        root_h_layout.addWidget(self.left_widget, 1)

        # ============================================================
        # ПРАВАЯ КОЛОНКА: Встроенный просмотрщик PDF
        # ============================================================
        self.pdf_container = QFrame(self)
        self.pdf_container.setMinimumWidth(500)
        self.pdf_container.setStyleSheet("""
            QFrame {
                background-color: #F4F7FB;
                border-left: 1px solid #E2E8F0;
            }
        """)

        pdf_box = QVBoxLayout(self.pdf_container)
        pdf_box.setContentsMargins(16, 16, 16, 16)
        pdf_box.setSpacing(10)

        pdf_top_bar = QHBoxLayout()
        pdf_top_bar.setSpacing(6)

        self.pdf_preview_title = QLabel("Документ", self.pdf_container)
        self.pdf_preview_title.setStyleSheet(
            "font-weight: 700; color: #0F172A; font-size: 13px;"
        )

        btn_style = """
            QPushButton {
                background: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                color: #334155;
                font-size: 13px;
                font-weight: 700;
                padding: 0px;
                text-align: center;
            }
            QPushButton:hover { background: #F1F5F9; border-color: #94A3B8; }
            QPushButton:pressed { background: #E2E8F0; }
        """

        # Пагинация
        self.btn_prev_page = QPushButton("<", self.pdf_container)
        self.btn_prev_page.setFixedSize(26, 28)
        self.btn_prev_page.setStyleSheet(btn_style)
        self.btn_prev_page.setToolTip("Предыдущая страница")
        self.btn_prev_page.clicked.connect(self.prev_pdf_page)

        self.pdf_page_label = QLabel("1 / 1", self.pdf_container)
        self.pdf_page_label.setFixedWidth(56)
        self.pdf_page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pdf_page_label.setStyleSheet(
            "color: #475569; font-size: 11px; font-weight: 600;"
        )

        self.btn_next_page = QPushButton(">", self.pdf_container)
        self.btn_next_page.setFixedSize(26, 28)
        self.btn_next_page.setStyleSheet(btn_style)
        self.btn_next_page.setToolTip("Следующая страница")
        self.btn_next_page.clicked.connect(self.next_pdf_page)

        sep = QFrame(self.pdf_container)
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: #CBD5E1; margin: 4px 2px;")

        # Зум-контролы
        btn_zoom_out = QPushButton("-", self.pdf_container)
        btn_zoom_out.setFixedSize(30, 30)
        btn_zoom_out.setStyleSheet(btn_style)
        btn_zoom_out.setToolTip("Уменьшить")
        btn_zoom_out.clicked.connect(self.zoom_out_pdf)

        self.zoom_label = QLabel("Шир.", self.pdf_container)
        self.zoom_label.setFixedWidth(42)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_label.setStyleSheet(
            "color: #475569; font-size: 11px; font-weight: 600;"
        )

        btn_zoom_in = QPushButton("+", self.pdf_container)
        btn_zoom_in.setFixedSize(30, 30)
        btn_zoom_in.setStyleSheet(btn_style)
        btn_zoom_in.setToolTip("Увеличить")
        btn_zoom_in.clicked.connect(self.zoom_in_pdf)

        btn_fit_w = QPushButton("↔", self.pdf_container)
        btn_fit_w.setFixedSize(30, 30)
        btn_fit_w.setStyleSheet(btn_style)
        btn_fit_w.setToolTip("По ширине окна")
        btn_fit_w.clicked.connect(self.zoom_fit_width)

        btn_fit_page = QPushButton("⊡", self.pdf_container)
        btn_fit_page.setFixedSize(30, 30)
        btn_fit_page.setStyleSheet(btn_style)
        btn_fit_page.setToolTip("Вся страница целиком")
        btn_fit_page.clicked.connect(self.zoom_fit_page)

        btn_ext = QPushButton("↗", self.pdf_container)
        btn_ext.setFixedSize(30, 30)
        btn_ext.setStyleSheet(btn_style)
        btn_ext.setToolTip("Открыть во внешней программе")
        btn_ext.clicked.connect(self.open_external_pdf)

        btn_close_pdf = QPushButton("×", self.pdf_container)
        btn_close_pdf.setFixedSize(30, 30)
        btn_close_pdf.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
                border-radius: 6px;
                color: #64748B;
                font-weight: 700;
                font-size: 18px;
                padding: 0px;
                text-align: center;
            }
            QPushButton:hover { background: #E2E8F0; color: #0F172A; }
        """)
        btn_close_pdf.clicked.connect(lambda: self.toggle_pdf_viewer(False))

        # Сборка тулбара просмотрщика в правильном порядке
        pdf_top_bar.addWidget(self.pdf_preview_title)
        pdf_top_bar.addStretch()
        pdf_top_bar.addWidget(self.btn_prev_page)
        pdf_top_bar.addWidget(self.pdf_page_label)
        pdf_top_bar.addWidget(self.btn_next_page)
        pdf_top_bar.addWidget(sep)
        pdf_top_bar.addWidget(btn_zoom_out)
        pdf_top_bar.addWidget(self.zoom_label)
        pdf_top_bar.addWidget(btn_zoom_in)
        pdf_top_bar.addWidget(btn_fit_w)
        pdf_top_bar.addWidget(btn_fit_page)
        pdf_top_bar.addWidget(btn_ext)
        pdf_top_bar.addWidget(btn_close_pdf)
        pdf_box.addLayout(pdf_top_bar)

        # Виджет рендера страниц PDF
        self.pdf_view = QPdfView(self.pdf_container)
        self.pdf_view.setDocument(self.pdf_document)
        self.pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
        self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.pdf_view.setStyleSheet(
            "QPdfView { background-color: #64748B; border-radius: 8px; border: none; }"
        )

        # Сигналы пагинации и обновления страниц
        self.pdf_view.pageNavigator().currentPageChanged.connect(
            self._on_pdf_page_changed
        )
        self.pdf_document.statusChanged.connect(self._on_pdf_status_changed)

        self.pdf_view.installEventFilter(self)
        self.pdf_view.viewport().installEventFilter(self)

        pdf_box.addWidget(self.pdf_view, 1)

        self.pdf_container.hide()
        root_h_layout.addWidget(self.pdf_container, 1)

        self._update_pdf_actions_state()

        # Активируем отслеживание изменений для нового запроса
        if self.request is None:
            self._loaded = True
