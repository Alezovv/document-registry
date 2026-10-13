from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from app import __version__
from app.ui.styles import SIDEBAR_COLOR


class NavigationSidebar(QFrame):
    page_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(224)
        self.setStyleSheet(
            f"QFrame {{ background-color: {SIDEBAR_COLOR}; border: none; }}"
        )
        self._buttons: dict[str, QPushButton] = {}
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 20, 12, 14)
        layout.setSpacing(6)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        badge = QLabel()
        badge.setFixedSize(36, 36)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        app = QApplication.instance()
        icon = app.windowIcon() if app is not None else None
        if icon is not None and not icon.isNull():
            badge.setPixmap(icon.pixmap(34, 34))
            badge.setStyleSheet("background:transparent; border:none;")
        else:
            badge.setText("R")
            badge.setStyleSheet(
                "background:#2563EB; color:white; border-radius:9px; "
                "font-size:16px; font-weight:800;"
            )
        brand_text = QVBoxLayout()
        brand_text.setSpacing(1)
        logo = QLabel("Реестр запросов")
        logo.setStyleSheet("color:#F8FAFC; font-size:14px; font-weight:750;")
        description = QLabel("Рабочее место")
        description.setStyleSheet("color:#7C8AA0; font-size:10px; font-weight:600;")
        brand_text.addWidget(logo)
        brand_text.addWidget(description)
        brand.addWidget(badge)
        brand.addLayout(brand_text, 1)
        layout.addLayout(brand)
        layout.addSpacing(24)

        nav_label = QLabel("НАВИГАЦИЯ")
        nav_label.setStyleSheet(
            "color:#64748B; font-size:9px; font-weight:750; letter-spacing:1px; padding-left:12px;"
        )
        layout.addWidget(nav_label)
        layout.addSpacing(2)

        for page, icon, text in (
            ("registry", "▤", "Реестр"),
            ("documents", "▱", "Документы"),
            ("history", "◷", "История"),
            ("shahmatka", "▦", "Шахматка"),
            ("settings", "⚙", "Настройки"),
        ):
            button = self._create_button(icon, text)
            button.clicked.connect(
                lambda checked=False, name=page: self.page_requested.emit(name)
            )
            self._buttons[page] = button
            layout.addWidget(button)

        layout.addStretch()

        footer = QFrame()
        footer.setStyleSheet(
            "QFrame { background:#111C2C; border:1px solid #223047; border-radius:10px; }"
        )
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(12, 10, 12, 10)
        footer_layout.setSpacing(3)
        status_line = QHBoxLayout()
        status_line.setSpacing(7)
        status_dot = QLabel("●")
        status_dot.setStyleSheet("color:#34D399; font-size:9px; border:none;")
        status_text = QLabel("Система готова")
        status_text.setStyleSheet("color:#E2E8F0; font-size:10px; font-weight:650; border:none;")
        status_line.addWidget(status_dot)
        status_line.addWidget(status_text)
        status_line.addStretch()
        footer_layout.addLayout(status_line)
        version = QLabel(f"Document Registry · v{__version__}")
        version.setStyleSheet("color:#64748B; font-size:9px; border:none;")
        footer_layout.addWidget(version)
        layout.addWidget(footer)

    @staticmethod
    def _create_button(icon: str, text: str) -> QPushButton:
        button = QPushButton(f"{icon}    {text}")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setMinimumHeight(44)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return button

    def set_active(self, page: str):
        for name, button in self._buttons.items():
            button.setStyleSheet(
                self._active_style() if name == page else self._idle_style()
            )

    @staticmethod
    def _idle_style() -> str:
        return """
            QPushButton {
                background-color: transparent;
                color: #CBD5E1;
                border: 1px solid transparent;
                border-radius: 9px;
                padding: 0 14px;
                text-align: left;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton:hover { background-color: #172238; color: #FFFFFF; border-color: #22324A; }
            QPushButton:pressed { background-color: #202B40; color: white; }
        """

    @staticmethod
    def _active_style() -> str:
        return """
            QPushButton {
                background-color: #183A70;
                color: white;
                border: 1px solid #2D5DA8;
                border-left: 3px solid #60A5FA;
                border-radius: 9px;
                padding: 0 12px;
                text-align: left;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton:hover { background-color: #1E4A89; border-color: #3B82F6; }
            QPushButton:pressed { background-color: #1D4ED8; }
        """
