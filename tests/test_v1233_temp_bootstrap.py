from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

from app import __version__
from app.update_service import UpdateInfo, UpdateService
from app.updater_main import UPDATER_FILE, validate_staged_package


class V1233TempBootstrapTests(unittest.TestCase):
    def test_release_version_is_1233(self):
        self.assertEqual(__version__, "1.28.5")

    def _package(self, root: Path, *, updater_name: str = "DocumentRegistryUpdater.exe") -> tuple[Path, str]:
        package = root / "DocumentRegistry_update.zip"
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "update_manifest.json",
                json.dumps({"format_version": 1, "version": "1.23.3", "database_schema": 6}),
            )
            archive.writestr("payload/DocumentRegistry.exe", b"main")
            archive.writestr("payload/_internal/placeholder.txt", b"internal")
            archive.writestr(f"payload/updater/{updater_name}", b"fresh-updater-1233")
            archive.writestr(
                "payload/version.json",
                json.dumps({"version": "1.23.3", "database_schema": 6}),
            )
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        return package, digest

    def test_bootstrap_extracts_fresh_updater_from_verified_package(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package, digest = self._package(root)
            install = root / "installed"
            (install / "data").mkdir(parents=True)
            service = UpdateService(SimpleNamespace(executable_dir=install, data=install / "data"))
            info = UpdateInfo(
                version="1.23.3",
                package_path=package,
                sha256=digest,
                size_bytes=package.stat().st_size,
                database_schema=6,
            )
            updater = service._prepare_bootstrap_updater(package, info)
            self.assertEqual(updater.name, "DocumentRegistryUpdater.exe")
            self.assertEqual(updater.read_bytes(), b"fresh-updater-1233")
            self.assertNotEqual(updater.parent, install / "updater")
            self.assertIn("updater_bootstrap", str(updater.parent))

    def test_launch_uses_temp_updater_not_installed_stale_updater(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package, digest = self._package(root)
            install = root / "installed"
            data = install / "data"
            data.mkdir(parents=True)
            (install / "DocumentRegistry.exe").write_bytes(b"main")
            stale = install / "updater" / "DocumentRegistryUpdater.exe"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"stale-updater")
            service = UpdateService(SimpleNamespace(executable_dir=install, data=data))
            info = UpdateInfo(
                version="1.23.3",
                package_path=package,
                sha256=digest,
                size_bytes=package.stat().st_size,
                database_schema=6,
            )
            with patch("app.update_service.sys.frozen", True, create=True), patch(
                "app.update_service.subprocess.Popen"
            ) as popen:
                service.launch_updater(package, info)
            command = popen.call_args.args[0]
            launched = Path(command[0])
            self.assertNotEqual(launched, stale)
            self.assertIn("updater_bootstrap", str(launched.parent))
            self.assertEqual(launched.read_bytes(), b"fresh-updater-1233")
            self.assertEqual(Path(popen.call_args.kwargs["cwd"]), launched.parent)

    def test_staged_package_requires_real_updater_exe(self):
        with tempfile.TemporaryDirectory() as temp:
            staging = Path(temp)
            payload = staging / "payload"
            (payload / "_internal").mkdir(parents=True)
            (payload / "DocumentRegistry.exe").write_bytes(b"main")
            (payload / "updater").mkdir(parents=True)
            (payload / "updater" / UPDATER_FILE).write_bytes(b"updater")
            (staging / "update_manifest.json").write_text(
                json.dumps({"format_version": 1, "version": "1.23.3", "database_schema": 6}),
                encoding="utf-8",
            )
            self.assertEqual(validate_staged_package(staging, "1.23.3"), payload)

    def test_build_package_no_longer_uses_next_exe(self):
        source = Path("packaging/build.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('Join-Path $PayloadUpdater "DocumentRegistryUpdater.exe"', source)
        self.assertNotIn('Join-Path $PayloadUpdater "DocumentRegistryUpdater.next.exe"', source)

    def test_win32_window_does_not_require_wintypes_hcursor_attribute(self):
        source = Path("app/updater_status.py").read_text(encoding="utf-8")
        self.assertIn('HCURSOR = getattr(wintypes, "HCURSOR", HANDLE)', source)
        self.assertNotIn('(\"hCursor\", wintypes.HCURSOR)', source)


if __name__ == "__main__":
    unittest.main()
