from __future__ import annotations

from datetime import date
from pathlib import Path
import unittest

from app.date_input import format_date, parse_friendly_date


ROOT = Path(__file__).resolve().parents[1]


class DateInputTests(unittest.TestCase):
    def test_common_date_spellings_are_accepted(self):
        today = date(2026, 9, 17)
        expected = date(2026, 9, 17)
        for raw in ("17.09.2026", "17/09/26", "17-9-26", "17092026", "170926", "1709", "17.09"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_friendly_date(raw, today=today), expected)

    def test_invalid_date_is_rejected(self):
        self.assertIsNone(parse_friendly_date("31.02.2026", today=date(2026, 9, 17)))
        self.assertIsNone(parse_friendly_date("abc", today=date(2026, 9, 17)))

    def test_format_is_registry_format(self):
        self.assertEqual(format_date(date(2026, 9, 7)), "07.09.2026")

    def test_window_always_starts_on_registry(self):
        source = (ROOT / "app" / "window.py").read_text(encoding="utf-8")
        self.assertIn('self.current_page = "registry"', source)
        self.assertNotIn("self.current_page = self.settings.last_page", source)

    def test_registry_filters_use_blank_friendly_dates(self):
        source = (ROOT / "app" / "ui" / "registry_page.py").read_text(encoding="utf-8")
        self.assertIn('empty_text="С даты"', source)
        self.assertIn('empty_text="По дату"', source)
        self.assertIn("self.date_from_filter.is_empty()", source)
        self.assertIn("self.date_to_filter.is_empty()", source)


if __name__ == "__main__":
    unittest.main()
