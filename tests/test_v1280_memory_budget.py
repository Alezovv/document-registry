from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import __version__
from app.background_task_coordinator import BackgroundTaskCoordinator
from app.database import Database
from app.memory_governor import (
    ECONOMY_BLOCK_OCR_BYTES,
    ECONOMY_RELEASE_OCR_BYTES,
    MemoryGovernor,
)
from app.memory_metrics import ProcessMemoryInfo
from app.models import Request
from app.request_service import RequestService
from app.resource_profile import choose_profile


ROOT = Path(__file__).resolve().parents[1]


class V1280MemoryProfileTests(unittest.TestCase):
    def test_release_version_and_schema(self):
        self.assertEqual(__version__, "1.28.5")
        self.assertEqual(Database.SCHEMA_VERSION, 7)

    def test_auto_4gb_uses_strict_economy_policy(self):
        profile = choose_profile(4, 4 * 1024**3, "auto")
        self.assertTrue(profile.economy_memory_policy)
        self.assertEqual(profile.worker_threads, 1)
        self.assertLessEqual(profile.ocr_dpi, 125)
        self.assertLessEqual(profile.max_image_width, 1700)
        self.assertEqual(profile.memory_budget_mb, 1024)

    def test_auto_8gb_does_not_force_strict_budget(self):
        profile = choose_profile(4, 8 * 1024**3, "auto")
        self.assertFalse(profile.economy_memory_policy)
        self.assertEqual(profile.memory_budget_mb, 0)

    def test_governor_requests_ocr_release_under_pressure(self):
        profile = choose_profile(4, 4 * 1024**3, "economy")
        governor = MemoryGovernor(profile)
        releases = []
        samples = [
            ProcessMemoryInfo(ECONOMY_RELEASE_OCR_BYTES + 1, ECONOMY_RELEASE_OCR_BYTES + 1),
            ProcessMemoryInfo(800 * 1024**2, ECONOMY_RELEASE_OCR_BYTES + 1),
        ]
        with patch("app.memory_governor.process_memory_info", side_effect=samples), patch(
            "app.memory_governor.system_memory_status", return_value=(4 * 1024**3, 700 * 1024**2)
        ), patch("app.memory_governor.gc.collect"), patch(
            "app.memory_governor.trim_process_working_set", return_value=True
        ):
            decision = governor.prepare_heavy_task(lambda: releases.append(True), wait_seconds=0)
        self.assertTrue(releases)
        self.assertTrue(decision.allow_heavy_task)

    def test_governor_blocks_new_ocr_above_one_gb_after_cleanup(self):
        profile = choose_profile(4, 4 * 1024**3, "economy")
        governor = MemoryGovernor(profile)
        high = ProcessMemoryInfo(ECONOMY_BLOCK_OCR_BYTES + 20 * 1024**2, ECONOMY_BLOCK_OCR_BYTES + 20 * 1024**2)
        with patch("app.memory_governor.process_memory_info", return_value=high), patch(
            "app.memory_governor.system_memory_status", return_value=(4 * 1024**3, 200 * 1024**2)
        ), patch("app.memory_governor.gc.collect"), patch(
            "app.memory_governor.trim_process_working_set", return_value=True
        ):
            decision = governor.prepare_heavy_task(lambda: None, wait_seconds=0)
        self.assertFalse(decision.allow_heavy_task)

    def test_background_coordinator_allows_only_one_heavy_task(self):
        coordinator = BackgroundTaskCoordinator()
        self.assertTrue(coordinator.try_start("ocr"))
        self.assertFalse(coordinator.try_start("backup"))
        self.assertFalse(coordinator.try_start("ocr"))
        coordinator.finish("ocr")
        self.assertTrue(coordinator.try_start("backup"))

    def test_manual_economy_keeps_auto_ocr_quality_on_same_hardware(self):
        auto = choose_profile(20, 16 * 1024**3, "auto")
        economy = choose_profile(20, 16 * 1024**3, "economy")
        self.assertEqual(economy.ocr_dpi, auto.ocr_dpi)
        self.assertEqual(economy.retry_dpi, auto.retry_dpi)
        self.assertEqual(economy.max_image_width, auto.max_image_width)
        self.assertEqual(economy.max_image_height, auto.max_image_height)
        self.assertEqual(economy.worker_threads, 1)
        self.assertTrue(economy.economy_memory_policy)



class V1280LazyRegistryTests(unittest.TestCase):
    def test_registry_query_is_paged_and_casefolded_in_sqlite(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            db = Database(data_dir=root / "data")
            for index in range(175):
                db.add_request(
                    Request(
                        received_date="18.09.2026",
                        from_department="Отдел Кадров" if index == 174 else "Канцелярия",
                        from_person=f"Иванов {index}",
                        to_person="Петров",
                        request_subject=f"Тема {index}",
                        status="Новый",
                    )
                )
            first, total = db.query_requests_page(limit=150)
            second, total2 = db.query_requests_page(limit=150, offset=150)
            self.assertEqual(total, 175)
            self.assertEqual(total2, 175)
            self.assertEqual(len(first), 150)
            self.assertEqual(len(second), 25)
            filtered, filtered_total = db.query_requests_page(sender="отдел кадров", limit=20)
            self.assertEqual(filtered_total, 1)
            self.assertEqual(filtered[0].from_department, "Отдел Кадров")

    def test_transient_ocr_payload_is_released_after_persistence(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            db = Database(data_dir=root / "data")
            service = RequestService(database=db)
            request = Request(received_date="18.09.2026", request_subject="Тест")
            request._ocr_text = "Большой OCR текст"
            request._parser_diagnostics = {"field_results": {}}
            service.create_request(request)
            self.assertFalse(hasattr(request, "_ocr_text"))
            self.assertFalse(hasattr(request, "_parser_diagnostics"))
            self.assertEqual(db.get_request_ocr_text(request.id), "Большой OCR текст")


class V1280SourceArchitectureTests(unittest.TestCase):
    def test_worker_compacts_diagnostics_before_ui_and_cache(self):
        source = (ROOT / "app" / "import_workers.py").read_text(encoding="utf-8")
        self.assertIn("compact_parser_diagnostics(asdict(parser.last_diagnostics))", source)
        self.assertIn("prepare_ocr_memory_budget", source)
        self.assertIn('COORDINATOR.wait_start("ocr"', source)

    def test_registry_uses_paged_database_loading(self):
        source = (ROOT / "app" / "ui" / "registry_page.py").read_text(encoding="utf-8")
        self.assertIn("query_requests_page", source)
        self.assertIn("self._page_size = 150", source)
        self.assertIn("Показать ещё", source)

    def test_maintenance_uses_small_batches_instead_of_full_archive(self):
        search_source = (ROOT / "app" / "search_index_worker.py").read_text(encoding="utf-8")
        fingerprint_source = (ROOT / "app" / "document_fingerprint.py").read_text(encoding="utf-8")
        self.assertIn("get_requests_missing_ocr_text_page", search_source)
        self.assertNotIn("for request in self.request_service.get_requests()", search_source)
        self.assertIn("backfill_missing(limit=3)", fingerprint_source)

    def test_release_build_rejects_dev_gold_and_tests_in_portable(self):
        build = (ROOT / "packaging" / "build.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("Assert-PortableRuntimeClean", build)
        self.assertIn('"tests", "tools", "gold", "gold_dataset"', build)
        self.assertIn("Developer-only content leaked into portable runtime", build)

    def test_normal_runtime_entrypoints_do_not_import_gold_dataset(self):
        for relative in ("app/main.py", "app/window.py"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("app.gold_dataset", source)


if __name__ == "__main__":
    unittest.main()
