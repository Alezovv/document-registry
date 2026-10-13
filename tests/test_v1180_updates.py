import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from app.app_paths import AppPaths
from app.settings_service import AppSettings, SettingsService
from app.update_service import (
    UpdateError,
    UpdateNotReady,
    UpdateService,
    is_newer_version,
)
from app.updater_main import InstallError, safe_extract, validate_staged_package


class V1180UpdateTests(unittest.TestCase):
    def _paths(self, root: Path) -> AppPaths:
        return AppPaths(
            executable_dir=root,
            resources=root,
            runtime=root,
            data=root / "data",
            documents=root / "data" / "documents",
            backups=root / "backups",
            logs=root / "logs",
            paddle_cache=root / "data" / "paddle_cache",
            assets=root / "assets",
            models=root / "models",
        )

    def test_version_comparison(self):
        self.assertTrue(is_newer_version("1.18.0", "1.17.0"))
        self.assertFalse(is_newer_version("1.17.0", "1.17.0"))
        self.assertFalse(is_newer_version("1.16.9", "1.17.0"))

    def test_update_manifest_and_package_are_validated(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "drive"
            source.mkdir()
            package = source / "DocumentRegistry_update.zip"
            package.write_bytes(b"test-package")
            digest = hashlib.sha256(package.read_bytes()).hexdigest()
            (source / "manifest.json").write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "version": "1.18.0",
                        "package": package.name,
                        "sha256": digest,
                        "size": package.stat().st_size,
                        "notes": "test",
                    }
                ),
                encoding="utf-8",
            )
            service = UpdateService(self._paths(root))
            info = service.check_for_update(source, "1.17.0")
            self.assertIsNotNone(info)
            self.assertEqual(info.version, "1.18.0")
            prepared = service.prepare_update(info)
            self.assertEqual(prepared.read_bytes(), b"test-package")

    def test_manifest_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "drive"
            source.mkdir()
            (source / "manifest.json").write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "version": "1.18.0",
                        "package": "../evil.zip",
                        "sha256": "0" * 64,
                        "size": 1,
                    }
                ),
                encoding="utf-8",
            )
            service = UpdateService(self._paths(root))
            with self.assertRaises(UpdateError):
                service.check_for_update(source, "1.17.0")

    def test_partially_synced_package_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "drive"
            source.mkdir()
            package = source / "DocumentRegistry_update.zip"
            package.write_bytes(b"small")
            (source / "manifest.json").write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "version": "1.18.0",
                        "package": package.name,
                        "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
                        "size": 999,
                    }
                ),
                encoding="utf-8",
            )
            service = UpdateService(self._paths(root))
            with self.assertRaises(UpdateNotReady):
                service.check_for_update(source, "1.17.0")

    def test_settings_roundtrip_update_source(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            service = SettingsService(
                root / "data" / "settings.json",
                root / "data" / "documents",
                runtime_root=root,
            )
            original = AppSettings(
                documents_dir=str(root / "data" / "documents"),
                update_source_dir=r"G:\My Drive\DocumentRegistry_Updates",
            )
            service.save(original)
            loaded = service.load()
            self.assertEqual(loaded.update_source_dir, original.update_source_dir)

    def test_updater_rejects_zip_path_traversal(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive_path = root / "bad.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside.txt", "bad")
            with self.assertRaises(InstallError):
                safe_extract(archive_path, root / "extract")

    def test_updater_accepts_program_only_payload(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            stage = root / "stage"
            payload = stage / "payload"
            (payload / "_internal").mkdir(parents=True)
            (payload / "DocumentRegistry.exe").write_bytes(b"exe")
            (payload / "updater").mkdir(parents=True)
            (payload / "updater" / "DocumentRegistryUpdater.exe").write_bytes(b"updater")
            (stage / "update_manifest.json").write_text(
                json.dumps({"format_version": 1, "version": "1.18.0"}),
                encoding="utf-8",
            )
            result = validate_staged_package(stage, "1.18.0")
            self.assertEqual(result, payload)

    def test_updater_rejects_user_data_in_payload(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            stage = root / "stage"
            payload = stage / "payload"
            (payload / "_internal").mkdir(parents=True)
            (payload / "data").mkdir()
            (payload / "DocumentRegistry.exe").write_bytes(b"exe")
            (payload / "updater").mkdir(parents=True)
            (payload / "updater" / "DocumentRegistryUpdater.exe").write_bytes(b"updater")
            (stage / "update_manifest.json").write_text(
                json.dumps({"format_version": 1, "version": "1.18.0"}),
                encoding="utf-8",
            )
            with self.assertRaises(InstallError):
                validate_staged_package(stage, "1.18.0")


if __name__ == "__main__":
    unittest.main()
