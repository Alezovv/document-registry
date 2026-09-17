from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ImportDialogCancelRegressionTests(unittest.TestCase):
    def test_cancel_is_available_before_pdf_selection(self):
        source = (ROOT / "app" / "import_dialog.py").read_text(encoding="utf-8")
        setup = source.split("    def setup_ui(self):", 1)[1].split("    def select_files(self):", 1)[0]
        self.assertIn("self.cancel_button.clicked.connect(self.cancel_import)", setup)
        self.assertIn("self.cancel_button.setEnabled(True)", setup)
        self.assertNotIn("self.cancel_button.setEnabled(False)", setup)

    def test_idle_cancel_closes_without_waiting_for_workers(self):
        source = (ROOT / "app" / "import_dialog.py").read_text(encoding="utf-8")
        block = source.split("    def cancel_import(self):", 1)[1].split("    def closeEvent", 1)[0]
        self.assertIn("not self.processing", block)
        self.assertIn("self.worker is None", block)
        self.assertIn("self.parser_worker is None", block)
        self.assertIn("self._finish_cancel()", block)


if __name__ == "__main__":
    unittest.main()
