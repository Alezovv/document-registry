from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import tempfile
import unittest

from app.app_paths import AppPaths
from app.self_check import perform_ocr_runtime_self_check


def make_paths(root: Path) -> AppPaths:
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


class OCRRuntimeSelfCheckTests(unittest.TestCase):
    def test_reports_success_when_engine_initializes(self):
        with tempfile.TemporaryDirectory() as name:
            paths = make_paths(Path(name))
            fake_engine = object()
            with mock.patch("app.ocr.OCR._get_engine", return_value=fake_engine):
                result = perform_ocr_runtime_self_check(paths)
            self.assertTrue(result.ok)
            self.assertEqual(result.items[0].name, "ocr:runtime")

    def test_reports_failure_when_engine_initialization_fails(self):
        with tempfile.TemporaryDirectory() as name:
            paths = make_paths(Path(name))
            with mock.patch("app.ocr.OCR._get_engine", side_effect=RuntimeError("pipeline missing")):
                result = perform_ocr_runtime_self_check(paths)
            self.assertFalse(result.ok)
            self.assertIn("pipeline missing", result.items[0].details)


if __name__ == "__main__":
    unittest.main()
