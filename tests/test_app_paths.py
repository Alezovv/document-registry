import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.app_paths as app_paths
from app.app_paths import ENV_HOME, get_app_paths, resource_dir, runtime_dir


class AppPathsTests(unittest.TestCase):
    def test_environment_override_makes_runtime_location_independent(self):
        with tempfile.TemporaryDirectory() as temp:
            custom = Path(temp) / "portable-home"
            with patch.dict(os.environ, {ENV_HOME: str(custom)}, clear=False):
                self.assertEqual(runtime_dir(), custom.resolve(strict=False))
                paths = get_app_paths()
                self.assertEqual(paths.data, custom.resolve(strict=False) / "data")
                self.assertEqual(paths.backups, custom.resolve(strict=False) / "backups")
                self.assertEqual(paths.logs, custom.resolve(strict=False) / "logs")

    def test_frozen_resources_use_meipass_not_executable_directory(self):
        with tempfile.TemporaryDirectory() as exe_temp, tempfile.TemporaryDirectory() as bundle_temp:
            fake_exe = Path(exe_temp) / "Document Registry.exe"
            fake_exe.write_bytes(b"")
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", str(fake_exe)), \
                 patch.object(sys, "_MEIPASS", bundle_temp, create=True), \
                 patch.dict(os.environ, {ENV_HOME: exe_temp}, clear=False):
                self.assertEqual(resource_dir(), Path(bundle_temp).resolve())
                paths = get_app_paths()
                self.assertEqual(paths.executable_dir, Path(exe_temp).resolve())
                self.assertEqual(paths.assets, Path(bundle_temp).resolve() / "assets")

    def test_unwritable_executable_folder_falls_back_to_local_app_data(self):
        with tempfile.TemporaryDirectory() as exe_temp, tempfile.TemporaryDirectory() as local_temp:
            fake_exe = Path(exe_temp) / "Document Registry.exe"
            fake_exe.write_bytes(b"")
            env = {key: value for key, value in os.environ.items() if key != ENV_HOME}
            env["LOCALAPPDATA"] = local_temp
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", str(fake_exe)), \
                 patch.dict(os.environ, env, clear=True), \
                 patch.object(app_paths, "_can_write_directory", return_value=False):
                expected = Path(local_temp) / "DocumentRegistry"
                self.assertEqual(runtime_dir(), expected.resolve(strict=False))


if __name__ == "__main__":
    unittest.main()
