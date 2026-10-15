from pathlib import Path

from PySide6.QtCore import QEvent, QRect, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QStyledItemDelegate


class StatusDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        status = index.data(Qt.ItemDataRole.DisplayRole)
        if not status:
            return super().paint(painter, option, index)

        styles = {
            "Новый": {"icon": "✦", "text": "#175CD3", "bg": "#EFF8FF"},
            "В работе": {"icon": "◐", "text": "#B54708", "bg": "#FFFAEB"},
            "Исполнен": {"icon": "✓", "text": "#027A48", "bg": "#ECFDF3"},
            "Отклонен": {"icon": "×", "text": "#B42318", "bg": "#FEF3F2"},
            "Просрочен": {"icon": "!", "text": "#B42318", "bg": "#FEE4E2"},
        }
        style = styles.get(
            status,
            {"icon": "•", "text": "#475467", "bg": "#F2F4F7"},
        )

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = f'{style["icon"]}  {status}'
        fm = painter.fontMetrics()
        status_w = fm.boundingRect(text).width() + 18
        pill_height = fm.height() + 6
        rect = option.rect

        dup_info = index.data(Qt.ItemDataRole.UserRole + 2)
        is_dup = isinstance(dup_info, dict) and dup_info.get("is_duplicate")

        if is_dup:
            gap = 5
            dup_candidates = ("📑 Дубликат", "📑 Копия", "📑")
            chosen_dup = "📑"
            dup_w = 22
            for candidate in dup_candidates:
                w = fm.boundingRect(candidate).width() + 14 if candidate != "📑" else 22
                if status_w + gap + w <= rect.width() - 6:
                    chosen_dup = candidate
                    dup_w = w
                    break

            total_w = status_w + gap + dup_w
            start_x = rect.left() + max(2, (rect.width() - total_w) // 2)
            y = rect.top() + (rect.height() - pill_height) // 2
            status_rect = QRect(start_x, y, status_w, pill_height)
            dup_rect = QRect(start_x + status_w + gap, y, dup_w, pill_height)

            text_color = QColor(style["text"])
            painter.setPen(QPen(text_color, 1))
            painter.setBrush(QBrush(QColor(style["bg"])))
            painter.drawRoundedRect(status_rect, 10, 10)
            painter.setPen(text_color)
            painter.drawText(status_rect, Qt.AlignmentFlag.AlignCenter, text)

            painter.setPen(QPen(QColor("#FDBA74"), 1))
            painter.setBrush(QBrush(QColor("#FFF7ED")))
            painter.drawRoundedRect(dup_rect, 9, 9)
            painter.setPen(QColor("#C2410C"))
            painter.drawText(dup_rect, Qt.AlignmentFlag.AlignCenter, chosen_dup)
        else:
            x = rect.left() + (rect.width() - status_w) // 2
            y = rect.top() + (rect.height() - pill_height) // 2
            bg_rect = QRect(x, y, status_w, pill_height)
            text_color = QColor(style["text"])
            painter.setPen(QPen(text_color, 1))
            painter.setBrush(QBrush(QColor(style["bg"])))
            painter.drawRoundedRect(bg_rect, 11, 11)
            painter.setPen(text_color)
            painter.drawText(bg_rect, Qt.AlignmentFlag.AlignCenter, text)

        painter.restore()


class DocumentActionsDelegate(QStyledItemDelegate):
    OPEN_WIDTH = 85
    DELETE_WIDTH = 75

    def __init__(self, parent=None, open_callback=None, delete_callback=None):
        super().__init__(parent)
        self.open_callback = open_callback
        self.delete_callback = delete_callback

    def _button_rects(self, rect):
        content_rect = rect.adjusted(10, 0, -10, 0)
        open_rect = QRect(
            content_rect.left(),
            content_rect.top() + 9,
            self.OPEN_WIDTH,
            content_rect.height() - 18,
        )
        delete_rect = QRect(
            open_rect.right() + 8,
            open_rect.top(),
            self.DELETE_WIDTH,
            open_rect.height(),
        )
        return open_rect, delete_rect

    def paint(self, painter, option, index):
        path = index.data(Qt.ItemDataRole.UserRole)
        if not path:
            return super().paint(painter, option, index)

        open_rect, delete_rect = self._button_rects(option.rect)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        painter.setPen(QPen(QColor("#BFDBFE"), 1))
        painter.setBrush(QBrush(QColor("#EFF6FF")))
        painter.drawRoundedRect(open_rect, 7, 7)
        painter.setPen(QColor("#2563EB"))
        painter.drawText(open_rect, Qt.AlignmentFlag.AlignCenter, "Открыть")

        painter.setPen(QPen(QColor("#FDA29B"), 1))
        painter.setBrush(QBrush(QColor("#FEF2F2")))
        painter.drawRoundedRect(delete_rect, 7, 7)
        painter.setPen(QColor("#B42318"))
        painter.drawText(delete_rect, Qt.AlignmentFlag.AlignCenter, "Удалить")
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() != QEvent.Type.MouseButtonRelease:
            return True
        if event.button() != Qt.MouseButton.LeftButton:
            return False

        path = index.data(Qt.ItemDataRole.UserRole)
        if not path:
            return False

        open_rect, delete_rect = self._button_rects(option.rect)
        position = event.position().toPoint()
        if open_rect.contains(position) and self.open_callback:
            self.open_callback(Path(path))
            return True
        if delete_rect.contains(position) and self.delete_callback:
            self.delete_callback(Path(path))
            return True
        return False
