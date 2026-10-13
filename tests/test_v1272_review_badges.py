from __future__ import annotations

from pathlib import Path
import unittest

from app import __version__


ROOT = Path(__file__).resolve().parents[1]


class V1272ReviewBadgeTests(unittest.TestCase):
    def test_release_version(self):
        self.assertEqual(__version__, "1.28.5")

    def test_parser_review_badges_wrap_and_size_from_text(self):
        source = (ROOT / "app" / "ui" / "dialogs" / "request_dialog.py").read_text(encoding="utf-8")
        self.assertIn("QGridLayout", source)
        self.assertIn("grid_row, grid_col = divmod(badge_index, 3)", source)
        self.assertIn("button.fontMetrics().horizontalAdvance(button.text()) + 24", source)
        self.assertIn("QSizePolicy.Policy.Expanding", source)


if __name__ == "__main__":
    unittest.main()
