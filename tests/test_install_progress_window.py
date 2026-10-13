from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.update_service import UpdateInfo, UpdateService
from app.updater_status import NativeUpdaterWindow, UpdaterStatusReporter


class InstallProgressWindowTests(unittest.TestCase):
    def test_status_file_records_whether_native_window_started(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reporter = UpdaterStatusReporter(root, "1.22.1", show_window=False)
            reporter.stage("extract", "Распаковываем новую версию...", 22)
            payload = (root / "data" / "update_status.json").read_text(encoding="utf-8")
            self.assertIn('"window_started": false', payload)

    def test_native_window_uses_only_core_win32_controls_and_minimize_box(self):
        source = Path("app/updater_status.py").read_text(encoding="utf-8")
        self.assertIn("WS_MINIMIZEBOX", source)
        self.assertIn("WS_EX_APPWINDOW", source)
        self.assertIn('"STATIC"', source)
        self.assertNotIn("msctls_progress32", source)
        self.assertNotIn("InitCommonControls", source)
        self.assertIn("self._ready.wait", source)

    def test_finalize_pending_updater_remains_as_legacy_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            updater_dir = root / "updater"
            updater_dir.mkdir(parents=True)
            current = updater_dir / "DocumentRegistryUpdater.exe"
            pending = updater_dir / "DocumentRegistryUpdater.next.exe"
            current.write_bytes(b"old-updater")
            pending.write_bytes(b"legacy-new-updater")

            paths = SimpleNamespace(executable_dir=root, data=root / "data")
            service = UpdateService(paths)
            self.assertTrue(service.finalize_pending_updater())
            self.assertEqual(current.read_bytes(), b"legacy-new-updater")
            self.assertFalse(pending.exists())

    def test_native_window_start_is_noop_off_windows(self):
        window = NativeUpdaterWindow("1.22.1")
        if __import__("os").name != "nt":
            self.assertFalse(window.start(timeout_seconds=0.1))


if __name__ == "__main__":
    unittest.main()
