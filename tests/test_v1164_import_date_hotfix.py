from pathlib import Path
import unittest

from app.date_input import format_date_while_typing


ROOT = Path(__file__).resolve().parents[1]


class V1164ImportDateHotfixTests(unittest.TestCase):
    def test_date_adds_second_dot_after_two_digit_month(self):
        cases = {
            "17": "17.",
            "17.0": "17.0",
            "17.09": "17.09.",
            "17/09": "17.09.",
            "17-09": "17.09.",
            "17.09.": "17.09.",
            "17.09.2": "17.09.2",
            "1709": "17.09.",
            "17092026": "17.09.2026",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(format_date_while_typing(raw), expected)

    def test_import_window_has_only_native_minimize_and_close(self):
        source = (ROOT / "app" / "import_dialog.py").read_text(encoding="utf-8")
        self.assertIn("WindowMinimizeButtonHint", source)
        self.assertIn("WindowCloseButtonHint", source)
        self.assertNotIn("WindowMaximizeButtonHint", source)
        self.assertNotIn('QPushButton("Свернуть")', source)
        self.assertNotIn('QPushButton("Отмена")', source)

    def test_native_minimize_hides_but_does_not_cancel_queue(self):
        source = (ROOT / "app" / "import_dialog.py").read_text(encoding="utf-8")
        self.assertIn("def changeEvent", source)
        self.assertIn("self.send_to_background(hide=True)", source)
        self.assertIn("def restore_from_background", source)
        self.assertIn("self.showNormal()", source)

    def test_native_close_clears_queue_and_requests_worker_stop(self):
        source = (ROOT / "app" / "import_dialog.py").read_text(encoding="utf-8")
        self.assertIn("def closeEvent", source)
        self.assertIn("self._request_stop_and_close()", source)
        self.assertIn("self.pdf_queue.clear()", source)
        self.assertGreaterEqual(source.count("requestInterruption()"), 2)
        self.assertIn("self.hide()", source)


if __name__ == "__main__":
    unittest.main()
