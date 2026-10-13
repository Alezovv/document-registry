from pathlib import Path
import unittest

from app import __version__

ROOT = Path(__file__).resolve().parents[1]


class V1282NonModalReviewTests(unittest.TestCase):
    def test_release_version(self):
        self.assertEqual(__version__, "1.28.5")

    def test_import_review_is_non_modal_and_detached_from_main_window(self):
        source = (ROOT / "app" / "ui" / "import_controller.py").read_text(encoding="utf-8")
        self.assertIn("review = RequestDialog(\n                None,", source)
        self.assertIn("Qt.WindowModality.NonModal", source)
        self.assertIn("review.setModal(False)", source)
        self.assertIn("review.finished.connect(self._on_review_finished)", source)
        review_section = source.split("def _review_and_save", 1)[1].split("def _on_review_finished", 1)[0]
        self.assertNotIn("review.exec()", review_section)

    def test_import_queue_waits_for_async_review_handshake(self):
        source = (ROOT / "app" / "import_dialog.py").read_text(encoding="utf-8")
        self.assertIn("self._manual_review_active = True", source)
        self.assertIn("if self._review_pending and not self._manual_review_active", source)
        self.assertIn("self._manual_review_active = False", source)


if __name__ == "__main__":
    unittest.main()
