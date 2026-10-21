from html import escape

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.ui.history_utils import parse_history_diff


class HistoryDetailsDialog(QDialog):
    def __init__(
        self,
        *,
        date_text,
        action_text,
        description_text,
        history_id,
        restore_handler,
        can_restore: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self.date_text = date_text or ""
        self.action_text = action_text or ""
        self.description_text = description_text or ""
        self.history_id = history_id
        self.restore_handler = restore_handler
        self.can_restore = bool(can_restore and restore_handler is not None)

        self.setWindowTitle("Подробности изменения")
        self.setMinimumSize(900, 550)
        self.resize(1000, 650)
        self.setStyleSheet(
            """
            QDialog { background-color: #F8FAFC; }
            QLabel { color: #101828; }
            QTableWidget {
                background-color: #FFFFFF;
                border: 1px solid #E4E7EC;
                border-radius: 10px;
                gridline-color: #EAECF0;
                color: #101828;
                font-size: 13px;
            }
            QTableWidget::item { padding: 10px; }
            QHeaderView::section {
                background-color: #F9FAFB;
                color: #475467;
                border: none;
                border-bottom: 1px solid #E4E7EC;
                padding: 10px;
                font-size: 12px;
                font-weight: 700;
            }
            """
        )
        self._build_ui()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(24, 22, 24, 22)
        main_layout.setSpacing(16)

        title = QLabel("Подробности изменения")
        title.setStyleSheet("font-size: 21px; font-weight: 800; color: #101828;")
        main_layout.addWidget(title)

        meta_card = QFrame()
        meta_card.setStyleSheet(
            "QFrame { background-color: #FFFFFF; border: 1px solid #E4E7EC; border-radius: 10px; }"
        )
        meta_layout = QGridLayout(meta_card)
        meta_layout.setContentsMargins(18, 14, 18, 14)
        meta_layout.setHorizontalSpacing(40)
        self._add_meta(meta_layout, "Дата и время", self.date_text, 0)
        self._add_meta(meta_layout, "Действие", self.action_text, 1)
        main_layout.addWidget(meta_card)

        changes = parse_history_diff(self.description_text)
        section_title = QLabel("Изменения" if changes else "Описание действия")
        section_title.setStyleSheet("font-size: 14px; font-weight: 800; color: #344054;")
        main_layout.addWidget(section_title)

        if changes:
            main_layout.addWidget(self._build_changes_table(changes), 1)
        else:
            main_layout.addWidget(self._build_description_card(), 1)

        footer = QHBoxLayout()
        footer.setSpacing(10)

        restore_button = QPushButton("Восстановить эту версию")
        restore_button.setCursor(Qt.CursorShape.PointingHandCursor)
        restore_button.setFixedHeight(38)
        restore_button.setStyleSheet(
            """
            QPushButton {
                background-color: #2563EB; color: white; border: none;
                border-radius: 8px; padding: 0 18px; font-size: 13px; font-weight: 600;
            }
            QPushButton:hover { background-color: #1D4ED8; }
            QPushButton:pressed { background-color: #1E40AF; }
            """
        )
        restore_button.clicked.connect(self._restore)

        close_button = QPushButton("Закрыть")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.setFixedHeight(38)
        close_button.setStyleSheet(
            """
            QPushButton {
                background-color: #FFFFFF; color: #344054; border: 1px solid #D0D5DD;
                border-radius: 8px; padding: 0 18px; font-size: 13px; font-weight: 600;
            }
            QPushButton:hover { background-color: #F9FAFB; border-color: #98A2B3; }
            QPushButton:pressed { background-color: #F2F4F7; }
            """
        )
        close_button.clicked.connect(self.accept)

        restore_button.setVisible(self.can_restore)
        if self.can_restore:
            footer.addWidget(restore_button)
        footer.addStretch()
        footer.addWidget(close_button)
        main_layout.addLayout(footer)

    @staticmethod
    def _add_meta(layout, label_text, value_text, column):
        label = QLabel(label_text)
        label.setStyleSheet("color: #667085; font-size: 11px; font-weight: 700;")
        value = QLabel(value_text)
        value.setStyleSheet("color: #101828; font-size: 13px; font-weight: 600;")
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label, 0, column)
        layout.addWidget(value, 1, column)

    @staticmethod
    def _build_changes_table(changes):
        table = QTableWidget()
        table.setAlternatingRowColors(True)
        table.setWordWrap(True)
        table.setTextElideMode(Qt.TextElideMode.ElideNone)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        table.verticalHeader().setVisible(False)
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(["Поле", "Было", "Стало"])
        table.setRowCount(len(changes))

        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        for row_index, (field_name, old_value, new_value) in enumerate(changes):
            field_item = QTableWidgetItem(field_name)
            field_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            field_item.setFlags(field_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row_index, 0, field_item)

            old_item = QTableWidgetItem(old_value)
            old_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            old_item.setForeground(QColor("#B42318"))
            old_item.setBackground(QColor("#FEF3F2"))
            old_item.setFlags(old_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row_index, 1, old_item)

            new_item = QTableWidgetItem(new_value)
            new_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            new_item.setForeground(QColor("#027A48"))
            new_item.setBackground(QColor("#ECFDF3"))
            new_item.setFlags(new_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row_index, 2, new_item)

        table.resizeRowsToContents()
        for row_index in range(table.rowCount()):
            table.setRowHeight(row_index, max(60, table.rowHeight(row_index)) + 16)
        return table

    def _build_description_card(self):
        card = QFrame()
        card.setStyleSheet(
            "QFrame { background-color: #FFFFFF; border: 1px solid #E4E7EC; border-radius: 10px; }"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 22, 22, 22)

        label = QLabel(self._description_html(self.description_text))
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        label.setStyleSheet("font-size: 13px; line-height: 1.5;")
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label)
        layout.addStretch()
        return card

    @staticmethod
    def _description_html(raw_text):
        raw_text = raw_text or "Изменения отсутствуют"
        try:
            if ":" in raw_text and "|" in raw_text and " -> " in raw_text:
                prefix, rest = raw_text.split(":", 1)
                dept_part, rest2 = rest.split(" · ", 1)
                sender, rest3 = rest2.split(" -> ", 1)
                target, rest4 = rest3.split(" | ", 1)
                file_name = "Без файла"
                subject = rest4
                if "[Файл:" in rest4:
                    subject, file_part = rest4.split("[Файл:", 1)
                    file_name = file_part.rstrip("]")
                return (
                    f"<div style='margin-bottom: 18px;'><b style='color: #101828; font-size: 15px;'>{escape(prefix.strip())}</b></div>"
                    "<table width='100%' cellpadding='5' cellspacing='0'>"
                    f"<tr><td width='75' style='color: #667085;'>Отдел:</td><td style='color: #101828; font-weight: 600;'>{escape(dept_part.strip(' «»'))}</td></tr>"
                    f"<tr><td style='color: #667085;'>От кого:</td><td style='color: #101828; font-weight: 600;'>{escape(sender.strip())}</td></tr>"
                    f"<tr><td style='color: #667085;'>На кого:</td><td style='color: #101828; font-weight: 600;'>{escape(target.strip())}</td></tr>"
                    f"<tr><td style='color: #667085;'>Суть:</td><td style='color: #101828; font-weight: 600;'>{escape(subject.strip(' «»'))}</td></tr>"
                    f"<tr><td style='color: #667085;'>Файл:</td><td style='color: #027A48; font-weight: 600;'>{escape(file_name.strip())}</td></tr>"
                    "</table>"
                )
        except (ValueError, TypeError):
            pass
        return escape(raw_text).replace("\n", "<br>")

    def _restore(self):
        if not self.history_id:
            QMessageBox.warning(self, "Ошибка", "Не удалось определить ID записи истории.")
            return

        reply = QMessageBox.question(
            self,
            "Подтверждение восстановления",
            "Восстановить состояние запроса из этой версии?\n\n"
            "Текущие данные будут перезаписаны, а операция будет записана в историю.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            self.restore_handler(self.history_id)
        except Exception as error:
            QMessageBox.critical(self, "Ошибка", f"Не удалось восстановить версию:\n{error}")
            return

        QMessageBox.information(self, "Готово", "Состояние запроса восстановлено.")
        self.accept()
