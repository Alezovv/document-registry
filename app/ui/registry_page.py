from __future__ import annotations

from collections import Counter

from PySide6.QtCore import QDate, QEvent, QSignalBlocker, QTimer, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QMenu,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui.date_controls import NoWheelComboBox, SmartDateEdit
from app.resource_profile import resource_profile_from_environment
from app.ui.delegates import StatusDelegate
from app.ui.styles import (
    INFO_COLOR,
    PRIMARY_COLOR,
    PRIMARY_HOVER,
    SEARCH_FIELDS,
    STATUSES,
    SUCCESS_COLOR,
    TEXT_COLOR,
    WARNING_COLOR,
)
from app.ui.widgets import create_card, create_stat_card


REGISTRY_MIN_DATE = (1950, 1, 1)


class RegistryPage(QWidget):
    create_requested = Signal()
    edit_requested = Signal()
    export_requested = Signal(object)
    bulk_action_requested = Signal(str, object)
    document_requested = Signal(object)

    def __init__(self, request_service, parent=None):
        super().__init__(parent)
        self.request_service = request_service
        self.requests = []
        self._filter_cache: dict[int, dict[str, object]] = {}
        self._status_counts: Counter[str] = Counter()
        self.sort_column = None
        self.sort_ascending = True
        self._last_filtered = []
        self._search_snippets: dict[int, str] = {}
        profile = resource_profile_from_environment()
        self._page_size = 150 if profile.economy_memory_policy else 400
        self._page_offset = 0
        self._total_matching = 0
        self._manual_requests_mode = False
        self._duplicate_map_cache: dict[int, dict] | None = None
        self._registry_counts_cache: dict[str, int] | None = None
        self._registry_counts_cache_day = ""

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(300)
        self.search_timer.timeout.connect(self.apply_filters)

        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.addLayout(self._build_header())
        root.addLayout(self._build_statistics())
        root.addWidget(self._build_search_card())
        root.addWidget(self._build_table_card(), 1)

    def _build_header(self):
        header = QHBoxLayout()
        header.setSpacing(12)

        title_layout = QVBoxLayout()
        title_layout.setSpacing(2)
        title = QLabel("Реестр запросов")
        title.setStyleSheet(
            "font-size: 22px; font-weight: 750; color: #0F172A; letter-spacing: -0.3px;"
        )
        description = QLabel("Управление входящими запросами")
        description.setStyleSheet(
            "color: #64748B; font-size: 12px; font-weight: 500;"
        )
        title_layout.addWidget(title)
        title_layout.addWidget(description)

        title_container = QWidget()
        title_container.setObjectName('titleContainer')
        title_container.setStyleSheet('QWidget#titleContainer { background-color: transparent; }')
        title_container.setLayout(title_layout)
        title_container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        header.addWidget(title_container, 1)

        add_button = QPushButton("+  Новый запрос")
        add_button.setMinimumWidth(145)
        add_button.setMaximumWidth(180)
        add_button.setMinimumHeight(38)
        add_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        add_button.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {PRIMARY_COLOR}; color: white; border: 1px solid {PRIMARY_COLOR};
                border-radius: 9px; padding: 8px 16px; font-size: 13px; font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {PRIMARY_HOVER}; border-color: {PRIMARY_HOVER};
            }}
            QPushButton:pressed {{
                background-color: #1E40AF; border-color: #1E40AF;
            }}
            """
        )
        add_button.clicked.connect(self.create_requested.emit)
        header.addWidget(add_button)

        export_button = QPushButton("Экспорт Excel")
        export_button.setMinimumWidth(130)
        export_button.setMinimumHeight(38)
        export_button.setStyleSheet(
            "QPushButton { background: #FFFFFF; color: #334155; border: 1px solid #CBD5E1; "
            "border-radius: 9px; font-size: 13px; font-weight: 600; padding: 8px 16px; } "
            "QPushButton:hover { background: #F8FAFC; border-color: #94A3B8; color:#1D4ED8; }"
        )
        export_button.clicked.connect(
            lambda: self.export_requested.emit(self.get_filter_snapshot_fetcher())
        )


        header.addWidget(export_button)

        return header

    def _build_statistics(self):
        stats = QHBoxLayout()
        stats.setSpacing(10)

        self.total_card, self.total_value = create_stat_card("Всего запросов", 0, TEXT_COLOR)
        self.new_card, self.new_value = create_stat_card("Новые", 0, INFO_COLOR)
        self.progress_card, self.progress_value = create_stat_card("В работе", 0, WARNING_COLOR)
        self.done_card, self.done_value = create_stat_card("Исполнено", 0, SUCCESS_COLOR)
        self.overdue_card, self.overdue_value = create_stat_card("Просрочено", 0, "#D92D20")

        for card, status in (
            (self.total_card, None),
            (self.new_card, "Новый"),
            (self.progress_card, "В работе"),
            (self.done_card, "Исполнен"),
            (self.overdue_card, "Просрочен"),
        ):
            card.setProperty("stat_status", status)
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            card.installEventFilter(self)
            stats.addWidget(card)
        return stats

    def _build_search_card(self):
        card = create_card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("Поиск и фильтрация")
        title.setStyleSheet("font-size: 13px; font-weight: 700; color: #0F172A;")
        self.search_count_label = QLabel("Найдено: 0")
        self.search_count_label.setStyleSheet(
            "color: #64748B; font-size: 12px; font-weight: 600;"
        )
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.search_count_label)
        layout.addLayout(header)

        search_row = QHBoxLayout()
        search_row.setSpacing(10)
        self.search_field = NoWheelComboBox()
        self.search_field.addItem("Все поля")
        self.search_field.addItems(SEARCH_FIELDS)
        self.search_field.setFixedWidth(160)
        self.search_field.setFixedHeight(34)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Введите значение...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setFixedHeight(34)
        search_row.addWidget(self.search_field)
        search_row.addWidget(self.search_input, 1)
        layout.addLayout(search_row)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)
        status_label = QLabel("Статус:")
        status_label.setStyleSheet("color: #475467; font-size: 12px; font-weight: 500;")
        self.status_filter = NoWheelComboBox()
        self.status_filter.addItem("Все статусы")
        self.status_filter.addItems(["Активные"] + STATUSES + ["Просрочен", "Дубликат"])
        self.status_filter.setFixedHeight(32)
        self.status_filter.setMinimumWidth(140)
        filter_row.addWidget(status_label)
        filter_row.addWidget(self.status_filter)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setStyleSheet("color: #EAECF0;")
        filter_row.addWidget(separator)

        date_label = QLabel("Период:")
        date_label.setStyleSheet("color: #475467; font-size: 12px; font-weight: 500;")
        filter_row.addWidget(date_label)
        self.date_from_filter = SmartDateEdit(
            optional=True, empty_text="С даты", minimum_date=QDate(*REGISTRY_MIN_DATE)
        )
        self.date_from_filter.setFixedHeight(32)
        self.date_from_filter.setMinimumWidth(132)
        filter_row.addWidget(self.date_from_filter)
        arrow = QLabel("→")
        arrow.setStyleSheet("color: #98A2B3;")
        filter_row.addWidget(arrow)
        self.date_to_filter = SmartDateEdit(
            optional=True, empty_text="По дату", minimum_date=QDate(*REGISTRY_MIN_DATE)
        )
        self.date_to_filter.setFixedHeight(32)
        self.date_to_filter.setMinimumWidth(132)
        filter_row.addWidget(self.date_to_filter)

        deadline_label = QLabel("Срок:")
        deadline_label.setStyleSheet("color: #475467; font-size: 12px; font-weight: 500;")
        self.deadline_filter = NoWheelComboBox()
        self.deadline_filter.addItems(
            ["Любой", "Сегодня", "Ближайшие 7 дней", "Без срока", "Просрочен"]
        )
        self.deadline_filter.setFixedHeight(32)
        self.deadline_filter.setMinimumWidth(135)
        filter_row.addWidget(deadline_label)
        filter_row.addWidget(self.deadline_filter)
        filter_row.addStretch()

        self.clear_search_button = QPushButton("Сбросить")
        self.clear_search_button.setFixedHeight(32)
        self.clear_search_button.setStyleSheet(
            """
            QPushButton {
                background-color: #F8FAFC; color: #475569; border:1px solid #E2E8F0; border-radius: 8px;
                padding: 0 16px; font-weight: 600;
            }
            QPushButton:hover { background-color: #F1F5F9; border-color:#CBD5E1; }
            """
        )
        filter_row.addWidget(self.clear_search_button)
        layout.addLayout(filter_row)

        # Поля «Стороны» и «Адресат» скрыты из интерфейса, чтобы освободить место для таблицы запросов
        self.sender_filter = QLineEdit(card)
        self.sender_filter.hide()
        self.recipient_filter = QLineEdit(card)
        self.recipient_filter.hide()

        quick_row = QHBoxLayout()
        quick_row.setSpacing(6)
        quick_label = QLabel("Быстрый период:")
        quick_label.setStyleSheet("color:#667085; font-size:11px; font-weight:600;")
        quick_row.addWidget(quick_label)
        for text, mode in (
            ("Сегодня", "today"),
            ("7 дней", "7d"),
            ("30 дней", "30d"),
            ("Этот месяц", "month"),
            ("Весь период", "all"),
        ):
            button = QPushButton(text)
            button.setFixedHeight(26)
            button.setStyleSheet(
                "QPushButton { background:#FFFFFF; color:#475569; border:1px solid #E2E8F0; "
                "border-radius:6px; padding:2px 8px; font-size:11px; font-weight:600; } "
                "QPushButton:hover { background:#EFF6FF; color:#1D4ED8; border-color:#BFDBFE; }"
            )
            button.clicked.connect(lambda _checked=False, preset=mode: self._set_quick_period(preset))
            quick_row.addWidget(button)
        quick_row.addStretch()
        layout.addLayout(quick_row)

        self.search_input.textChanged.connect(self.search_timer.start)
        self.sender_filter.textChanged.connect(self.search_timer.start)
        self.recipient_filter.textChanged.connect(self.search_timer.start)
        self.search_field.currentIndexChanged.connect(self.search_field_changed)
        self.status_filter.currentIndexChanged.connect(self.apply_filters)
        self.status_filter.currentIndexChanged.connect(self.update_stat_card_selection)
        self.date_from_filter.dateChanged.connect(self.apply_filters)
        self.date_to_filter.dateChanged.connect(self.apply_filters)
        self.deadline_filter.currentIndexChanged.connect(self.apply_filters)
        self.deadline_filter.currentIndexChanged.connect(self.update_stat_card_selection)
        self.clear_search_button.clicked.connect(self.clear_search)

        return card

    def _build_table_card(self):
        card = create_card()
        card.setStyleSheet(
            "QFrame#card { background: white; border: 1px solid #EAECF0; border-radius: 12px; }"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.bulk_bar = self._build_bulk_bar(card)
        self.bulk_bar.hide()
        layout.addWidget(self.bulk_bar)

        self.table = QTableWidget()
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels(
            ["★", "ID", "Дата", "Срок", "От кого", "На кого", "Суть запроса", "Примечание", "Статус"]
        )
        self.table.setColumnHidden(1, True)
        self.table.setItemDelegateForColumn(8, StatusDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(48)
        self.table.setStyleSheet(
            """
            QTableWidget {
                background-color: #FFFFFF; alternate-background-color: #FBFDFF;
                border: none; border-radius: 0px; gridline-color: transparent;
                font-size: 13px; outline: none; selection-background-color: #EAF2FF;
                selection-color: #101828;
            }
            QTableWidget::item { padding: 6px 10px; border-bottom: 1px solid #EEF2F6; }
            QTableWidget::item:selected { background-color: #EAF2FF; color: #101828; }
            QTableWidget::item:hover { background-color: #F4F8FE; }
            QHeaderView { background-color: #F8FAFC; }
            QHeaderView::section {
                background-color: #F8FAFC; color: #475569; border: none;
                border-bottom: 1px solid #E4E7EC; padding: 8px 10px;
                font-size: 11px; font-weight: 700;
            }
            QScrollBar:vertical { background: transparent; width: 10px; margin: 6px 2px 6px 0; }
            QScrollBar::handle:vertical { background: #C7D2E0; min-height: 40px; border-radius: 5px; }
            QScrollBar::handle:vertical:hover { background: #98A8BC; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
            """
        )

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self.sort_table)
        for column, width in ((0, 38), (1, 60), (2, 110), (3, 110), (4, 240), (5, 170), (6, 260), (7, 120), (8, 140)):
            self.table.setColumnWidth(column, width)

        self.table.cellClicked.connect(self.handle_pin_click)
        self.table.cellDoubleClicked.connect(self._handle_table_double_click)
        self.table.itemSelectionChanged.connect(self._update_bulk_bar)
        layout.addWidget(self.table)

        self.empty_label = QLabel(
            "Запросы не найдены\nИзмените фильтры или создайте новый запрос."
        )
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setMinimumHeight(180)
        self.empty_label.setStyleSheet(
            "color:#667085; font-size:13px; font-weight:600; background:#FFFFFF; "
            "padding:32px; border:none;"
        )
        self.empty_label.hide()
        layout.addWidget(self.empty_label, 1)

        self.paging_bar = QFrame()
        paging_layout = QHBoxLayout(self.paging_bar)
        paging_layout.setContentsMargins(12, 8, 12, 10)
        self.paging_label = QLabel("")
        self.paging_label.setStyleSheet("color:#667085; font-size:11px; font-weight:600;")
        self.load_more_button = QPushButton("Показать ещё")
        self.load_more_button.setFixedHeight(32)
        self.load_more_button.setStyleSheet(
            "QPushButton { background:#FFFFFF; color:#344054; border:1px solid #D0D5DD; "
            "border-radius:8px; padding:0 14px; font-size:11px; font-weight:650; } "
            "QPushButton:hover { background:#F8FAFC; border-color:#98A2B3; }"
        )
        self.load_more_button.clicked.connect(self._load_more)
        paging_layout.addWidget(self.paging_label)
        paging_layout.addStretch()
        paging_layout.addWidget(self.load_more_button)
        layout.addWidget(self.paging_bar)
        return card

    def _build_bulk_bar(self, parent):
        bar = QFrame(parent)
        bar.setObjectName("bulkActionBar")
        bar.setMinimumHeight(54)
        bar.setStyleSheet(
            "QFrame#bulkActionBar { background:#F5F8FF; border:none; border-bottom:1px solid #DDE7F8; }"
        )
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 9, 12, 9)
        row.setSpacing(9)
        row.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.bulk_count_label = QLabel("Выбрано: 0")
        self.bulk_count_label.setFixedHeight(34)
        self.bulk_count_label.setMinimumWidth(92)
        self.bulk_count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bulk_count_label.setStyleSheet(
            "QLabel { background:#E7F0FF; color:#1849A9; border:1px solid #C9DBFF; "
            "border-radius:9px; padding:0 12px; font-size:12px; font-weight:700; }"
        )
        row.addWidget(self.bulk_count_label, 0, Qt.AlignmentFlag.AlignVCenter)

        def menu_button(text, actions):
            button = QPushButton(text)
            button.setFixedHeight(34)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                "QPushButton { background:#FFFFFF; color:#344054; border:1px solid #C7D7F4; "
                "border-radius:9px; padding:0 13px; font-size:12px; font-weight:600; } "
                "QPushButton:hover { background:#EEF4FF; border-color:#84ADFF; color:#1849A9; } "
                "QPushButton:pressed { background:#E3ECFF; } "
                "QPushButton::menu-indicator { width:0px; }"
            )
            menu = QMenu(button)
            menu.setStyleSheet(
                "QMenu { background:#FFFFFF; border:1px solid #E4E7EC; border-radius:8px; "
                "padding:5px; } QMenu::item { padding:7px 22px 7px 10px; border-radius:6px; } "
                "QMenu::item:selected { background:#EFF6FF; color:#175CD3; }"
            )
            for label, action_code in actions:
                action = menu.addAction(label)
                action.triggered.connect(
                    lambda _checked=False, code=action_code: self._emit_bulk_action(code)
                )
            button.setMenu(menu)
            return button

        row.addWidget(menu_button("Статус ▾", [
            ("Новый", "status:new"),
            ("В работе", "status:progress"),
            ("Исполнен", "status:done"),
            ("Отклонен", "status:rejected"),
        ]))
        row.addWidget(menu_button("Срок ▾", [
            ("Сегодня", "due:today"),
            ("+7 дней", "due:+7"),
            ("+14 дней", "due:+14"),
            ("+30 дней", "due:+30"),
            ("Очистить срок", "due:clear"),
        ]))
        row.addWidget(menu_button("Закрепление ▾", [
            ("Закрепить", "pin:on"),
            ("Открепить", "pin:off"),
        ]))

        export_button = QPushButton("Экспорт выбранных")
        export_button.setFixedHeight(34)
        export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        export_button.setStyleSheet(
            "QPushButton { background:#FFFFFF; color:#175CD3; border:1px solid #B2CCFF; "
            "border-radius:9px; padding:0 13px; font-size:12px; font-weight:650; } "
            "QPushButton:hover { background:#EFF6FF; border-color:#84ADFF; } "
            "QPushButton:pressed { background:#E3ECFF; }"
        )
        export_button.clicked.connect(lambda: self._emit_bulk_action("export"))
        row.addWidget(export_button)

        row.addStretch()
        delete_button = QPushButton("Удалить выбранные")
        delete_button.setFixedHeight(34)
        delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_button.setStyleSheet(
            "QPushButton { background:#FFF7F6; color:#B42318; border:1px solid #FECDCA; "
            "border-radius:9px; padding:0 13px; font-size:12px; font-weight:700; } "
            "QPushButton:hover { background:#FEE4E2; border-color:#FDA29B; } "
            "QPushButton:pressed { background:#FECDCA; }"
        )
        delete_button.clicked.connect(lambda: self._emit_bulk_action("delete"))
        row.addWidget(delete_button)
        return bar

    def _emit_bulk_action(self, action: str):
        selected = self.get_selected_requests()
        if not selected:
            return
        self.bulk_action_requested.emit(action, selected)

    def _update_bulk_bar(self):
        selected = self.get_selected_requests()
        count = len(selected)
        self.bulk_count_label.setText(f"Выбрано: {count}")
        self.bulk_bar.setVisible(count > 0)

    def eventFilter(self, source, event):
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
            and source.property("stat_status") is not None
        ):
            self.filter_by_status(source.property("stat_status"))
            return True
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
            and source is self.total_card
        ):
            self.filter_by_status(None)
            return True
        return super().eventFilter(source, event)

    def refresh(self):
        self._manual_requests_mode = False
        self._page_offset = 0
        # Data may have changed outside the current filter (e.g. a request was
        # edited or removed), so the duplicate summary must be refreshed.
        self._duplicate_map_cache = None
        self._registry_counts_cache = None
        self._registry_counts_cache_day = ""
        self._load_page(reset=True)

    def set_requests(self, requests):
        """Compatibility path for tests/legacy callers that provide rows directly."""
        self._manual_requests_mode = True
        self.requests = list(requests)
        self._duplicate_map_cache = None
        self._registry_counts_cache = None
        self._registry_counts_cache_day = ""
        self._rebuild_filter_cache()
        self.update_statistics()
        self._apply_filters_in_memory()

    def _rebuild_filter_cache(self, requests=None, *, reset: bool = True):
        """Build the filter cache, optionally extending it for newly loaded rows."""
        if reset:
            cache: dict[int, dict[str, object]] = {}
            counts: Counter[str] = Counter()
        else:
            cache = self._filter_cache
            counts = self._status_counts
        for request in self.requests if requests is None else requests:
            key = id(request)
            if key in cache:
                continue
            received = QDate.fromString(request.received_date, "dd.MM.yyyy")
            due_date = QDate.fromString(request.due_date, "dd.MM.yyyy")
            display_status = request.display_status()
            values = {
                "received_date": str(request.received_date or "").casefold(),
                "due_date": str(request.due_date or "").casefold(),
                "completed_date": str(request.completed_date or "").casefold(),
                "from_department": str(request.from_department or "").casefold(),
                "from_position": str(request.from_position or "").casefold(),
                "from_person": str(request.from_person or "").casefold(),
                "to_person": str(request.to_person or "").casefold(),
                "request_subject": str(request.request_subject or "").casefold(),
                "note": str(request.note or "").casefold(),
                "status": str(display_status or "").casefold(),
            }
            cache[key] = {
                "date": received.toJulianDay() if received.isValid() else 0,
                "due": due_date.toJulianDay() if due_date.isValid() else 0,
                "display_status": display_status,
                "from_text": "\n".join(
                    part for part in (
                        request.from_department,
                        request.from_position,
                        request.from_person,
                    ) if part
                ),
                **values,
            }
            counts[display_status] += 1
        self._filter_cache = cache
        self._status_counts = counts

    def _get_duplicate_map_cached(self) -> dict[int, dict]:
        if self._duplicate_map_cache is None:
            try:
                self._duplicate_map_cache = self.request_service.get_duplicate_map()
            except Exception:
                self._duplicate_map_cache = {}
        return self._duplicate_map_cache

    def update_statistics(self):
        if not self._manual_requests_mode:
            today_key = QDate.currentDate().toString("yyyy-MM-dd")
            if (
                self._registry_counts_cache is None
                or self._registry_counts_cache_day != today_key
            ):
                try:
                    counts = self.request_service.get_registry_status_counts()
                except Exception:
                    counts = {}
                self._registry_counts_cache = counts or None
                self._registry_counts_cache_day = today_key if counts else ""
            counts = self._registry_counts_cache or {}
            if counts:
                self.total_value.setText(str(counts.get("total", 0)))
                self.new_value.setText(str(counts.get("Новый", 0)))
                self.progress_value.setText(str(counts.get("В работе", 0)))
                self.done_value.setText(str(counts.get("Исполнен", 0)))
                self.overdue_value.setText(str(counts.get("Просрочен", 0)))
                return
        self.total_value.setText(str(len(self.requests)))
        self.new_value.setText(str(self._status_counts["Новый"]))
        self.progress_value.setText(str(self._status_counts["В работе"]))
        self.done_value.setText(str(self._status_counts["Исполнен"]))
        self.overdue_value.setText(str(self._status_counts["Просрочен"]))

    def count_status(self, status):
        return self._status_counts[status]

    def filter_by_status(self, status):
        status_blocker = QSignalBlocker(self.status_filter)
        deadline_blocker = QSignalBlocker(self.deadline_filter)
        if status is None:
            self.status_filter.setCurrentIndex(0)
        else:
            index = self.status_filter.findText(status)
            if index >= 0:
                self.status_filter.setCurrentIndex(index)
        self.deadline_filter.setCurrentIndex(0)
        self.sender_filter.clear()
        self.recipient_filter.clear()
        del status_blocker, deadline_blocker
        self.update_stat_card_selection()
        self.apply_filters()

    def update_stat_card_selection(self):
        status = self.status_filter.currentText()
        for card, card_status in (
            (self.total_card, "Все статусы"),
            (self.new_card, "Новый"),
            (self.progress_card, "В работе"),
            (self.done_card, "Исполнен"),
            (self.overdue_card, "Просрочен"),
        ):
            active = status == card_status
            card.setProperty("active", active)
            card.style().unpolish(card)
            card.style().polish(card)
            card.update()

    def display_requests(self, requests, *, append: bool = False):
        has_rows = bool(requests)
        self.table.setVisible(has_rows)
        if hasattr(self, "empty_label"):
            self.empty_label.setVisible(not has_rows)
        self.table.setUpdatesEnabled(False)
        duplicate_map = self._get_duplicate_map_cached()
        try:
            previous_row_count = self.table.rowCount()
            start_row = previous_row_count if append and previous_row_count <= len(requests) else 0
            self.table.setRowCount(len(requests))
            for row_index in range(start_row, len(requests)):
                request = requests[row_index]
                cached = self._filter_cache.get(id(request), {})
                from_text = str(cached.get("from_text", ""))

                pin_item = QTableWidgetItem("★" if request.is_pinned else "☆")
                pin_item.setData(Qt.ItemDataRole.UserRole, request.id)
                pin_item.setToolTip("Открепить запрос" if request.is_pinned else "Закрепить запрос")
                pin_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if request.is_pinned:
                    pin_item.setForeground(QBrush(QColor("#F59E0B")))
                    font = pin_item.font()
                    font.setBold(True)
                    pin_item.setFont(font)
                self.table.setItem(row_index, 0, pin_item)

                id_item = QTableWidgetItem(str(request.id))
                id_item.setData(Qt.ItemDataRole.UserRole, request.id)
                self.table.setItem(row_index, 1, id_item)

                snippet = self._search_snippets.get(int(request.id or 0), "")
                subject_display = str(request.request_subject or "")
                if snippet:
                    compact_snippet = " ".join(snippet.split())
                    if len(compact_snippet) > 150:
                        compact_snippet = compact_snippet[:147] + "..."
                    subject_display = (subject_display + "\n↳ " + compact_snippet).strip()
                values = (
                    request.received_date,
                    request.due_date,
                    from_text,
                    request.to_person,
                    subject_display,
                    request.note,
                    request.display_status(),
                )
                dup = duplicate_map.get(request.id)
                for offset, raw_value in enumerate(values):
                    value = str(raw_value or "")
                    column = offset + 2
                    item = QTableWidgetItem(value)
                    item.setToolTip(value)
                    if column == 8 and dup and dup.get("is_duplicate"):
                        item.setData(Qt.ItemDataRole.UserRole + 2, dup)
                        orig_id = dup.get("original_id", "")
                        orig_date = dup.get("original_date", "")
                        orig_dept = dup.get("original_department", "")
                        orig_subj = dup.get("original_subject", "")
                        date_part = f" от {orig_date}" if orig_date else ""
                        dept_part = f" [{orig_dept}]" if orig_dept else ""
                        subj_part = f"\nСуть: «{orig_subj[:60]}…»" if len(orig_subj) > 60 else (f"\nСуть: «{orig_subj}»" if orig_subj else "")
                        item.setToolTip(f"Статус: {value}\n\n⚠️ Внимание: дубликат документа!\nОригинал: Запрос #{orig_id}{date_part}{dept_part}{subj_part}")
                    if column in (2, 3, 8):
                        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.table.setItem(row_index, column, item)

                row_background = None
                if request.is_overdue():
                    row_background = QColor("#FFF1F0")
                    due_item = self.table.item(row_index, 3)
                    if due_item is not None:
                        due_item.setForeground(QBrush(QColor("#D92D20")))
                        font = due_item.font()
                        font.setBold(True)
                        due_item.setFont(font)
                elif request.is_pinned:
                    row_background = QColor("#FFF9E6")
                if row_background is not None:
                    for column in range(self.table.columnCount()):
                        item = self.table.item(row_index, column)
                        if item is not None:
                            item.setBackground(row_background)
                self.table.setRowHeight(row_index, 66 if snippet else 48)
        finally:
            self.table.setUpdatesEnabled(True)

    def apply_filters(self, *_args):
        if self._manual_requests_mode:
            self._apply_filters_in_memory()
            return
        self._page_offset = 0
        self._load_page(reset=True)

    def _load_more(self):
        if self._manual_requests_mode or len(self.requests) >= self._total_matching:
            return
        self._page_offset = len(self.requests)
        self._load_page(reset=False)

    def _load_page(self, *, reset: bool):
        date_from_iso = "" if self.date_from_filter.is_empty() else self.date_from_filter.date().toString("yyyy-MM-dd")
        date_to_iso = "" if self.date_to_filter.is_empty() else self.date_to_filter.date().toString("yyyy-MM-dd")
        try:
            page, total = self.request_service.query_requests_page(
                search_text=self.search_input.text().strip(),
                search_field=self.search_field.currentText(),
                sender=self.sender_filter.text().strip(),
                recipient=self.recipient_filter.text().strip(),
                status=self.status_filter.currentText(),
                deadline=self.deadline_filter.currentText(),
                date_from_iso=date_from_iso,
                date_to_iso=date_to_iso,
                sort_column=self.sort_column,
                sort_ascending=self.sort_ascending,
                limit=self._page_size,
                offset=0 if reset else self._page_offset,
                include_total=reset,
            )
        except Exception:
            # Keep the registry usable if a migration/custom SQLite build cannot
            # satisfy the paged query yet. The compatibility path is bounded.
            fallback = self.request_service.get_requests()[: self._page_size]
            self.requests = list(fallback)
            self._manual_requests_mode = True
            self._rebuild_filter_cache()
            self.update_statistics()
            self._apply_filters_in_memory()
            return

        if reset:
            self.requests = list(page)
            new_requests = list(page)
            self._search_snippets = {}
            self._rebuild_filter_cache(reset=True)
        else:
            known = {request.id for request in self.requests}
            new_requests = [request for request in page if request.id not in known]
            self.requests.extend(new_requests)
            self._rebuild_filter_cache(new_requests, reset=False)
        if total is not None:
            self._total_matching = int(total)
        if reset or self._registry_counts_cache_day != QDate.currentDate().toString("yyyy-MM-dd"):
            # Status cards describe the whole registry, not the visible page.
            # Refresh on the first action after midnight so overdue counts stay current.
            self.update_statistics()

        search_raw = self.search_input.text().strip()
        if search_raw and self.search_field.currentText() == "Все поля" and new_requests:
            try:
                page_ids = {int(request.id) for request in new_requests if request.id is not None}
                if page_ids:
                    hits = self.request_service.search_request_hits_full_text(
                        search_raw,
                        limit=min(2000, max(len(page_ids), self._page_size)),
                        request_ids=page_ids,
                    )
                    self._search_snippets.update({
                        int(hit["request_id"]): str(hit.get("snippet", "") or "")
                        for hit in hits
                        if int(hit["request_id"]) in page_ids
                        and str(hit.get("snippet", "") or "").strip()
                    })
            except Exception:
                # Preserve snippets already calculated for earlier pages.
                pass
        elif reset:
            self._search_snippets = {}

        self._last_filtered = list(self.requests)
        self.display_requests(self.requests, append=not reset)
        shown = len(self.requests)
        self.search_count_label.setText(f"Показано: {shown} из {self._total_matching}")
        self.paging_label.setText(f"Загружено {shown} из {self._total_matching}")
        self.load_more_button.setVisible(shown < self._total_matching)
        self.paging_bar.setVisible(self._total_matching > self._page_size)
        self.update_sort_headers()

    def _apply_filters_in_memory(self):
        search_raw = self.search_input.text().strip()
        search_text = search_raw.casefold()
        selected_field = self.search_field.currentText()
        full_text_ids = None
        self._search_snippets = {}
        if search_raw and selected_field == "Все поля":
            try:
                hits = self.request_service.search_request_hits_full_text(search_raw, limit=2000)
                full_text_ids = {int(hit["request_id"]) for hit in hits}
                self._search_snippets = {
                    int(hit["request_id"]): str(hit.get("snippet", "") or "")
                    for hit in hits if str(hit.get("snippet", "") or "").strip()
                }
            except Exception:
                # The in-memory metadata filter remains a safe fallback if a
                # search index is temporarily unavailable or being migrated.
                full_text_ids = None
        sender_filter = self.sender_filter.text().strip().casefold()
        recipient_filter = self.recipient_filter.text().strip().casefold()
        selected_status = self.status_filter.currentText()
        selected_deadline = self.deadline_filter.currentText()
        date_from = (
            0 if self.date_from_filter.is_empty()
            else self.date_from_filter.date().toJulianDay()
        )
        date_to = (
            0 if self.date_to_filter.is_empty()
            else self.date_to_filter.date().toJulianDay()
        )
        today_julian = QDate.currentDate().toJulianDay()

        field_mapping = {
            "Дата поступления": "received_date",
            "Срок исполнения": "due_date",
            "Отдел": "from_department",
            "Должность": "from_position",
            "От кого": "from_person",
            "На кого": "to_person",
            "Суть запроса": "request_subject",
            "Статус": "status",
        }

        is_dup_filter = selected_status in ("Дубликат", "Дубликаты")
        is_dup_search = any(term in search_text for term in ("дубликат", "дубликаты", "дубль", "дубли", "копи"))
        dup_map = None
        if is_dup_filter or (search_text and is_dup_search and selected_field in ("Все поля", "Статус")):
            try:
                dup_map = self._get_duplicate_map_cached()
            except Exception:
                dup_map = {}

        filtered = []
        for request in self.requests:
            if selected_status == "Активные":
                if request.status in ("Исполнен", "Отклонен", "Отклонён"):
                    continue
            elif selected_status in ("Дубликат", "Дубликаты"):
                if not dup_map or request.id not in dup_map:
                    continue
            elif selected_status != "Все статусы":
                if selected_status in ("Отклонен", "Отклонён"):
                    if request.display_status() not in ("Отклонен", "Отклонён"):
                        continue
                elif request.display_status() != selected_status:
                    continue

            cached = self._filter_cache.get(id(request), {})
            if sender_filter:
                sender_haystack = " ".join((
                    str(cached.get("from_department", "")),
                    str(cached.get("from_position", "")),
                    str(cached.get("from_person", "")),
                ))
                if sender_filter not in sender_haystack:
                    continue
            if recipient_filter and recipient_filter not in str(cached.get("to_person", "")):
                continue
            request_date = int(cached.get("date", 0))
            if request_date:
                if date_from and request_date < date_from:
                    continue
                if date_to and request_date > date_to:
                    continue

            due_julian = int(cached.get("due", 0))
            if selected_deadline == "Сегодня":
                if request.status in ("Исполнен", "Отклонен", "Отклонён") or due_julian != today_julian:
                    continue
            if selected_deadline == "Ближайшие 7 дней":
                if request.status in ("Исполнен", "Отклонен", "Отклонён") or not due_julian or not (today_julian <= due_julian <= today_julian + 6):
                    continue
            if selected_deadline == "Без срока" and due_julian:
                continue
            if selected_deadline == "Просрочен" and not request.is_overdue():
                continue

            if search_text:
                if selected_field == "Все поля":
                    if full_text_ids is not None:
                        if request.id not in full_text_ids and not (is_dup_search and dup_map and request.id in dup_map):
                            continue
                    else:
                        match_text = search_text in " ".join(
                            str(cached.get(key, ""))
                            for key in (
                                "received_date", "due_date", "completed_date",
                                "from_department", "from_position", "from_person",
                                "to_person", "request_subject", "note", "status",
                            )
                        )
                        match_dup = is_dup_search and dup_map and request.id in dup_map
                        if not match_text and not match_dup:
                            continue
                elif selected_field == "Статус":
                    status_text = str(cached.get("status", ""))
                    match_status = search_text in status_text
                    match_dup = is_dup_search and dup_map and request.id in dup_map
                    if not match_status and not match_dup:
                        continue
                else:
                    attr = field_mapping.get(selected_field)
                    if search_text not in str(cached.get(attr or "", "")):
                        continue
            filtered.append(request)

        pinned = [request for request in filtered if request.is_pinned]
        unpinned = [request for request in filtered if not request.is_pinned]
        if self.sort_column is not None:
            pinned.sort(key=self._sort_value, reverse=not self.sort_ascending)
            unpinned.sort(key=self._sort_value, reverse=not self.sort_ascending)
        filtered = pinned + unpinned
        self._last_filtered = list(filtered)

        self.display_requests(filtered)
        self.search_count_label.setText(f"Найдено: {len(filtered)}")
        self.update_sort_headers()

    def _sort_value(self, request):
        cached = self._filter_cache.get(id(request), {})
        if self.sort_column == 2:
            return int(cached.get("date", 0))
        if self.sort_column == 3:
            return int(cached.get("due", 0))
        if self.sort_column == 4:
            return str(cached.get("from_department", ""))
        if self.sort_column == 5:
            return str(cached.get("to_person", ""))
        if self.sort_column == 6:
            return str(cached.get("request_subject", ""))
        if self.sort_column == 7:
            return str(cached.get("note", ""))
        if self.sort_column == 8:
            return str(cached.get("status", ""))
        return ""

    def get_filter_snapshot_fetcher(self):
        """Capture filter values on the GUI thread and return a thread-safe database fetcher."""
        if getattr(self, "_manual_requests_mode", False) or getattr(self, "request_service", None) is None:
            snapshot = list(self._last_filtered)
            return lambda: snapshot

        date_from_iso = "" if self.date_from_filter.is_empty() else self.date_from_filter.date().toString("yyyy-MM-dd")
        date_to_iso = "" if self.date_to_filter.is_empty() else self.date_to_filter.date().toString("yyyy-MM-dd")
        search_text = self.search_input.text().strip()
        search_field = self.search_field.currentText()
        sender = self.sender_filter.text().strip()
        recipient = self.recipient_filter.text().strip()
        status = self.status_filter.currentText()
        deadline = self.deadline_filter.currentText()
        sort_column = self.sort_column
        sort_ascending = self.sort_ascending
        req_service = self.request_service
        last_filtered_snapshot = list(self._last_filtered)

        def fetcher():
            try:
                page, _total = req_service.query_requests_page(
                    search_text=search_text,
                    search_field=search_field,
                    sender=sender,
                    recipient=recipient,
                    status=status,
                    deadline=deadline,
                    date_from_iso=date_from_iso,
                    date_to_iso=date_to_iso,
                    sort_column=sort_column,
                    sort_ascending=sort_ascending,
                    limit=100_000,
                    offset=0,
                    include_total=False,
                )
                return list(page)
            except Exception:
                return last_filtered_snapshot

        return fetcher

    def get_all_filtered_requests(self) -> list:
        """Return all matching requests for the current filter across all pages."""
        return self.get_filter_snapshot_fetcher()()


    def _set_quick_period(self, preset: str):
        today = QDate.currentDate()
        if preset == "today":
            start, end = today, today
        elif preset == "7d":
            start, end = today.addDays(-6), today
        elif preset == "30d":
            start, end = today.addDays(-29), today
        elif preset == "month":
            start, end = QDate(today.year(), today.month(), 1), today
        else:
            self.date_from_filter.clear_date()
            self.date_to_filter.clear_date()
            self.apply_filters()
            return

        blockers = [QSignalBlocker(self.date_from_filter), QSignalBlocker(self.date_to_filter)]
        self.date_from_filter.setDate(start)
        self.date_to_filter.setDate(end)
        del blockers
        self.apply_filters()

    def clear_search(self):
        self.search_timer.stop()
        blockers = [
            QSignalBlocker(self.search_input),
            QSignalBlocker(self.status_filter),
            QSignalBlocker(self.search_field),
            QSignalBlocker(self.date_from_filter),
            QSignalBlocker(self.date_to_filter),
            QSignalBlocker(self.deadline_filter),
            QSignalBlocker(self.sender_filter),
            QSignalBlocker(self.recipient_filter),
        ]
        self.search_input.clear()
        self.status_filter.setCurrentIndex(0)
        self.search_field.setCurrentIndex(0)
        self.date_from_filter.clear_date()
        self.date_to_filter.clear_date()
        self.deadline_filter.setCurrentIndex(0)
        self.sender_filter.clear()
        self.recipient_filter.clear()
        del blockers
        self.sort_column = None
        self.sort_ascending = True
        self.search_field_changed()
        self.update_stat_card_selection()

    def search_field_changed(self):
        placeholders = {
            "Все поля": "Поиск по реквизитам и распознанному тексту PDF...",
            "Дата поступления": "Например: 18.08.2026",
            "Срок исполнения": "Например: 25.08.2026",
            "Отдел": "Например: отдел кадров",
            "Должность": "Например: начальник",
            "От кого": "Например: Иванов",
            "На кого": "Например: Петров",
            "Суть запроса": "Введите часть текста",
            "Статус": "Например: Новый",
        }
        self.search_input.setPlaceholderText(
            placeholders.get(self.search_field.currentText(), "Введите значение...")
        )
        self.apply_filters()

    def sort_table(self, column):
        if column in (0, 1):
            return
        if self.sort_column == column:
            self.sort_ascending = not self.sort_ascending
        else:
            self.sort_column = column
            self.sort_ascending = True
        self.apply_filters()

    def update_sort_headers(self):
        headers = ["★", "", "Дата", "Срок", "От кого", "На кого", "Суть запроса", "Примечание", "Статус"]
        for column, text in enumerate(headers):
            item = self.table.horizontalHeaderItem(column)
            if item is None:
                continue
            if column == self.sort_column:
                item.setText(text + (" ↑" if self.sort_ascending else " ↓"))
            else:
                item.setText(text)

    def _handle_table_double_click(self, row: int, column: int):
        request = self.get_selected_request(warn=False)
        if (
            request is not None
            and column == 6
            and self.search_input.text().strip()
            and request.source_document
        ):
            self.document_requested.emit(request.source_document)
            return
        self.edit_requested.emit()

    def handle_pin_click(self, row, column):
        if column != 0:
            return
        item = self.table.item(row, column)
        if item is None:
            return
        request_id = item.data(Qt.ItemDataRole.UserRole)
        if request_id is None:
            return
        self.request_service.toggle_request_pinned(int(request_id))
        self.refresh()

    def get_selected_request(self, warn=True):
        row = self.table.currentRow()
        if row < 0:
            if warn:
                QMessageBox.warning(self, "Запрос не выбран", "Выберите запись в таблице.")
            return None
        item = self.table.item(row, 1)
        if item is None:
            return None
        try:
            request_id = int(item.text())
        except ValueError:
            return None
        return next((request for request in self.requests if request.id == request_id), None)

    def get_selected_requests(self):
        selected_rows = sorted(index.row() for index in self.table.selectionModel().selectedRows())
        by_id = {request.id: request for request in self.requests}
        selected = []
        for row in selected_rows:
            item = self.table.item(row, 1)
            if item is None:
                continue
            try:
                request_id = int(item.text())
            except ValueError:
                continue
            request = by_id.get(request_id)
            if request is not None:
                selected.append(request)
        return selected
