import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.settings_service import AppSettings, RUNTIME_TOKEN, SettingsService


class SettingsServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings_file = self.root / "data" / "settings.json"
        self.default_documents = self.root / "data" / "documents"
        self.service = SettingsService(
            self.settings_file,
            self.default_documents,
            runtime_root=self.root,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_load_creates_defaults_when_file_missing(self):
        settings = self.service.load()
        self.assertEqual(settings.documents_dir, str(self.default_documents))
        self.assertTrue(settings.confirm_delete)
        self.assertTrue(self.settings_file.exists())

    def test_removed_dashboard_page_falls_back_to_registry(self):
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(
            '{"documents_dir": "docs", "confirm_delete": true, "last_page": "dashboard"}',
            encoding="utf-8",
        )
        loaded = self.service.load()
        self.assertEqual(loaded.last_page, "registry")

    def test_invalid_last_page_falls_back_to_registry(self):
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(
            '{"documents_dir": "docs", "confirm_delete": true, "last_page": "missing"}',
            encoding="utf-8",
        )
        loaded = self.service.load()
        self.assertEqual(loaded.last_page, "registry")

    def test_roundtrip(self):
        custom = self.root / "custom"
        custom.mkdir()
        expected = AppSettings(
            documents_dir=str(custom),
            confirm_delete=False,
            last_page="documents",
            performance_mode="performance",
        )
        self.service.save(expected)
        self.assertEqual(self.service.load(), expected)

    def test_invalid_json_falls_back_to_defaults(self):
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text("{broken", encoding="utf-8")
        settings = self.service.load()
        self.assertEqual(settings.documents_dir, str(self.default_documents))
        self.assertTrue(settings.confirm_delete)

    def test_invalid_performance_mode_falls_back_to_auto(self):
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(
            '{"documents_dir": "docs", "performance_mode": "turbo"}',
            encoding="utf-8",
        )
        loaded = self.service.load()
        self.assertEqual(loaded.performance_mode, "auto")

    def test_save_leaves_valid_json_and_uses_portable_runtime_path(self):
        docs = self.root / "docs"
        docs.mkdir()
        settings = AppSettings(str(docs), False, "settings")
        self.service.save(settings)
        payload = json.loads(self.settings_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["documents_dir"], f"{RUNTIME_TOKEN}docs")
        self.assertFalse(payload["confirm_delete"])
        self.assertEqual(payload["last_page"], "settings")
        self.assertEqual(payload["performance_mode"], "auto")
        leftovers = list(self.settings_file.parent.glob(".settings.json.*.tmp"))
        self.assertEqual(leftovers, [])

    def test_portable_path_survives_move_to_another_root(self):
        docs = self.root / "data" / "documents"
        docs.mkdir(parents=True)
        self.service.save(AppSettings(str(docs)))
        raw = self.settings_file.read_text(encoding="utf-8")

        moved = self.root / "moved-copy"
        moved_settings = moved / "data" / "settings.json"
        moved_settings.parent.mkdir(parents=True)
        moved_settings.write_text(raw, encoding="utf-8")
        moved_service = SettingsService(
            moved_settings,
            moved / "data" / "documents",
            runtime_root=moved,
        )
        loaded = moved_service.load()
        self.assertEqual(Path(loaded.documents_dir), moved / "data" / "documents")

    def test_missing_legacy_absolute_path_falls_back_to_current_documents(self):
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        stale = self.root / "old-machine" / "data" / "documents"
        self.settings_file.write_text(
            json.dumps({"documents_dir": str(stale), "last_page": "registry"}),
            encoding="utf-8",
        )
        loaded = self.service.load()
        self.assertEqual(Path(loaded.documents_dir), self.default_documents)


if __name__ == "__main__":
    unittest.main()
