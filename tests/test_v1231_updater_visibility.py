from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import __version__
from app.update_service import UpdateError, UpdateService
from app.updater_status import UpdaterStatusReporter


class _FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15


class V1231UpdaterVisibilityTests(unittest.TestCase):
    def test_release_version_is_1233(self):
        self.assertEqual(__version__, "1.28.5")

    def test_status_records_display_mode_even_without_window(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reporter = UpdaterStatusReporter(root, "1.23.1", show_window=False)
            reporter.stage("wait_app", "Ожидаем закрытия DocumentRegistry...", 5)
            raw = json.loads((root / "data" / "update_status.json").read_text(encoding="utf-8"))
            self.assertFalse(raw["window_started"])
            self.assertEqual(raw["window_mode"], "none")

    def test_main_app_accepts_positive_updater_visibility_handshake(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            data.mkdir()
            (data / "update_status.json").write_text(
                json.dumps(
                    {
                        "state": "installing",
                        "version": "1.23.1",
                        "window_started": True,
                        "window_mode": "native",
                    }
                ),
                encoding="utf-8",
            )
            service = UpdateService(SimpleNamespace(executable_dir=root, data=data))
            process = _FakeProcess()
            result = service.wait_for_updater_window(process, "1.23.1", timeout_seconds=0.2)
            self.assertTrue(result["window_started"])
            self.assertFalse(process.terminated)

    def test_main_app_stops_handoff_when_updater_has_no_visible_window(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            data.mkdir()
            (data / "update_status.json").write_text(
                json.dumps(
                    {
                        "state": "installing",
                        "version": "1.23.1",
                        "window_started": False,
                        "window_mode": "none",
                        "window_error": "CreateWindowExW failed",
                    }
                ),
                encoding="utf-8",
            )
            service = UpdateService(SimpleNamespace(executable_dir=root, data=data))
            process = _FakeProcess()
            with self.assertRaises(UpdateError) as caught:
                service.wait_for_updater_window(process, "1.23.1", timeout_seconds=0.2)
            self.assertIn("Установка не начата", str(caught.exception))
            self.assertTrue(process.terminated)

    def test_ui_test_launches_standalone_updater_without_install_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            updater = root / "updater" / "DocumentRegistryUpdater.exe"
            updater.parent.mkdir(parents=True)
            updater.write_bytes(b"updater")
            service = UpdateService(SimpleNamespace(executable_dir=root, data=data))
            with patch("app.update_service.sys.frozen", True, create=True), patch.object(
                service, "finalize_pending_updater", return_value=False
            ), patch("app.update_service.subprocess.Popen") as popen:
                service.launch_updater_ui_test("1.23.1")
            command = popen.call_args.args[0]
            self.assertIn("--ui-test", command)
            self.assertIn("--version", command)
            self.assertIn("1.23.1", command)
            self.assertNotIn("--install", command)

    def test_source_has_native_visibility_check_console_fallback_and_diagnostic_button(self):
        updater = Path("app/updater_status.py").read_text(encoding="utf-8")
        controller = Path("app/ui/update_controller.py").read_text(encoding="utf-8")
        dialog = Path("app/ui/diagnostics_dialog.py").read_text(encoding="utf-8")
        main = Path("app/updater_main.py").read_text(encoding="utf-8")
        self.assertIn("IsWindowVisible", updater)
        self.assertIn("class ConsoleUpdaterWindow", updater)
        self.assertIn("class UpdaterDisplay", updater)
        self.assertIn("wait_for_updater_window", controller)
        self.assertIn("Проверить окно установки", dialog)
        self.assertIn('parser.add_argument("--ui-test"', main)


if __name__ == "__main__":
    unittest.main()
