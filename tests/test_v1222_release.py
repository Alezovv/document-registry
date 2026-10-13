from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V1222ReleaseTests(unittest.TestCase):
    def test_release_version_sources_stay_in_sync_and_are_not_older_than_1222(self):
        app_text = (ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
        build_text = (ROOT / "packaging" / "build.ps1").read_text(encoding="utf-8-sig")
        app_match = re.search(r'__version__\s*=\s*"([^"]+)"', app_text)
        build_match = re.search(r'\[string\]\$Version\s*=\s*"([^"]+)"', build_text)
        self.assertIsNotNone(app_match)
        self.assertIsNotNone(build_match)
        self.assertEqual(app_match.group(1), build_match.group(1))
        version = tuple(int(part) for part in app_match.group(1).split("."))
        self.assertGreaterEqual(version, (1, 22, 2))

    def test_parser_cache_version_is_not_invalidated_by_ui_only_release(self):
        parser_text = (ROOT / "app" / "document_parser.py").read_text(encoding="utf-8")
        self.assertIn('PARSER_VERSION = "1.22.1"', parser_text)

    def test_update_offer_shows_current_target_notes_and_package_size(self):
        source = (ROOT / "app" / "ui" / "update_controller.py").read_text(encoding="utf-8")
        self.assertIn('f"DocumentRegistry {self.current_version} → {info.version}"', source)
        self.assertIn('details = info.notes or', source)
        self.assertIn('f"\\n\\nРазмер пакета: {size_mb:.1f} МБ."', source)
        self.assertIn('f"Обновление DocumentRegistry до {info.version}"', source)

    def test_publish_guards_reject_version_mismatches(self):
        build_text = (ROOT / "packaging" / "build.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("function Assert-AppVersion", build_text)
        self.assertIn("function Assert-PortableVersion", build_text)
        self.assertIn("function Assert-UpdatePackageVersion", build_text)
        self.assertIn("Assert-AppVersion $Version", build_text)
        self.assertIn("Assert-PortableVersion $PortableSource $PublishVersion", build_text)
        self.assertIn("Assert-UpdatePackageVersion $PackageTarget $PublishVersion", build_text)

    def test_install_progress_and_publish_guards_remain_in_current_source(self):
        updater = (ROOT / "app" / "updater_status.py").read_text(encoding="utf-8")
        build_text = (ROOT / "packaging" / "build.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("NativeUpdaterWindow", updater)
        self.assertIn("window_started", updater)
        self.assertIn("Assert-UpdatePackageVersion", build_text)


if __name__ == "__main__":
    unittest.main()
