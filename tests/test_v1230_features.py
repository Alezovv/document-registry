from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import __version__
from app.database import Database
from app.db_migrations import CURRENT_SCHEMA_VERSION
from app.models import Request
from app.update_history import load_update_history
from app.update_service import UpdateService
from app.updater_status import UpdaterStatusReporter


class V1230DatabaseSearchTests(unittest.TestCase):
    def test_release_and_schema_versions(self):
        self.assertEqual(__version__, "1.28.5")
        self.assertEqual(CURRENT_SCHEMA_VERSION, 7)

    def test_fts_finds_request_by_ocr_text_and_metadata(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            db = Database(data_dir=root / "data")
            request = Request(
                received_date="18.09.2026",
                from_department="Административный отдел",
                request_subject="Обращение гражданина",
            )
            request_id = db.add_request(request)
            db.set_request_ocr_text(
                request_id,
                "Просим выполнить капитальный ремонт кровли здания школы.",
            )

            self.assertEqual(db.search_request_ids_full_text("кровл"), [request_id])
            self.assertEqual(db.search_request_ids_full_text("административ"), [request_id])
            stats = db.get_search_index_stats()
            self.assertEqual(stats["requests"], 1)
            self.assertEqual(stats["indexed"], 1)
            self.assertEqual(stats["with_ocr"], 1)

    def test_fts_tracks_request_updates(self):
        with tempfile.TemporaryDirectory() as name:
            db = Database(data_dir=Path(name) / "data")
            request = Request(received_date="18.09.2026", request_subject="Старый предмет")
            request.id = db.add_request(request)
            request.request_subject = "Новый уникальный предмет"
            db.update_request(request)
            self.assertEqual(db.search_request_ids_full_text("уникаль"), [request.id])
            self.assertEqual(db.search_request_ids_full_text("старый"), [])


class V1230UpdateHistoryTests(unittest.TestCase):
    def test_updater_status_writes_durable_stage_history(self):
        with tempfile.TemporaryDirectory() as name:
            target = Path(name)
            reporter = UpdaterStatusReporter(target, "1.23.0", show_window=False)
            reporter.stage("verify_package", "Проверяем пакет", 12)
            reporter.stage("backup", "Создаём backup", 45)
            reporter.success()

            rows = load_update_history(target / "data", limit=10)
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["state"], "success")
            self.assertEqual(rows[1]["stage"], "backup")
            self.assertEqual(rows[2]["stage"], "verify_package")
            self.assertTrue(all(row.get("session_id") for row in rows))

    def test_legacy_terminal_status_is_promoted_to_history_once(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            data = root / "data"
            data.mkdir(parents=True)
            status = {
                "format_version": 1,
                "state": "success",
                "stage": "done",
                "message": "Установлено",
                "progress": 100,
                "version": "1.23.0",
                "updated_at": "2026-09-18T16:00:00+0300",
                "notified": False,
            }
            (data / "update_status.json").write_text(
                json.dumps(status, ensure_ascii=False), encoding="utf-8"
            )

            from types import SimpleNamespace
            paths = SimpleNamespace(runtime=root, data=data)
            service = UpdateService(paths)
            self.assertIsNotNone(service.consume_last_install_status())
            self.assertIsNone(service.consume_last_install_status())
            rows = load_update_history(data, limit=10)
            terminal = [row for row in rows if row.get("state") == "success"]
            self.assertEqual(len(terminal), 1)


class V1230UiSourceTests(unittest.TestCase):
    def test_registry_all_fields_uses_full_text_search(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "ui" / "registry_page.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("search_request_hits_full_text", source)
        self.assertIn("распознанному тексту PDF", source)

    def test_import_dialog_exposes_queue_progress_and_pause(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "import_dialog.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("self.queue_table", source)
        self.assertIn("self.overall_progress", source)
        self.assertIn("def toggle_pause", source)
        self.assertIn("Пауза после текущего", source)

    def test_diagnostics_center_and_history_are_wired(self):
        root = Path(__file__).resolve().parents[1]
        settings = (root / "app" / "ui" / "settings_page.py").read_text(encoding="utf-8")
        controller = (root / "app" / "ui" / "diagnostics_controller.py").read_text(encoding="utf-8")
        dialog = (root / "app" / "ui" / "diagnostics_dialog.py").read_text(encoding="utf-8")
        self.assertIn("diagnostics_center_requested", settings)
        self.assertIn("def open_center", controller)
        self.assertIn("История обновлений", dialog)
        self.assertIn("Полнотекстовый индекс", dialog)


if __name__ == "__main__":
    unittest.main()
