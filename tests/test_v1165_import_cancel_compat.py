from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class V1165ImportCancelCompatibilityTests(unittest.TestCase):
    def test_cancel_api_is_kept_for_legacy_regressions_but_button_is_hidden(self):
        source = (ROOT / "app" / "import_dialog.py").read_text(encoding="utf-8")
        setup = source.split("    def setup_ui(self):", 1)[1].split("    def select_files", 1)[0]
        self.assertIn("self.cancel_button.clicked.connect(self.cancel_import)", setup)
        self.assertIn("self.cancel_button.hide()", setup)
        self.assertNotIn("root.addWidget(self.cancel_button)", setup)
        self.assertNotIn("footer.addWidget(self.cancel_button)", setup)

    def test_idle_cancel_finishes_immediately_while_native_close_stops_active_workers(self):
        source = (ROOT / "app" / "import_dialog.py").read_text(encoding="utf-8")
        block = source.split("    def cancel_import(self):", 1)[1].split("    def closeEvent", 1)[0]
        self.assertIn("not self.processing", block)
        self.assertIn("self.worker is None", block)
        self.assertIn("self.parser_worker is None", block)
        self.assertIn("self._finish_cancel()", block)
        close_block = source.split("    def closeEvent(self, event):", 1)[1].split("    def reject", 1)[0]
        self.assertIn("self._request_stop_and_close()", close_block)


if __name__ == "__main__":
    unittest.main()
