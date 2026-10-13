from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QDialog,
    QFrame,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui.dialogs.history_details_dialog import HistoryDetailsDialog
from app.ui.history_utils import format_history_action, format_history_description
from app.ui.widgets import create_card


_ACTION_COLORS = {
    "create": "#027A48",
    "update": "#175CD3",
    "delete": "#B42318",
    "attach": "#7A5AF8",
    "detach": "#D92D20",
    "pin": "#F59E0B",
    "unpin": "#667085",
    "restore": "#0EA5E9",
}


class HistoryPage(QWidget):
    request_restored = Signal()
    toast_requested = Signal(str, str, str)

    def __init__(self, request_service, parent=None):
        super().__init__(parent)
        self.request_service = request_service
        self._search_cache: list[str] = []
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(self._apply_search_filter)
        self._build_ui()
        self.load_history()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)
        root.addWidget(self._build_header())
        root.addWidget(self._build_search_card())
        root.addWidget(self._build_history_card(), 1)

    def _build_header(self):
        container = QWidget()
        container.setObjectName('headerContainer')
        container.setStyleSheet('QWidget#headerContainer { background-color: transparent; }')
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        title_layout = QVBoxLayout()
        title_layout.setSpacing(3)
        title = QLabel("История изменений")
        title.setStyleSheet("font-size:28px; font-weight:750; color:#0F172A; letter-spacing:-0.35px;")
        description = QLabel("Журнал действий с запросами")
        description.setStyleSheet("color:#64748B; font-size:13px; font-weight:500;")
        title_layout.addWidget(title)
        title_layout.addWidget(description)

        title_container = QWidget()
        title_container.setObjectName('titleContainer')
        title_container.setStyleSheet('QWidget#titleContainer { background-color: transparent; }')
        title_container.setLayout(title_layout)
        title_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(title_container, 1)

        clear_button = QPushButton("Очистить историю")
        clear_button.setMinimumHeight(42)
        clear_button.setStyleSheet(
            """
            QPushButton {
                background-color: #FFFFFF; color: #B42318; border: 1px solid #FECACA;
                border-radius: 9px; padding: 10px 18px; font-size: 13px; font-weight: 600;
            }
            QPushButton:hover { background-color: #FFF7F6; border-color: #FDA29B; }
            QPushButton:pressed { background-color: #FEE4E2; }
            """
        )
        clear_button.clicked.connect(self._clear_history)
        layout.addWidget(clear_button)
        return container

    def _build_search_card(self):
        card = create_card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 15, 18, 15)

        title = QLabel("Поиск по истории")
        title.setStyleSheet("font-size: 13px; font-weight: 700; color: #0F172A;")
        layout.addWidget(title)

        row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск по действию или описанию...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMinimumHeight(38)
        self.search_input.setStyleSheet(
            """
            QLineEdit {
                background-color: #FFFFFF; color: #101828; border: 1px solid #D0D5DD;
                border-radius: 9px; padding: 0 12px; font-size: 13px;
            }
            QLineEdit:focus { border: 1px solid #2E90FA; }
            """
        )
        self.search_input.textChanged.connect(self._search_timer.start)
        row.addWidget(self.search_input, 1)

        self.search_count = QLabel("Записей: 0")
        self.search_count.setStyleSheet("color: #667085; font-size: 12px; font-weight: 600;")
        row.addWidget(self.search_count)

        reset = QPushButton("Сбросить")
        reset.setMinimumHeight(38)
        reset.setStyleSheet(
            """
            QPushButton {
                background-color: #F2F4F7; color: #475467; border: none;
                border-radius: 9px; padding: 0 16px; font-size: 12px; font-weight: 600;
            }
            QPushButton:hover { background-color: #E4E7EC; }
            """
        )
        reset.clicked.connect(self.search_input.clear)
        row.addWidget(reset)
        layout.addLayout(row)
        return card

    def _build_history_card(self):
        card = create_card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Дата и время", "Действие", "Описание"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setStyleSheet(
            """
            QTableWidget {
                background-color: #FFFFFF; alternate-background-color: #FBFDFF;
                border: 1px solid #E4E7EC; border-radius: 12px; gridline-color: transparent;
                font-size: 13px; outline: none; selection-background-color: #EAF2FF;
                selection-color: #101828;
            }
            QTableWidget::item { padding: 12px 10px; border-bottom: 1px solid #F2F4F7; }
            QTableWidget::item:selected { background-color: #EAF2FF; color: #101828; }
            QTableWidget::item:hover { background-color: #F4F8FE; }
            QHeaderView::section {
                background-color: #F8FAFC; color: #475569; border: none;
                border-bottom: 1px solid #E4E7EC; padding: 13px 10px;
                font-size: 11px; font-weight: 700;
            }
            """
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(self.show_history_details)
        layout.addWidget(self.table)
        return card

    @staticmethod
    def _style_secondary_button(button):
        button.setMinimumHeight(42)
        button.setStyleSheet(
            """
            QPushButton {
                background-color: #FFFFFF; color: #344054; border: 1px solid #D0D5DD;
                border-radius: 9px; padding: 10px 18px; font-size: 13px; font-weight: 600;
            }
            QPushButton:hover { background-color: #F9FAFB; border-color: #98A2B3; }
            QPushButton:pressed { background-color: #F2F4F7; }
            """
        )

    @staticmethod
    def _format_size(size_bytes):
        size = float(size_bytes)
        for unit in ("Б", "КБ", "МБ", "ГБ"):
            if size < 1024 or unit == "ГБ":
                return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size_bytes} Б"

    def _confirm_clear_history(self, history_count, trash_stats):
        dialog = QDialog(self)
        dialog.setWindowTitle("Очистка истории и корзины")
        dialog.setFixedWidth(520)
        dialog.setStyleSheet(
            """
            QDialog { background-color: #FFFFFF; }
            QLabel { background: transparent; color: #101828; }
            """
        )

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(16)

        top = QHBoxLayout()
        top.setSpacing(14)
        icon = QLabel("🗑️")
        icon.setFixedSize(46, 46)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(
            "background-color: #FEF3F2; border-radius: 23px; font-size: 20px;"
        )
        top.addWidget(icon)

        text_box = QVBoxLayout()
        text_box.setSpacing(4)
        title = QLabel("Очистить историю и корзину?")
        title.setStyleSheet("font-size: 17px; font-weight: 800;")
        subtitle = QLabel(
            "Будут безвозвратно удалены записи истории и файлы, "
            "сохранённые для восстановления старых версий."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("font-size: 12px; color: #475467;")
        text_box.addWidget(title)
        text_box.addWidget(subtitle)
        top.addLayout(text_box, 1)
        layout.addLayout(top)

        summary = QFrame()
        summary.setStyleSheet(
            "QFrame { background-color: #F8FAFC; border: 1px solid #EAECF0; border-radius: 10px; }"
        )
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(16, 14, 16, 14)
        summary_layout.setSpacing(7)
        rows = (
            ("Записей истории", str(history_count)),
            ("Файлов в корзине", str(trash_stats.file_count)),
            ("Освободится места", self._format_size(trash_stats.size_bytes)),
        )
        for label_text, value_text in rows:
            row = QHBoxLayout()
            label = QLabel(label_text)
            label.setStyleSheet("color: #667085; font-size: 12px;")
            value = QLabel(value_text)
            value.setStyleSheet("font-weight: 700; font-size: 12px;")
            row.addWidget(label)
            row.addStretch()
            row.addWidget(value)
            summary_layout.addLayout(row)
        layout.addWidget(summary)

        warning = QLabel(
            "Текущие запросы и прикреплённые к ним документы останутся без изменений. "
            "После очистки восстановить удалённые запросы и старые PDF-версии будет невозможно."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(
            "background-color: #FFF7ED; border: 1px solid #FED7AA; border-radius: 8px; "
            "padding: 10px 12px; color: #9A3412; font-size: 12px;"
        )
        layout.addWidget(warning)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Отмена")
        self._style_secondary_button(cancel)
        cancel.clicked.connect(dialog.reject)
        clear = QPushButton("Очистить историю и корзину")
        clear.setMinimumHeight(42)
        clear.setStyleSheet(
            """
            QPushButton { background-color: #D92D20; color: white; border: none;
                border-radius: 9px; padding: 0 18px; font-weight: 700; font-size: 13px; }
            QPushButton:hover { background-color: #B42318; }
            QPushButton:pressed { background-color: #912018; }
            """
        )
        clear.clicked.connect(dialog.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(clear)
        layout.addLayout(buttons)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _clear_history(self):
        try:
            trash_stats = self.request_service.get_trash_stats()
            history_count = self.request_service.get_history_count()
        except Exception as error:
            QMessageBox.critical(
                self, "Ошибка", f"Не удалось получить сведения о хранилище:\n{error}"
            )
            return

        if not self._confirm_clear_history(history_count, trash_stats):
            return

        try:
            removed = self.request_service.clear_history()
            self.load_history()
            self.toast_requested.emit(
                "История очищена",
                f"Удалено файлов: {removed.file_count} · освобождено {self._format_size(removed.size_bytes)}",
                "success",
            )
        except Exception as error:
            QMessageBox.critical(
                self, "Ошибка", f"Не удалось очистить историю и корзину:\n{error}"
            )

    def load_history(self):
        history = self.request_service.get_history()
        search_cache: list[str] = []
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(history))
            for row, record in enumerate(history):
                created_at = record["created_at"] or ""
                action = record["action"] or ""
                raw_description = record["description"] or ""

                date_item = QTableWidgetItem(str(created_at))
                date_item.setData(Qt.ItemDataRole.UserRole + 1, record["id"])
                self.table.setItem(row, 0, date_item)

                action_text = format_history_action(action)
                action_item = QTableWidgetItem(action_text)
                color = _ACTION_COLORS.get(action)
                if color:
                    action_item.setForeground(QBrush(QColor(color)))
                self.table.setItem(row, 1, action_item)

                display_description = format_history_description(raw_description)
                description_item = QTableWidgetItem(display_description)
                description_item.setData(Qt.ItemDataRole.UserRole, raw_description)
                description_item.setToolTip(display_description)
                self.table.setItem(row, 2, description_item)

                search_cache.append(
                    f"{created_at} {action_text} {display_description}".casefold()
                )
        finally:
            self.table.setUpdatesEnabled(True)
        self._search_cache = search_cache
        self.filter_history(self.search_input.text())

    def _apply_search_filter(self):
        self.filter_history(self.search_input.text())

    def filter_history(self, text):
        needle = (text or "").strip().casefold()
        visible_count = 0
        for row, searchable in enumerate(self._search_cache):
            match = not needle or needle in searchable
            self.table.setRowHidden(row, not match)
            visible_count += int(match)
        self.search_count.setText(f"Записей: {visible_count}")

    def show_history_details(self, row, _column):
        date_item = self.table.item(row, 0)
        action_item = self.table.item(row, 1)
        description_item = self.table.item(row, 2)

        description_text = ""
        if description_item is not None:
            description_text = (
                description_item.data(Qt.ItemDataRole.UserRole)
                or description_item.text()
                or ""
            )

        history_id = date_item.data(Qt.ItemDataRole.UserRole + 1) if date_item else None
        dialog = HistoryDetailsDialog(
            date_text=date_item.text() if date_item else "",
            action_text=action_item.text() if action_item else "",
            description_text=description_text,
            history_id=history_id,
            restore_handler=self._restore_history,
            parent=self,
        )
        dialog.exec()

    def _restore_history(self, history_id):
        self.request_service.restore_request_from_history(history_id)
        self.load_history()
        self.request_restored.emit()
