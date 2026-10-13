from __future__ import annotations

from datetime import datetime
from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui.delegates import DocumentActionsDelegate
from app.ui.styles import PRIMARY_COLOR, PRIMARY_HOVER
from app.ui.widgets import create_card


class DocumentsPage(QWidget):
    add_requested = Signal()
    open_requested = Signal(object)
    delete_requested = Signal(object)

    def __init__(self, document_service, parent=None):
        super().__init__(parent)
        self.document_service = document_service
        self._entries = []
        self._search_names: list[str] = []
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._apply_filter)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)
        root.addLayout(self._build_header())
        root.addWidget(self._build_storage_card())
        root.addWidget(self._build_documents_card(), 1)

    def _build_header(self):
        header = QHBoxLayout()
        header.setSpacing(14)

        title_layout = QVBoxLayout()
        title_layout.setSpacing(3)
        title = QLabel("Документы")
        title.setStyleSheet("font-size:28px; font-weight:750; color:#0F172A; letter-spacing:-0.35px;")
        description = QLabel("Исходные файлы, прикреплённые к запросам")
        description.setStyleSheet("color:#64748B; font-size:13px; font-weight:500;")
        title_layout.addWidget(title)
        title_layout.addWidget(description)

        title_container = QWidget()
        title_container.setObjectName('titleContainer')
        title_container.setStyleSheet('QWidget#titleContainer { background-color: transparent; }')
        title_container.setLayout(title_layout)
        title_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        header.addWidget(title_container, 1)

        add_button = QPushButton("+  Добавить документ")
        add_button.setMinimumWidth(170)
        add_button.setMaximumWidth(190)
        add_button.setMinimumHeight(42)
        add_button.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {PRIMARY_COLOR}; color: white; border:1px solid {PRIMARY_COLOR}; border-radius: 9px;
                padding: 10px 18px; font-weight: 700;
            }}
            QPushButton:hover {{ background-color: {PRIMARY_HOVER}; border-color:{PRIMARY_HOVER}; }}
            """
        )
        add_button.clicked.connect(self.add_requested.emit)
        header.addWidget(add_button)
        return header

    def _build_storage_card(self):
        card = create_card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(7)

        title = QLabel("Хранилище документов")
        title.setStyleSheet("font-size: 13px; font-weight: 700; color: #344054; border: none;")
        self.storage_path = QLabel()
        self.storage_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.storage_path.setStyleSheet(
            """
            QLabel {
                color: #667085; background-color: #F8FAFC; border: 1px solid #E2E8F0;
                border-radius: 7px; padding: 9px 11px; font-size: 12px;
            }
            """
        )
        layout.addWidget(title)
        layout.addWidget(self.storage_path)
        return card

    def _build_documents_card(self):
        card = create_card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        header.setContentsMargins(18, 16, 18, 12)
        title = QLabel("Файлы")
        title.setStyleSheet("font-size: 15px; font-weight: 700; color: #0F172A;")
        self.count_label = QLabel("0 документов · 0 Б")
        self.count_label.setStyleSheet("color: #667085; font-size: 12px; font-weight: 600;")
        header.addWidget(title)
        header.addSpacing(10)
        header.addWidget(self.count_label)
        header.addStretch()
        layout.addLayout(header)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(18, 0, 18, 12)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск документа по имени...")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(38)
        self.search.setStyleSheet(
            """
            QLineEdit {
                background-color: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 8px;
                padding: 9px 12px; color: #344054; font-size: 13px;
            }
            QLineEdit:hover { border-color: #D0D5DD; }
            QLineEdit:focus { background-color: #FFFFFF; border: 1px solid #2563EB; }
            """
        )
        self.search.textChanged.connect(self._search_timer.start)
        search_row.addWidget(self.search)
        layout.addLayout(search_row)

        self.content_stack = QStackedWidget()
        self.empty_widget = self._build_empty_state()
        self.table = self._build_table()
        self.content_stack.addWidget(self.empty_widget)
        self.content_stack.addWidget(self.table)
        layout.addWidget(self.content_stack, 1)
        return card

    @staticmethod
    def _build_empty_state():
        container = QWidget()
        container.setObjectName('emptyStateContainer')
        container.setStyleSheet('QWidget#emptyStateContainer { background-color: transparent; }')
        layout = QVBoxLayout(container)
        layout.setContentsMargins(20, 35, 20, 45)
        layout.setSpacing(8)

        icon = QLabel("📄")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size: 34px; border: none;")
        title = QLabel("Документов пока нет")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color: #344054; font-size: 15px; font-weight: 600; border: none;")
        description = QLabel("Здесь будут отображаться PDF-файлы из текущего хранилища.")
        description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        description.setStyleSheet("color: #98A2B3; font-size: 12px; border: none;")
        layout.addStretch()
        layout.addWidget(icon)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addStretch()
        return container

    def _build_table(self):
        table = QTableWidget()
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(["Имя файла", "Размер", "Изменён", "Путь", "Действие"])
        table.verticalHeader().setVisible(False)
        table.setFrameShape(QFrame.Shape.NoFrame)
        table.setShowGrid(False)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        table.setMinimumHeight(250)
        table.setStyleSheet(
            """
            QTableWidget { background-color: #FFFFFF; border: none; gridline-color: #F2F4F7; }
            QHeaderView::section {
                background-color: #F8FAFC; color: #667085; font-size: 11px; font-weight: 700;
                border: none; border-top: 1px solid #E2E8F0; border-bottom: 1px solid #E2E8F0;
                padding: 9px 20px; text-align: left;
            }
            QTableWidget::item { border-bottom: 1px solid #F2F4F7; padding: 0 10px; }
            QScrollBar:vertical { background: #F8FAFC; width: 10px; margin: 2px 2px 2px 0; border-radius: 5px; }
            QScrollBar::handle:vertical { background: #CBD5E1; min-height: 40px; border-radius: 5px; }
            QScrollBar::handle:vertical:hover { background: #94A3B8; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            """
        )
        table.setItemDelegateForColumn(
            4,
            DocumentActionsDelegate(
                table,
                lambda path: self.open_requested.emit(path),
                lambda path: self.delete_requested.emit(path),
            ),
        )
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(1, 100)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(2, 120)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        table.setColumnWidth(3, 180)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(4, 250)
        return table

    def refresh(self):
        self._entries = self.document_service.list_document_entries()
        documents_dir = self.document_service.documents_dir.resolve()
        self.storage_path.setText(str(documents_dir))
        self.storage_path.setToolTip(str(documents_dir))
        self._update_summary()
        self._populate()
        self.content_stack.setCurrentWidget(
            self.table if self._entries else self.empty_widget
        )
        self._filter(self.search.text())

    def _populate(self):
        self.table.setUpdatesEnabled(False)
        search_names: list[str] = []
        try:
            self.table.setRowCount(len(self._entries))
            self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
            self.table.verticalHeader().setDefaultSectionSize(58)
            for row, entry in enumerate(self._entries):
                file = entry.path
                search_names.append(file.name.casefold())

                name = QTableWidgetItem(f"📄  {file.name}")
                name.setToolTip(str(file))
                self.table.setItem(row, 0, name)

                size = QTableWidgetItem(self._format_size(entry.size_bytes))
                size.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, 1, size)

                changed_text = datetime.fromtimestamp(entry.modified_ns / 1_000_000_000).strftime(
                    "%d.%m.%Y %H:%M"
                )
                changed = QTableWidgetItem(changed_text)
                changed.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, 2, changed)

                parent = QTableWidgetItem(str(file.parent))
                parent.setToolTip(str(file))
                parent.setForeground(QColor("#667085"))
                self.table.setItem(row, 3, parent)

                actions = QTableWidgetItem()
                actions.setData(Qt.ItemDataRole.UserRole, str(file))
                self.table.setItem(row, 4, actions)
        finally:
            self.table.setUpdatesEnabled(True)
        self._search_names = search_names

    def _apply_filter(self):
        self._filter(self.search.text())

    def _filter(self, text: str):
        needle = text.strip().casefold()
        visible = 0
        for row, name in enumerate(self._search_names):
            match = not needle or needle in name
            self.table.setRowHidden(row, not match)
            visible += int(match)
        if self._entries and needle:
            self.count_label.setToolTip(f"По фильтру показано: {visible}")
        else:
            self.count_label.setToolTip("")

    def _update_summary(self):
        total_size = sum(entry.size_bytes for entry in self._entries)
        count = len(self._entries)
        self.count_label.setText(
            f"{count} {self._document_word(count)} · {self._format_size(total_size)}"
        )

    @staticmethod
    def _format_size(size: int) -> str:
        if size < 1024:
            return f"{size} Б"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} КБ"
        if size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} МБ"
        return f"{size / (1024 * 1024 * 1024):.2f} ГБ"

    @staticmethod
    def _document_word(count: int) -> str:
        if count % 10 == 1 and count % 100 != 11:
            return "документ"
        if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
            return "документа"
        return "документов"
