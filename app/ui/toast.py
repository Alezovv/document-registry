from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QTimer, Qt
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)


class ToastNotification(QFrame):
    """Lightweight non-modal notification with automatic stacking."""

    def __init__(self, parent, title, message, kind="success", duration=3600):
        super().__init__(parent)
        self.duration = duration
        self.title_text = str(title)
        self.message_text = str(message)
        self._hiding = False

        colors = {
            "success": {"accent": "#16A34A", "soft": "#F0FDF4", "icon": "✓"},
            "error": {"accent": "#DC2626", "soft": "#FEF2F2", "icon": "×"},
            "warning": {"accent": "#D97706", "soft": "#FFFBEB", "icon": "!"},
            "info": {"accent": "#2563EB", "soft": "#EFF6FF", "icon": "i"},
        }
        style = colors.get(kind, colors["info"])

        self.setObjectName("toast")
        self.setFixedWidth(390)
        self.setStyleSheet(
            f"""
            QFrame#toast {{
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-left: 4px solid {style['accent']};
                border-radius: 12px;
            }}
            """
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(13, 11, 10, 11)
        layout.setSpacing(11)

        icon = QLabel(style["icon"])
        icon.setFixedSize(30, 30)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(
            f"color:{style['accent']}; background:{style['soft']}; border:none; "
            "border-radius:15px; font-size:15px; font-weight:800;"
        )
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        title_label = QLabel(self.title_text)
        title_label.setStyleSheet(
            "color:#0F172A; font-size:13px; font-weight:750; border:none;"
        )
        message_label = QLabel(self.message_text)
        message_label.setWordWrap(True)
        message_label.setStyleSheet(
            "color:#64748B; font-size:12px; line-height:1.3; border:none;"
        )
        text_layout.addWidget(title_label)
        text_layout.addWidget(message_label)
        layout.addLayout(text_layout, 1)

        close = QPushButton("×")
        close.setToolTip("Закрыть уведомление")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setFixedSize(24, 24)
        close.setStyleSheet(
            "QPushButton { background:transparent; color:#94A3B8; border:none; "
            "font-size:17px; font-weight:600; padding:0; }"
            "QPushButton:hover { color:#475569; background:#F1F5F9; border-radius:6px; }"
        )
        close.clicked.connect(self.hide_toast)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)

        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.opacity_effect)
        self.opacity_effect.setOpacity(0.0)

        self.fade_in = QPropertyAnimation(self.opacity_effect, b"opacity", self)
        self.fade_in.setDuration(120)
        self.fade_in.setStartValue(0.0)
        self.fade_in.setEndValue(1.0)
        self.fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)

        self.fade_out = QPropertyAnimation(self.opacity_effect, b"opacity", self)
        self.fade_out.setDuration(150)
        self.fade_out.setStartValue(1.0)
        self.fade_out.setEndValue(0.0)
        self.fade_out.setEasingCurve(QEasingCurve.Type.InCubic)
        self.fade_out.finished.connect(self._finish_hide)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide_toast)

    @staticmethod
    def _active(parent) -> list["ToastNotification"]:
        active = getattr(parent, "_toast_notifications", None)
        if active is None:
            active = []
            setattr(parent, "_toast_notifications", active)
        return active

    @classmethod
    def _reposition(cls, parent) -> None:
        active = [toast for toast in cls._active(parent) if not toast._hiding]
        setattr(parent, "_toast_notifications", active)
        bottom = parent.height() - 24
        for toast in reversed(active):
            toast.adjustSize()
            toast.move(parent.width() - toast.width() - 24, bottom - toast.height())
            bottom -= toast.height() + 10

    def show_toast(self):
        parent = self.parentWidget()
        if parent is None:
            return

        active = self._active(parent)
        # Avoid stacking identical messages caused by repeated refresh signals.
        for toast in list(active):
            if (
                not toast._hiding
                and toast.title_text == self.title_text
                and toast.message_text == self.message_text
            ):
                toast._timer.start(self.duration)
                self.deleteLater()
                return

        active.append(self)
        self.adjustSize()
        self._reposition(parent)
        self.raise_()
        self.show()
        self.fade_in.start()
        self._timer.start(self.duration)

    def hide_toast(self):
        if self._hiding:
            return
        self._hiding = True
        self._timer.stop()
        self.fade_out.start()

    def _finish_hide(self):
        parent = self.parentWidget()
        if parent is not None:
            active = self._active(parent)
            if self in active:
                active.remove(self)
            self._reposition(parent)
        self.deleteLater()
