from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from app.ui.styles import BORDER_COLOR, CARD_COLOR


def create_card():
    card = QFrame()
    card.setObjectName("card")
    card.setStyleSheet(
        f"""
        QFrame#card {{
            background-color: {CARD_COLOR};
            border: 1px solid {BORDER_COLOR};
            border-radius: 14px;
        }}
        """
    )
    return card


def create_stat_card(title, value, color):
    card = QFrame()
    card.setObjectName("statCard")
    card.setProperty("active", False)
    card.setFixedHeight(64)
    card.setCursor(Qt.CursorShape.PointingHandCursor)
    card.setStyleSheet(f"""
        QFrame#statCard {{
            background-color: #FFFFFF;
            border: 1px solid #E2E8F0;
            border-left: 3px solid {color};
            border-radius: 11px;
        }}
        QFrame#statCard:hover {{
            border: 1px solid #CBD5E1;
            border-left: 3px solid {color};
            background-color: #FBFDFF;
        }}
        QFrame#statCard[active="true"] {{
            background-color: #F8FBFF;
            border: 1px solid {color};
            border-left: 4px solid {color};
        }}
    """)

    layout = QVBoxLayout(card)
    layout.setContentsMargins(14, 8, 14, 7)
    layout.setSpacing(2)

    top_layout = QHBoxLayout()
    top_layout.setSpacing(8)
    title_label = QLabel(title.upper())
    title_label.setStyleSheet("""
        color: #64748B;
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 0.45px;
        border: none;
    """)

    dot = QLabel("●")
    dot.setStyleSheet(f"color: {color}; font-size: 8px; border:none;")

    top_layout.addWidget(title_label)
    top_layout.addStretch()
    top_layout.addWidget(dot)
    layout.addLayout(top_layout)

    value_label = QLabel(str(value))
    value_label.setStyleSheet("""
        color: #0F172A;
        font-size: 20px;
        font-weight: 750;
        letter-spacing: -0.4px;
        border: none;
    """)
    layout.addWidget(value_label)

    return card, value_label

