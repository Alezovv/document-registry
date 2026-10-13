from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class V1187OcrDiagnosticsTests(unittest.TestCase):
    def test_version_keeps_v1187_features_in_later_releases(self):
        text = (ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
        import re
        match = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(tuple(map(int, match.groups())), (1, 18, 7))

    def test_settings_has_ocr_check_button_and_signal(self):
        text = (ROOT / "app" / "ui" / "settings_page.py").read_text(encoding="utf-8")
        self.assertIn("ocr_check_requested = Signal()", text)
        self.assertIn('QPushButton("Проверить OCR")', text)
        self.assertIn("self.ocr_check_requested.emit", text)

    def test_ocr_diagnostics_run_in_background_thread(self):
        text = (ROOT / "app" / "ui" / "ocr_diagnostics_controller.py").read_text(encoding="utf-8")
        self.assertIn("class _OcrDiagnosticThread(QThread)", text)
        self.assertIn("perform_ocr_runtime_self_check", text)
        self.assertIn("thread.start()", text)

    def test_window_wires_ocr_diagnostics(self):
        text = (ROOT / "app" / "ui" / "update_window_mixin.py").read_text(encoding="utf-8")
        self.assertIn("OcrDiagnosticsController", text)
        self.assertIn("widget.ocr_check_requested.connect", text)

    def test_build_keeps_frozen_ocr_self_check(self):
        text = (ROOT / "packaging" / "build.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("Initializing frozen OCR runtime", text)
        self.assertIn("--ocr-self-check", text)


if __name__ == "__main__":
    unittest.main()
