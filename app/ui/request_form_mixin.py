from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDateEdit,
    QFileDialog,
    QMessageBox,
    QTextEdit,
)

from app.models import Request
from app.ui.dialogs.incomplete_data_dialog import IncompleteDataDialog
from app.ui.date_controls import SmartDateEdit


class RequestFormMixin:
    def load_request(self):
        date = QDate.fromString(self.request.received_date, "dd.MM.yyyy")
        self.received_date.setDate(
            date if date.isValid() else QDate.currentDate())

        due = QDate.fromString(self.request.due_date, "dd.MM.yyyy")
        self.due_date.setDate(due if due.isValid() else self.due_date.minimumDate())
        completed = QDate.fromString(self.request.completed_date, "dd.MM.yyyy")
        self.completed_date.setDate(
            completed if completed.isValid() else self.completed_date.minimumDate()
        )

        self.from_department.setText(self.request.from_department)
        self.from_position.setText(self.request.from_position)
        self.from_person.setText(self.request.from_person)
        self.to_person.setText(self.request.to_person)
        for w in (self.from_department, self.from_position, self.from_person, self.to_person):
            if hasattr(w, "setCursorPosition"):
                w.setCursorPosition(0)
        self.request_subject.setPlainText(self.request.request_subject)

        index = self.status.findText(self.request.status)
        if index >= 0:
            self.status.setCurrentIndex(index)
        self.completed_date.setEnabled(self.status.currentText() == "Исполнен")

        self.note.setPlainText(self.request.note)

        if self.request.source_document:
            name = Path(self.request.source_document).name
            metrics = self.document_label.fontMetrics()
            elided = metrics.elidedText(
                name, Qt.TextElideMode.ElideMiddle, 250)
            self.document_label.setText(elided)
            self.document_label.setToolTip(name)
            self.document_label.setStyleSheet(
                "color: #027A48; font-weight: 600; border: none; background: transparent;"
            )

        self._update_pdf_actions_state()
        self._loaded = True
        self._is_dirty = False

    def highlight_empty_fields(self):
        fields = [
            self.received_date,
            self.from_department,
            self.from_person,
            self.to_person,
            self.request_subject,
        ]
        for field in fields:
            value = (not field.is_empty()) if isinstance(field, SmartDateEdit) else (
                field.date().isValid() if isinstance(field, QDateEdit) else bool(
                field.toPlainText().strip()
                if isinstance(field, QTextEdit)
                else field.text().strip()
                )
            )
            if not value:
                field.setStyleSheet(
                    "border: 1px solid #F79009; background-color: #FFFAEB;"
                )

    def _clear_field_warning(self, field):
        if field is None:
            return

        if isinstance(field, QTextEdit):
            text = field.toPlainText().strip()
        elif isinstance(field, QComboBox):
            text = field.currentText().strip()
        elif hasattr(field, "text"):
            text = field.text().strip()
        else:
            text = ""

        if isinstance(field, SmartDateEdit):
            return
        if text or isinstance(field, (QComboBox, QDateEdit)):
            field.setStyleSheet("")

    def validate_and_accept(self):
        if (
            self.due_date.date() != self.due_date.minimumDate()
            and self.due_date.date() < self.received_date.date()
        ):
            QMessageBox.warning(
                self,
                "Некорректный срок",
                "Срок исполнения не может быть раньше даты поступления.",
            )
            return

        if (
            self.completed_date.date() != self.completed_date.minimumDate()
            and self.completed_date.date() < self.received_date.date()
        ):
            QMessageBox.warning(
                self,
                "Некорректная дата исполнения",
                "Дата исполнения не может быть раньше даты поступления.",
            )
            return

        missing = []
        if not self.from_department.text().strip():
            missing.append("Отдел")
        if not self.from_person.text().strip():
            missing.append("От кого")
        if not self.to_person.text().strip():
            missing.append("На кого")
        if not self.request_subject.toPlainText().strip():
            missing.append("Суть запроса")

        if missing:
            self.highlight_empty_fields()
            dialog = IncompleteDataDialog(missing, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return

        self._is_dirty = False
        self.accept()

    def delete_request(self):
        if self.request is None:
            return

        answer = QMessageBox.question(
            self,
            "Удаление запроса",
            f"Удалить запрос #{self.request.id}?\n\nВы сможете восстановить его через раздел «История».",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.done(getattr(self, "DELETE_RESULT", 2))

    def attach_pdf(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите PDF", "", "PDF (*.pdf)"
        )
        if not path:
            return

        try:
            source = Path(path)
            if not source.exists() or not source.is_file():
                raise FileNotFoundError(source)

            # Do not copy here. The file is persisted only after the user
            # confirms the dialog and RequestService commits the request.
            self.selected_pdf = str(source.resolve())
            name = source.name
            elided = self.document_label.fontMetrics().elidedText(
                name, Qt.TextElideMode.ElideMiddle, 250
            )
            self.document_label.setText(elided)
            self.document_label.setToolTip(name)
            self.document_label.setStyleSheet(
                "color: #027A48; font-weight: 600; border: none; background: transparent;"
            )
            self.detach_pdf_requested = False
            self._update_pdf_actions_state()
            self._is_dirty = True
            self.open_pdf()
        except OSError as error:
            QMessageBox.critical(
                self, "Ошибка", f"Не удалось открыть PDF:\n\n{error}"
            )

    def detach_pdf(self):
        answer = QMessageBox.question(
            self, "Открепить", "Открепить PDF от этого запроса?"
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.detach_pdf_requested = True
            self.selected_pdf = None
            self.document_label.setText("PDF не прикреплён")
            self.document_label.setToolTip("")
            self.document_label.setStyleSheet(
                "color: #667085; font-size: 12px; border: none; background: transparent;"
            )
            self.toggle_pdf_viewer(False)
            self._update_pdf_actions_state()
            self._is_dirty = True

    def _confirm_discard(self) -> bool:
        if not getattr(self, "_is_dirty", False):
            return True

        box = QMessageBox(self)
        box.setWindowTitle("Несохранённые изменения")
        box.setText("В карточке есть несохранённые данные.")
        box.setInformativeText("Закрыть окно без сохранения?")
        discard = box.addButton(
            "Сбросить и закрыть", QMessageBox.ButtonRole.DestructiveRole
        )
        keep = box.addButton("Остаться", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        return box.clickedButton() == discard

    def reject(self):
        if self._confirm_discard():
            super().reject()

    def closeEvent(self, event):
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()

    def open_pdf(self):
        if hasattr(self, "pdf_container") and self.pdf_container.isVisible():
            self.toggle_pdf_viewer(False)
            return

        pdf_path = None
        if getattr(self, "selected_pdf", None):
            candidate = Path(self.selected_pdf)
            if candidate.exists():
                pdf_path = candidate

        if pdf_path is None and self.request and self.request.source_document:
            candidate = Path(self.request.source_document)
            if candidate.exists():
                pdf_path = candidate
            elif self.documents_dir:
                fallback = self.documents_dir / candidate.name
                if fallback.exists():
                    pdf_path = fallback

        if pdf_path is None or self.detach_pdf_requested:
            QMessageBox.warning(
                self, "Ошибка", "PDF-файл не прикреплен или не найден."
            )
            return

        self.toggle_pdf_viewer(True, pdf_path)

    def get_request(self):
        document = ""
        if not self.detach_pdf_requested:
            document = self.selected_pdf or (
                self.request.source_document if self.request else ""
            )

        return Request(
            id=self.request.id if self.request else None,
            received_date=self.received_date.date().toString("dd.MM.yyyy"),
            due_date=(
                ""
                if self.due_date.date() == self.due_date.minimumDate()
                else self.due_date.date().toString("dd.MM.yyyy")
            ),
            completed_date=(
                ""
                if self.completed_date.date() == self.completed_date.minimumDate()
                else self.completed_date.date().toString("dd.MM.yyyy")
            ),
            from_department=self.from_department.text().strip(),
            from_position=self.from_position.text().strip(),
            from_person=self.from_person.text().strip(),
            to_person=self.to_person.text().strip(),
            request_subject=self.request_subject.toPlainText().strip(),
            note=self.note.toPlainText().strip(),
            status=self.status.currentText(),
            source_document=document,
            is_pinned=self.pin_checkbox.isChecked(),
        )
