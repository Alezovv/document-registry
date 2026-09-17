from html import escape
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


class DeleteRequestDialog(QDialog):
    def __init__(self, requests, parent=None):
        super().__init__(parent)
        self.requests = list(requests)
        self.setWindowTitle("Удаление запроса")
        self.setFixedWidth(460)
        self.setStyleSheet("QDialog { background-color: #FFFFFF; } QLabel { background: transparent; }")
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        top = QHBoxLayout()
        top.setSpacing(14)
        icon = QLabel("🗑️")
        icon.setFixedSize(44, 44)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("QLabel { background-color: #FEE4E2; border-radius: 22px; font-size: 20px; }")
        top.addWidget(icon)

        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        count = len(self.requests)
        title = QLabel("Удалить выбранный запрос?" if count == 1 else f"Удалить выбранные запросы ({count})?")
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #101828;")
        subtitle = QLabel("Запись будет убрана из реестра. Вы сможете вернуть её через вкладку «История», пока история и корзина не очищены.")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("font-size: 12px; color: #475467;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        top.addLayout(title_box, 1)
        layout.addLayout(top)

        card = QFrame()
        card.setStyleSheet("QFrame { background-color: #F8FAFC; border: 1px solid #EAECF0; border-radius: 8px; }")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 12, 14, 12)

        if count == 1:
            req = self.requests[0]
            dep = escape(req.from_department or "Не указан")
            target = escape(req.to_person or "Не указан")
            pdf_name = escape(Path(req.source_document).name) if req.source_document else "Без файла"
            details = (
                f"<b>ID:</b> #{req.id}<br>"
                f"<b>Отдел:</b> {dep}<br>"
                f"<b>Фигурант:</b> {target}<br>"
                f"<b>Документ:</b> {pdf_name}"
            )
        else:
            details = f"<b>Выбрано записей:</b> {count} шт.<br>Все записи будут зафиксированы в журнале изменений."
        label = QLabel(details)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setStyleSheet("font-size: 12px; color: #344054;")
        card_layout.addWidget(label)
        layout.addWidget(card)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Отмена")
        delete = QPushButton("Удалить")
        for button in (cancel, delete):
            button.setFixedHeight(38)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet("QPushButton { background: white; color: #344054; border: 1px solid #D0D5DD; border-radius: 8px; padding: 0 16px; font-weight: 600; } QPushButton:hover { background: #F9FAFB; }")
        delete.setStyleSheet("QPushButton { background: #D92D20; color: white; border: none; border-radius: 8px; padding: 0 20px; font-weight: 700; } QPushButton:hover { background: #B42318; }")
        cancel.clicked.connect(self.reject)
        delete.clicked.connect(self.accept)
        cancel.setDefault(True)
        cancel.setFocus()
        buttons.addWidget(cancel)
        buttons.addWidget(delete)
        layout.addLayout(buttons)
