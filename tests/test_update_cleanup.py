import tempfile
import unittest
from pathlib import Path

from app.update_cleanup import cleanup_legacy_update_files


class UpdateCleanupTests(unittest.TestCase):
    def test_removes_obsolete_saved_filter_files_from_overlay_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            data_dir = root / "data"
            cache_dir = app_dir / "__pycache__"
            ui_dir = app_dir / "ui"
            ui_cache_dir = ui_dir / "__pycache__"
            app_dir.mkdir()
            data_dir.mkdir()
            cache_dir.mkdir()
            ui_cache_dir.mkdir(parents=True)

            stale_source = app_dir / "filter_preset_service.py"
            stale_data = data_dir / "filter_presets.json"
            stale_pyc = cache_dir / "filter_preset_service.cpython-313.pyc"
            stale_dashboard = app_dir / "dashboard_service.py"
            stale_dashboard_ui = ui_dir / "dashboard_page.py"
            stale_dashboard_pyc = cache_dir / "dashboard_service.cpython-313.pyc"
            stale_dashboard_ui_pyc = ui_cache_dir / "dashboard_page.cpython-313.pyc"
            keep = app_dir / "database.py"

            stale_source.write_text("obsolete", encoding="utf-8")
            stale_data.write_text("{}", encoding="utf-8")
            stale_pyc.write_bytes(b"obsolete")
            stale_dashboard.write_text("obsolete", encoding="utf-8")
            stale_dashboard_ui.write_text("obsolete", encoding="utf-8")
            stale_dashboard_pyc.write_bytes(b"obsolete")
            stale_dashboard_ui_pyc.write_bytes(b"obsolete")
            keep.write_text("keep", encoding="utf-8")

            cleanup_legacy_update_files(root)

            self.assertFalse(stale_source.exists())
            self.assertFalse(stale_data.exists())
            self.assertFalse(stale_pyc.exists())
            self.assertFalse(stale_dashboard.exists())
            self.assertFalse(stale_dashboard_ui.exists())
            self.assertFalse(stale_dashboard_pyc.exists())
            self.assertFalse(stale_dashboard_ui_pyc.exists())
            self.assertTrue(keep.exists())


if __name__ == "__main__":
    unittest.main()
