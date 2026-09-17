from __future__ import annotations

from datetime import date as py_date

from PySide6.QtCore import QDate, QEvent, QPoint, Qt, Signal
from PySide6.QtGui import QAction, QKeyEvent
from PySide6.QtWidgets import (
    QCalendarWidget,
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QToolButton,
    QWidget,
    QWidgetAction,
)

from app.date_input import format_date, format_date_while_typing, parse_friendly_date


EMPTY_DATE = QDate(1900, 1, 1)


class NoWheelComboBox(QComboBox):
    """Combo box that never changes its value from the mouse wheel."""

    def wheelEvent(self, event):
        event.ignore()


class NoWheelDateEdit(QDateEdit):
    """Legacy date editor kept for compatibility with older screens/tests."""

    def wheelEvent(self, event):
        event.ignore()


class SmartDateEdit(QWidget):
    """Easy date input with free typing and a calendar popup.

    Unlike QDateEdit's section-by-section editor, this control behaves like a
    normal text field. Users can paste/type dates as 17.09.2026, 17/09/26,
    17092026, 170926, or 1709. A calendar remains one click away.
    """

    dateChanged = Signal(QDate)

    def __init__(
        self,
        parent=None,
        *,
        optional: bool = False,
        empty_text: str = "Не указан",
        initial_date: QDate | None = None,
        minimum_date: QDate | None = None,
    ):
        super().__init__(parent)
        self.optional = optional
        self._empty_text = empty_text
        self._minimum_date = minimum_date or (EMPTY_DATE if optional else QDate(1950, 1, 1))
        self._maximum_date = QDate(2099, 12, 31)
        self._date = EMPTY_DATE if optional else QDate.currentDate()
        self._last_valid_date = self._date

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.editor = QLineEdit(self)
        self.editor.setPlaceholderText(empty_text if optional else "ДД.ММ.ГГГГ")
        self.editor.setClearButtonEnabled(optional)
        self.editor.setToolTip(
            "Можно ввести: 17.09.2026, 17/09/26 или 17092026. "
            "Ctrl+T — сегодня."
        )
        self.editor.editingFinished.connect(self._commit_text)
        self.editor.textEdited.connect(self._on_text_edited)
        self.editor.installEventFilter(self)
        layout.addWidget(self.editor, 1)

        self.calendar_button = QToolButton(self)
        self.calendar_button.setText("▾")
        self.calendar_button.setToolTip("Выбрать дату в календаре")
        self.calendar_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.calendar_button.setFixedWidth(32)
        self.calendar_button.clicked.connect(self._show_calendar)
        layout.addWidget(self.calendar_button)

        self.setStyleSheet(
            "QWidget { background: transparent; }"
            "QLineEdit { border-top-right-radius:0; border-bottom-right-radius:0; }"
            "QToolButton { background:#F8FAFC; border:1px solid #D0D5DD; "
            "border-left:0; border-top-right-radius:8px; border-bottom-right-radius:8px; "
            "font-size:14px; color:#475467; }"
            "QToolButton:hover { background:#EFF8FF; color:#175CD3; }"
        )

        if initial_date is not None and initial_date.isValid():
            self.setDate(initial_date)
        elif optional:
            self.clear_date(emit=False)
        else:
            self.setDate(QDate.currentDate())

    def minimumDate(self) -> QDate:
        return self._minimum_date

    def maximumDate(self) -> QDate:
        return self._maximum_date

    def setMinimumDate(self, value: QDate) -> None:
        if value.isValid():
            self._minimum_date = value

    def setMaximumDate(self, value: QDate) -> None:
        if value.isValid():
            self._maximum_date = value

    def date(self) -> QDate:
        return self._date

    def text(self) -> str:
        return self.editor.text()

    def setDate(self, value: QDate) -> None:
        if not isinstance(value, QDate) or not value.isValid():
            if self.optional:
                self.clear_date()
            return
        if self.optional and value == EMPTY_DATE:
            self.clear_date()
            return
        if value < self._minimum_date or value > self._maximum_date:
            return
        changed = value != self._date
        self._date = QDate(value)
        self._last_valid_date = QDate(value)
        self.editor.setText(value.toString("dd.MM.yyyy"))
        if changed:
            self.dateChanged.emit(QDate(value))

    def is_empty(self) -> bool:
        return self.optional and self._date == EMPTY_DATE

    def clear_date(self, *, emit: bool = True) -> None:
        if not self.optional:
            return
        changed = self._date != EMPTY_DATE
        self._date = QDate(EMPTY_DATE)
        self._last_valid_date = QDate(EMPTY_DATE)
        self.editor.clear()
        if emit and changed:
            self.dateChanged.emit(QDate(EMPTY_DATE))

    def set_today(self) -> None:
        self.setDate(QDate.currentDate())

    def set_days_from(self, base: QDate, days: int) -> None:
        if not isinstance(base, QDate) or not base.isValid() or base == EMPTY_DATE:
            base = QDate.currentDate()
        self.setDate(base.addDays(days))

    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(enabled)
        self.editor.setEnabled(enabled)
        self.calendar_button.setEnabled(enabled)

    def setMinimumHeight(self, height: int) -> None:
        super().setMinimumHeight(height)
        self.editor.setMinimumHeight(height)
        self.calendar_button.setMinimumHeight(height)

    def setFixedHeight(self, height: int) -> None:
        super().setFixedHeight(height)
        self.editor.setFixedHeight(height)
        self.calendar_button.setFixedHeight(height)

    def setToolTip(self, text: str) -> None:
        super().setToolTip(text)
        if hasattr(self, "editor"):
            self.editor.setToolTip(text)

    def setFocus(self, reason=Qt.FocusReason.OtherFocusReason) -> None:
        self.editor.setFocus(reason)
        self.editor.selectAll()

    def eventFilter(self, watched, event):
        if watched is self.editor and event.type() == QEvent.Type.KeyPress:
            key_event = event
            if (
                key_event.modifiers() & Qt.KeyboardModifier.ControlModifier
                and key_event.key() == Qt.Key.Key_T
            ):
                self.set_today()
                return True
            if key_event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._commit_text()
                return True
            if self.optional and key_event.key() == Qt.Key.Key_Escape:
                self.clear_date()
                return True
        return super().eventFilter(watched, event)

    def _on_text_edited(self, text: str) -> None:
        # Keep the committed QDate until the user finishes editing. While the
        # user types, normalize numeric input to DD.MM.YYYY so separators do not
        # need to be entered manually.
        if self.optional and not text.strip() and self._date != EMPTY_DATE:
            self.clear_date()
            return

        formatted = format_date_while_typing(text)
        if formatted == text:
            return

        # Preserve a sensible cursor position by counting the digits that were
        # to the left of the cursor before reformatting.
        old_cursor = self.editor.cursorPosition()
        digits_before = sum(ch.isdigit() for ch in text[:old_cursor])
        self.editor.blockSignals(True)
        try:
            self.editor.setText(formatted)
            if digits_before <= 0:
                new_cursor = 0
            else:
                seen = 0
                new_cursor = len(formatted)
                for index, ch in enumerate(formatted):
                    if ch.isdigit():
                        seen += 1
                    if seen >= digits_before:
                        new_cursor = index + 1
                        # When a separator is auto-added immediately after the
                        # completed day/month, place the caret after it.
                        if new_cursor < len(formatted) and formatted[new_cursor] == ".":
                            new_cursor += 1
                        break
            self.editor.setCursorPosition(new_cursor)
        finally:
            self.editor.blockSignals(False)

    def _commit_text(self) -> None:
        raw = self.editor.text().strip()
        if not raw:
            if self.optional:
                self.clear_date()
            else:
                self.editor.setText(self._last_valid_date.toString("dd.MM.yyyy"))
            return

        today = QDate.currentDate()
        parsed = parse_friendly_date(
            raw,
            today=py_date(today.year(), today.month(), today.day()),
        )
        if parsed is None:
            self.editor.setText(
                "" if self.optional and self._date == EMPTY_DATE else self._last_valid_date.toString("dd.MM.yyyy")
            )
            self.editor.setToolTip(
                "Дата не распознана. Примеры: 17.09.2026, 17/09/26, 17092026."
            )
            return

        qdate = QDate(parsed.year, parsed.month, parsed.day)
        if qdate < self._minimum_date or qdate > self._maximum_date:
            self.editor.setText(
                "" if self.optional and self._date == EMPTY_DATE else self._last_valid_date.toString("dd.MM.yyyy")
            )
            return
        self.editor.setToolTip(
            "Можно ввести: 17.09.2026, 17/09/26 или 17092026. Ctrl+T — сегодня."
        )
        self.setDate(qdate)
        self.editor.selectAll()

    def _show_calendar(self) -> None:
        menu = QMenu(self)
        # QMenu/QCalendarWidget can inherit the OS dark palette even when the
        # application itself uses a light theme. Force a local light palette so
        # the popup always matches the rest of Document Registry.
        menu.setStyleSheet(
            "QMenu { background:#FFFFFF; border:1px solid #D0D5DD; border-radius:8px; padding:4px; }"
        )
        calendar = QCalendarWidget(menu)
        calendar.setGridVisible(False)
        calendar.setStyleSheet(
            """
            QCalendarWidget QWidget { background:#FFFFFF; color:#101828; }
            QCalendarWidget QToolButton {
                background:#FFFFFF; color:#344054; border:none; border-radius:6px;
                padding:5px 8px; font-weight:600;
            }
            QCalendarWidget QToolButton:hover { background:#F2F4F7; }
            QCalendarWidget QMenu { background:#FFFFFF; color:#101828; }
            QCalendarWidget QSpinBox {
                background:#FFFFFF; color:#101828; border:1px solid #D0D5DD;
                border-radius:5px; padding:3px;
            }
            QCalendarWidget QAbstractItemView {
                background:#FFFFFF; color:#344054; selection-background-color:#DCEBFF;
                selection-color:#175CD3; outline:0;
            }
            QCalendarWidget QAbstractItemView:disabled { color:#98A2B3; }
            """
        )
        selected = self._date
        if not selected.isValid() or selected == EMPTY_DATE:
            selected = QDate.currentDate()
        calendar.setSelectedDate(selected)
        calendar.setMinimumDate(self._minimum_date if self._minimum_date != EMPTY_DATE else QDate(1950, 1, 1))
        calendar.setMaximumDate(self._maximum_date)

        action = QWidgetAction(menu)
        action.setDefaultWidget(calendar)
        menu.addAction(action)

        def choose(value: QDate):
            self.setDate(value)
            menu.close()

        calendar.clicked.connect(choose)
        button_pos = self.calendar_button.mapToGlobal(QPoint(0, self.calendar_button.height()))
        menu.exec(button_pos)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        today = QAction("Сегодня", menu)
        today.triggered.connect(self.set_today)
        menu.addAction(today)
        for days in (1, 7, 14, 30):
            action = QAction(f"+{days} дн.", menu)
            action.triggered.connect(
                lambda _checked=False, offset=days: self.setDate(QDate.currentDate().addDays(offset))
            )
            menu.addAction(action)
        if self.optional:
            menu.addSeparator()
            clear = QAction("Очистить дату", menu)
            clear.triggered.connect(self.clear_date)
            menu.addAction(clear)
        menu.exec(event.globalPos())
