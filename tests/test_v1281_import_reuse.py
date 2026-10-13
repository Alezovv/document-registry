from pathlib import Path
import unittest

from app import __version__
from app.resource_profile import choose_profile

ROOT = Path(__file__).resolve().parents[1]


class V1281ImportReuseTests(unittest.TestCase):
    def test_release_version(self):
        self.assertEqual(__version__, "1.28.5")

    def test_completed_import_can_start_another_batch(self):
        dialog = (ROOT / "app" / "import_dialog.py").read_text(encoding="utf-8")
        controller = (ROOT / "app" / "ui" / "import_controller.py").read_text(encoding="utf-8")
        self.assertIn("Добавить ещё PDF", dialog)
        self.assertIn('self.setProperty("batch_finished", False)', dialog)
        self.assertIn("def prepare_for_new_batch", dialog)
        self.assertIn('self.dialog.property("batch_finished")', controller)
        self.assertIn("self.dialog.prepare_for_new_batch()", controller)

    def test_economy_does_not_lower_quality_from_auto_on_same_pc(self):
        auto = choose_profile(20, 16 * 1024**3, "auto")
        economy = choose_profile(20, 16 * 1024**3, "economy")
        self.assertEqual((economy.ocr_dpi, economy.retry_dpi), (auto.ocr_dpi, auto.retry_dpi))
        self.assertEqual((economy.max_image_width, economy.max_image_height), (auto.max_image_width, auto.max_image_height))
        self.assertEqual(economy.worker_threads, 1)


if __name__ == "__main__":
    unittest.main()
