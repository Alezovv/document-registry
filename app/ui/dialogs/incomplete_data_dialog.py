from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)


class IncompleteDataDialog(QDialog):
    """Friendly confirmation shown when optional-but-important fields are empty."""

    def __init__(self, missing_fields, parent=None):
        super().__init__(parent)
        self.missing_fields = list(missing_fields)
        self.setWindowTitle("Проверьте данные")
        self.setModal(True)
        self.setFixedWidth(500)
        self.setStyleSheet(
            """
            QDialog {
                background-color: #FFFFFF;
            }
            QLabel {
                background: transparent;
                color: #101828;
            }
            """
        )
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 22)
        root.setSpacing(18)

        header = QHBoxLayout()
        header.setSpacing(14)

        icon = QLabel("!")
        icon.setFixedSize(46, 46)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(
            """
            QLabel {
                background-color: #FFFAEB;
                color: #B54708;
                border: 1px solid #FEDF89;
                border-radius: 23px;
                font-size: 22px;
                font-weight: 800;
            }
            """
        )
        header.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        title_box = QVBoxLayout()
        title_box.setSpacing(5)

        title = QLabel("Некоторые данные не заполнены")
        title.setStyleSheet("font-size: 17px; font-weight: 800; color: #101828;")

        subtitle = QLabel(
            "Запрос можно сохранить и сейчас, но лучше проверить поля ниже — "
            "они помогают быстрее находить и обрабатывать записи."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("font-size: 12px; color: #667085; line-height: 135%;")

        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        root.addLayout(header)

        card = QFrame()
        card.setStyleSheet(
            """
            QFrame {
                background-color: #F9FAFB;
                border: 1px solid #EAECF0;
                border-radius: 10px;
            }
            """
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(9)

        card_title = QLabel("Стоит заполнить:")
        card_title.setStyleSheet("font-size: 12px; font-weight: 700; color: #475467;")
        card_layout.addWidget(card_title)

        for field_name in self.missing_fields:
            row = QHBoxLayout()
            row.setSpacing(9)

            dot = QLabel("•")
            dot.setFixedWidth(12)
            dot.setStyleSheet("font-size: 16px; font-weight: 800; color: #F79009;")

            field = QLabel(str(field_name))
            field.setStyleSheet("font-size: 13px; font-weight: 600; color: #344054;")

            row.addWidget(dot)
            row.addWidget(field, 1)
            card_layout.addLayout(row)

        root.addWidget(card)

        hint = QLabel("Поля, требующие внимания, подсвечены в форме.")
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size: 11px; color: #667085;")
        root.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch()

        back_button = QPushButton("Вернуться к форме")
        back_button.setFixedHeight(40)
        back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        back_button.setStyleSheet(
            """
            QPushButton {
                background-color: #FFFFFF;
                color: #344054;
                border: 1px solid #D0D5DD;
                border-radius: 9px;
                padding: 0 17px;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton:hover { background-color: #F9FAFB; border-color: #98A2B3; }
            QPushButton:pressed { background-color: #F2F4F7; }
            """
        )
        back_button.clicked.connect(self.reject)

        save_button = QPushButton("Сохранить всё равно")
        save_button.setFixedHeight(40)
        save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        save_button.setStyleSheet(
            """
            QPushButton {
                background-color: #2563EB;
                color: #FFFFFF;
                border: none;
                border-radius: 9px;
                padding: 0 18px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton:hover { background-color: #1D4ED8; }
            QPushButton:pressed { background-color: #1E40AF; }
            """
        )
        save_button.clicked.connect(self.accept)

        back_button.setDefault(True)
        back_button.setFocus()

        buttons.addWidget(back_button)
        buttons.addWidget(save_button)
        root.addLayout(buttons)
