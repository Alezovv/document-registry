from __future__ import annotations

from pathlib import Path
import unittest

from app.date_input import format_date_while_typing


ROOT = Path(__file__).resolve().parents[1]


class V1163UiPolishTests(unittest.TestCase):
    def test_date_typing_inserts_dots_progressively(self):
        cases = {
            "1": "1",
            "17": "17.",
            "170": "17.0",
            "1709": "17.09.",
            "170920": "17.09.20",
            "17092026": "17.09.2026",
            "17/09/2026": "17.09.2026",
            "17-09-26": "17.09.26",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(format_date_while_typing(raw), expected)

    def test_calendar_popup_forces_light_theme(self):
        source = (ROOT / "app" / "ui" / "date_controls.py").read_text(encoding="utf-8")
        self.assertIn("QCalendarWidget QWidget { background:#FFFFFF", source)
        self.assertIn("QCalendarWidget QAbstractItemView", source)
        self.assertIn("format_date_while_typing", source)

    def test_request_editor_keeps_system_minimize_but_has_no_footer_cancel(self):
        source = (ROOT / "app" / "ui" / "dialogs" / "request_dialog.py").read_text(encoding="utf-8")
        self.assertIn("Qt.WindowType.WindowMinimizeButtonHint", source)
        self.assertNotIn('QPushButton("Отмена", footer_widget)', source)
        self.assertIn('QPushButton("Сохранить", footer_widget)', source)
        self.assertIn("Qt.WindowType.WindowCloseButtonHint", source)


if __name__ == "__main__":
    unittest.main()
