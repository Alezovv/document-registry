from pathlib import Path
import tempfile
import unittest

from app.app_paths import AppPaths
from app.self_check import perform_self_check, write_self_check_report


class SelfCheckTests(unittest.TestCase):
    def test_lightweight_self_check_validates_portable_layout_without_importing_gui(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "resources" / "assets"
            models = root / "models"
            assets.mkdir(parents=True)
            (assets / "app_icon.png").write_bytes(b"png")
            official = models / "paddle" / "official_models"
            for name in ("PP-OCRv5_mobile_det_onnx", "eslav_PP-OCRv5_mobile_rec_onnx"):
                folder = official / name
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "inference.onnx").write_bytes(b"onnx")

            paths = AppPaths(
                executable_dir=root,
                resources=root / "resources",
                runtime=root,
                data=root / "data",
                documents=root / "data" / "documents",
                backups=root / "backups",
                logs=root / "logs",
                paddle_cache=root / "data" / "paddle_cache",
                assets=assets,
                models=models,
            )
            result = perform_self_check(paths, check_imports=False)
            self.assertTrue(result.ok)
            report = write_self_check_report(result, root / "check.json")
            self.assertTrue(report.is_file())


if __name__ == "__main__":
    unittest.main()
