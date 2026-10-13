from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import __version__
from app.app_paths import AppPaths
from app.database import Database
from app.diagnostics_service import DiagnosticsService


class V1232DiagnosticsHotfixTests(unittest.TestCase):
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

    def test_release_version_is_1233(self):
        self.assertEqual(__version__, "1.28.5")

    def test_diagnostics_reads_real_update_status_json(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            paths = self._paths(root)
            paths.data.mkdir(parents=True)
            (paths.data / "update_status.json").write_text(
                json.dumps(
                    {
                        "state": "success",
                        "stage": "done",
                        "message": "Установка завершена",
                        "progress": 100,
                        "version": "1.23.1",
                        "window_started": True,
                        "window_mode": "native",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            db = Database(data_dir=paths.data)
            overview = DiagnosticsService(db, paths=paths).overview()
            status = overview["last_update"]
            self.assertEqual(status["state"], "success")
            self.assertEqual(status["version"], "1.23.1")
            self.assertTrue(status["window_started"])
            self.assertEqual(status["window_mode"], "native")

    def test_diagnostics_actions_use_two_row_grid(self):
        source = (
            Path(__file__).resolve().parents[1] / "app" / "ui" / "diagnostics_dialog.py"
        ).read_text(encoding="utf-8")
        self.assertIn("actions = QGridLayout()", source)
        self.assertIn("actions.addWidget(button, index // 3, index % 3)", source)
        self.assertIn("actions.setColumnStretch(column, 1)", source)


if __name__ == "__main__":
    unittest.main()
