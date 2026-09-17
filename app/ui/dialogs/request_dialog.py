from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDateEdit,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.ui.date_controls import NoWheelComboBox, SmartDateEdit
from app.parser_cache import ParserCache
from app.pdf_highlight_service import PdfHighlightPreview
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
        self.parser_cache = (
            ParserCache(self.documents_dir.parent / "parser_cache")
            if self.documents_dir is not None
            else None
        )
        self.pdf_highlight_preview = PdfHighlightPreview()
        self._pending_highlight_page = None

        self.pdf_document = QPdfDocument(self)
        self.current_pdf_path = None

        self.setStyleSheet(APP_STYLE + DIALOG_STYLE)
        self.setWindowTitle(
            "Новый запрос" if request is None else "Редактирование запроса")

        # Сделали стартовый размер меньше и убрали жесткое ограничение в 760px!
        self.resize(720, 760)
        self.setMinimumSize(500, 600)

        self.setStyleSheet(
            APP_STYLE + """
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

    def _cleanup_parser_preview(self):
        try:
            self.pdf_document.close()
        except Exception:
            pass
        try:
            self.pdf_highlight_preview.close()
        except Exception:
            pass

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

        header_layout.addLayout(title_row)

        subtitle = QLabel(
            "Проверьте и при необходимости измените данные входящего запроса",
            header_widget,
        )
        subtitle.setObjectName("dialogSubtitle")
        header_layout.addWidget(subtitle)

        main_layout.addWidget(header_widget)

        # 2. Форма с карточками (ScrollArea)
        self.form_container = QWidget()
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
