from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)



PRIMARY_BUTTON_STYLE = """
QPushButton {
    background-color: #2563EB;
    color: #FFFFFF;
    border: 1px solid #2563EB;
    border-radius: 8px;
    padding: 7px 18px;
    font-size: 13px;
    font-weight: 600;
    min-height: 20px;
}
QPushButton:hover {
    background-color: #1D4ED8;
    border-color: #1D4ED8;
}
QPushButton:pressed {
    background-color: #1E40AF;
    border-color: #1E40AF;
}
QPushButton:disabled {
    background-color: #E2E8F0;
    color: #94A3B8;
    border-color: #E2E8F0;
}
"""

SECONDARY_BUTTON_STYLE = """
QPushButton {
    background-color: #FFFFFF;
    color: #344054;
    border: 1px solid #D0D5DD;
    border-radius: 8px;
    padding: 7px 16px;
    font-size: 13px;
    font-weight: 600;
    min-height: 20px;
}
QPushButton:hover {
    background-color: #F8FAFC;
    border-color: #98A2B3;
    color: #1D2939;
}
QPushButton:pressed {
    background-color: #F2F4F7;
}
QPushButton:disabled {
    background-color: #F8FAFC;
    color: #94A3B8;
    border-color: #EAECF0;
}
"""

DANGER_BUTTON_STYLE = """
QPushButton {
    background-color: #FFF7F6;
    color: #B42318;
    border: 1px solid #FECDCA;
    border-radius: 8px;
    padding: 7px 16px;
    font-size: 13px;
    font-weight: 600;
    min-height: 20px;
}
QPushButton:hover {
    background-color: #FEE4E2;
    border-color: #FDA29B;
}
QPushButton:pressed {
    background-color: #FECDCA;
}
QPushButton:disabled {
    background-color: #F8FAFC;
    color: #94A3B8;
    border-color: #EAECF0;
}
"""

DIALOG_BASE_STYLE = """
QDialog { background-color: #F8FAFC; }
QLabel { color: #344054; font-size: 13px; }
QLineEdit, QPlainTextEdit {
    background-color: #FFFFFF;
    border: 1px solid #D0D5DD;
    border-radius: 8px;
    padding: 8px 12px;
    color: #101828;
    font-size: 13px;
}
QLineEdit:hover, QPlainTextEdit:hover {
    border-color: #98A2B3;
}
QLineEdit:focus, QPlainTextEdit:focus {
    border: 2px solid #84ADFF;
}
QListWidget {
    background-color: #FFFFFF;
    border: 1px solid #D0D5DD;
    border-radius: 8px;
    padding: 4px;
    outline: none;
    font-size: 13px;
}
QListWidget::item {
    padding: 8px 10px;
    border-radius: 6px;
    color: #1E293B;
}
QListWidget::item:hover {
    background-color: #F4F8FE;
}
QListWidget::item:selected {
    background-color: #EAF2FF;
    color: #0F172A;
}
QTableWidget {
    background-color: #FFFFFF;
    border: 1px solid #D0D5DD;
    border-radius: 8px;
    gridline-color: #F2F4F7;
    font-size: 13px;
}
QTableWidget::item {
    padding: 6px 8px;
}
"""


class CasePickerDialog(QDialog):
    """Search and select an existing case."""

    def __init__(self, request_service, parent=None, *, title="Привязать к делу"):
        super().__init__(parent)
        self.request_service = request_service
        self.selected_case_id: int | None = None
        self.selected_action = "link"
        self.setWindowTitle(title)
        self.resize(560, 480)
        self.setStyleSheet(DIALOG_BASE_STYLE)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Найдите дело по названию, заметке или реквизитам запроса."))
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Название, ФИО, отдел или тема…")
        self.search.textChanged.connect(self._refresh)
        layout.addWidget(self.search)
        self.results = QListWidget(self)
        self.results.itemDoubleClicked.connect(lambda _item: self._accept_selection())
        layout.addWidget(self.results, 1)

        buttons = QDialogButtonBox(parent=self)
        self.choose_button = buttons.addButton("Привязать", QDialogButtonBox.ButtonRole.AcceptRole)
        self.choose_button.clicked.connect(lambda: self._accept_selection(action="link"))
        self.open_button = buttons.addButton("Открыть дело", QDialogButtonBox.ButtonRole.ActionRole)
        self.open_button.clicked.connect(lambda: self._accept_selection(action="open"))
        self.cancel_button = buttons.addButton("Отмена", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(self.reject)

        self.choose_button.setStyleSheet(PRIMARY_BUTTON_STYLE)
        self.open_button.setStyleSheet(SECONDARY_BUTTON_STYLE)
        self.cancel_button.setStyleSheet(SECONDARY_BUTTON_STYLE)

        layout.addWidget(buttons)
        self._refresh()

    def _refresh(self):
        query = self.search.text().strip()
        rows = self.request_service.search_cases(query, limit=100) if query else self.request_service.list_cases(limit=200)
        self.results.clear()
        for case in rows:
            case_id = int(case["id"])
            count = int(case.get("request_count", 0) or 0)
            note = str(case.get("note", "") or "").strip()
            label = f"#{case_id} · {case.get('title', '')} · документов: {count}"
            if note:
                label += f"\n{note[:110]}"
            item = QListWidgetItem(label, self.results)
            item.setData(Qt.ItemDataRole.UserRole, case_id)
        has_items = self.results.count() > 0
        self.choose_button.setEnabled(has_items)
        self.open_button.setEnabled(has_items)

    def _accept_selection(self, *, action: str = "link"):
        item = self.results.currentItem()
        if item is None:
            return
        self.selected_case_id = int(item.data(Qt.ItemDataRole.UserRole))
        self.selected_action = action
        self.accept()


class RequestPickerDialog(QDialog):
    """Search and select a persisted request to add to a case."""

    def __init__(self, request_service, parent=None, *, exclude_case_id: int | None = None):
        super().__init__(parent)
        self.request_service = request_service
        self.exclude_case_id = exclude_case_id
        self.selected_request_id: int | None = None
        self.setWindowTitle("Добавить существующий запрос")
        self.resize(760, 500)
        self.setStyleSheet(DIALOG_BASE_STYLE)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Поиск по ID, номеру документа, отправителю, ФИО или теме."))
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Например, #123, номер документа, отдел или фамилия…")
        self.search.textChanged.connect(self._refresh)
        layout.addWidget(self.search)
        self.results = QListWidget(self)
        self.results.itemDoubleClicked.connect(lambda _item: self._accept_selection())
        layout.addWidget(self.results, 1)

        buttons = QDialogButtonBox(parent=self)
        self.choose_button = buttons.addButton("Добавить запрос", QDialogButtonBox.ButtonRole.AcceptRole)
        self.choose_button.clicked.connect(self._accept_selection)
        self.cancel_button = buttons.addButton("Отмена", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(self.reject)

        self.choose_button.setStyleSheet(PRIMARY_BUTTON_STYLE)
        self.cancel_button.setStyleSheet(SECONDARY_BUTTON_STYLE)

        layout.addWidget(buttons)

    def _refresh(self):
        query = self.search.text().strip()
        self.results.clear()
        if len(query) < 1:
            self.choose_button.setEnabled(False)
            return
        requests = self.request_service.search_requests_for_case_linking(
            query, exclude_case_id=self.exclude_case_id, limit=100
        )
        for request in requests:
            parts = [f"#{request.id}", request.received_date]
            if request.from_department:
                parts.append(request.from_department)
            if request.from_person:
                parts.append(request.from_person)
            if request.to_person:
                parts.append(f"На кого: {request.to_person}")
            if request.request_subject:
                parts.append(request.request_subject.replace("\n", " ")[:100])
            parts.append(f"Статус: {request.display_status()}")
            item = QListWidgetItem(" · ".join(parts), self.results)
            item.setData(Qt.ItemDataRole.UserRole, int(request.id))
        self.choose_button.setEnabled(self.results.count() > 0)

    def _accept_selection(self):
        item = self.results.currentItem()
        if item is None:
            return
        self.selected_request_id = int(item.data(Qt.ItemDataRole.UserRole))
        self.accept()


class CaseDialog(QDialog):
    """Case overview with chronological requests and direct request hand-off."""

    OPEN_REQUEST_RESULT = 3

    def __init__(self, request_service, case_id: int, parent=None):
        super().__init__(parent)
        self.request_service = request_service
        self.case_id = int(case_id)
        self.target_request_id: int | None = None
        self._dirty = False
        self._loading = False
        self.setWindowTitle(f"Карточка дела #{self.case_id}")
        self.resize(1120, 650)
        self.setMinimumSize(820, 500)
        self.setStyleSheet(DIALOG_BASE_STYLE)

        root = QVBoxLayout(self)
        heading = QHBoxLayout()
        self.heading_label = QLabel(f"Дело #{self.case_id}", self)
        self.heading_label.setStyleSheet("font-size: 18px; font-weight: 700; color: #101828;")
        heading.addWidget(self.heading_label, 1)
        self.updated_label = QLabel("", self)
        self.updated_label.setStyleSheet("color: #667085; font-size: 11px;")
        self.updated_label.setWordWrap(True)
        heading.addWidget(self.updated_label)
        root.addLayout(heading)

        form = QFormLayout()
        self.title_edit = QLineEdit(self)
        self.title_edit.setPlaceholderText("Например: Судебное дело № …")
        self.note_edit = QPlainTextEdit(self)
        self.note_edit.setPlaceholderText("Заметка по делу (необязательно)")
        self.note_edit.setMaximumHeight(70)
        form.addRow("Название дела", self.title_edit)
        form.addRow("Заметка", self.note_edit)
        root.addLayout(form)
        self.title_edit.textChanged.connect(self._mark_dirty)
        self.note_edit.textChanged.connect(self._mark_dirty)

        self.summary = QLabel("", self)
        self.summary.setStyleSheet("color: #475467; padding: 4px 0;")
        root.addWidget(self.summary)

        self.table = QTableWidget(0, 9, self)
        self.table.setHorizontalHeaderLabels([
            "ID", "Дата документа", "Номер", "Отправитель", "На кого",
            "Суть запроса", "Срок", "Статус", "PDF",
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.itemDoubleClicked.connect(lambda _item: self._open_selected_request())
        root.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.add_button = QPushButton("+ Добавить существующий запрос", self)
        self.add_button.clicked.connect(self._add_request)
        self.remove_button = QPushButton("Убрать из дела", self)
        self.remove_button.clicked.connect(self._remove_selected_request)
        self.delete_button = QPushButton("Удалить дело", self)
        self.delete_button.clicked.connect(self._delete_case)
        self.open_button = QPushButton("Открыть запрос", self)
        self.open_button.setDefault(True)
        self.open_button.clicked.connect(self._open_selected_request)

        self.add_button.setStyleSheet(SECONDARY_BUTTON_STYLE)
        self.remove_button.setStyleSheet(SECONDARY_BUTTON_STYLE)
        self.delete_button.setStyleSheet(DANGER_BUTTON_STYLE)
        self.open_button.setStyleSheet(PRIMARY_BUTTON_STYLE)

        actions.addWidget(self.add_button)
        actions.addWidget(self.remove_button)
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        actions.addWidget(self.open_button)
        root.addLayout(actions)

        footer = QDialogButtonBox(parent=self)
        self.save_button = footer.addButton("Сохранить название и заметку", QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.clicked.connect(self._save_case)
        self.close_button = footer.addButton("Закрыть", QDialogButtonBox.ButtonRole.RejectRole)
        footer.rejected.connect(self.reject)

        self.save_button.setStyleSheet(PRIMARY_BUTTON_STYLE)
        self.close_button.setStyleSheet(SECONDARY_BUTTON_STYLE)

        root.addWidget(footer)
        self._refresh(reset_fields=True)

    def _mark_dirty(self, *_args):
        if not self._loading:
            self._dirty = True

    def _refresh(self, *, reset_fields: bool = False):
        data = self.request_service.get_case_details(self.case_id)
        if data is None:
            QMessageBox.warning(self, "Дело не найдено", "Карточка дела была удалена или больше недоступна.")
            self.done(QDialog.DialogCode.Rejected)
            return
        self._data = data
        if reset_fields:
            self._loading = True
            self.title_edit.setText(str(data.get("title", "") or ""))
            self.note_edit.setPlainText(str(data.get("note", "") or ""))
            self._loading = False
            self._dirty = False
        self.heading_label.setText(f"Дело #{self.case_id} · {data.get('title', '')}")
        self.updated_label.setText(
            f"Создано: {data.get('created_at', '') or '—'}\n"
            f"Обновлено: {data.get('updated_at', '') or '—'}"
        )
        status_counts = data.get("status_counts", {})
        summary = [f"Документов: {int(data.get('request_count', 0) or 0)}"]
        if status_counts:
            summary.extend(f"{name}: {count}" for name, count in sorted(status_counts.items()))
        self.summary.setText(" · ".join(summary))

        members = data.get("requests", [])
        self.table.setRowCount(len(members))
        for row_index, member in enumerate(members):
            request = member["request"]
            values = [
                f"#{request.id}",
                request.received_date or "—",
                member.get("document_number") or "—",
                request.from_department or request.from_person or "—",
                request.to_person or "—",
                (request.request_subject or "").replace("\n", " ") or "—",
                request.due_date or "—",
                request.display_status(),
                "Есть" if str(request.source_document or "").strip() else "—",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, int(request.id))
                if column in (0, 1, 2, 6, 7, 8):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row_index, column, item)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(5, max(220, self.table.width() // 3))
        self.remove_button.setEnabled(bool(members))
        self.open_button.setEnabled(bool(members))

    def _save_case(self) -> bool:
        if not self._dirty:
            return True
        try:
            self.request_service.update_case(
                self.case_id, self.title_edit.text(), self.note_edit.toPlainText()
            )
        except Exception as error:
            QMessageBox.critical(self, "Ошибка сохранения", f"Не удалось сохранить дело:\n\n{error}")
            return False
        self._refresh(reset_fields=True)
        return True

    def _confirm_case_changes(self) -> bool:
        if not self._dirty:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Несохранённые изменения дела")
        box.setText("Название или заметка дела были изменены.")
        save = box.addButton("Сохранить и продолжить", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Продолжить без сохранения", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save)
        box.exec()
        clicked = box.clickedButton()
        if clicked == save:
            return self._save_case()
        if clicked == discard:
            self._refresh(reset_fields=True)
            return True
        return False

    def _selected_request_id(self) -> int | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return int(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _open_selected_request(self):
        request_id = self._selected_request_id()
        if request_id is None:
            return
        if not self._confirm_case_changes():
            return
        self.target_request_id = request_id
        self.done(self.OPEN_REQUEST_RESULT)

    def _add_request(self):
        picker = RequestPickerDialog(self.request_service, self, exclude_case_id=self.case_id)
        if picker.exec() != QDialog.DialogCode.Accepted or picker.selected_request_id is None:
            return
        try:
            added = self.request_service.add_request_to_case(self.case_id, picker.selected_request_id)
            if added:
                self._refresh(reset_fields=False)
        except Exception as error:
            QMessageBox.critical(self, "Ошибка привязки", f"Не удалось добавить запрос в дело:\n\n{error}")

    def _remove_selected_request(self):
        request_id = self._selected_request_id()
        if request_id is None:
            return
        answer = QMessageBox.question(
            self,
            "Убрать запрос из дела",
            f"Убрать запрос #{request_id} из этого дела?\n\nСама запись и PDF останутся в реестре.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            remaining = self.request_service.remove_request_from_case(self.case_id, request_id)
        except Exception as error:
            QMessageBox.critical(self, "Ошибка", f"Не удалось убрать запрос из дела:\n\n{error}")
            return
        if remaining == 0:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Пустое дело")
            box.setText("В деле больше нет связанных запросов.")
            keep = box.addButton("Оставить пустое дело", QMessageBox.ButtonRole.AcceptRole)
            delete = box.addButton("Удалить дело", QMessageBox.ButtonRole.DestructiveRole)
            box.setDefaultButton(keep)
            box.exec()
            if box.clickedButton() == delete:
                try:
                    self.request_service.delete_case(self.case_id)
                except Exception as error:
                    QMessageBox.critical(self, "Ошибка удаления", f"Не удалось удалить дело:\n\n{error}")
                    self._refresh(reset_fields=False)
                    return
                self.accept()
                return
        self._refresh(reset_fields=False)

    def _delete_case(self):
        answer = QMessageBox.question(
            self,
            "Удалить дело",
            f"Удалить дело «{self.title_edit.text().strip()}»?\n\nВсе связи будут удалены, но сами запросы и PDF останутся в реестре.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.request_service.delete_case(self.case_id)
        except Exception as error:
            QMessageBox.critical(self, "Ошибка удаления", f"Не удалось удалить дело:\n\n{error}")
            return
        self.accept()

    def reject(self):
        if self._confirm_case_changes():
            super().reject()

    def closeEvent(self, event):
        if self._confirm_case_changes():
            event.accept()
        else:
            event.ignore()
