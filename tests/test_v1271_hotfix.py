from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app import __version__
from app.database import Database


ROOT = Path(__file__).resolve().parents[1]


class V1271HotfixTests(unittest.TestCase):
    def test_release_version(self):
        self.assertEqual(__version__, "1.28.5")

    def test_import_dialog_does_not_import_ocr_directly(self):
        source = (ROOT / "app" / "import_dialog.py").read_text(encoding="utf-8")
        self.assertNotIn("from app.ocr import", source)
        self.assertIn("finalize_ocr_batch_memory", source)

    def test_database_temp_directory_can_be_released_after_operations(self):
        # Database itself keeps no persistent SQLite connection.  This test is
        # intentionally simple; on Windows it catches accidental long-lived
        # handles owned by Database methods.
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            db = Database(data_dir=root / "data")
            self.assertTrue(db.db_path.is_file())
            self.assertEqual(db.SCHEMA_VERSION, 7)


if __name__ == "__main__":
    unittest.main()
