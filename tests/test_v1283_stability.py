from pathlib import Path
import unittest
from unittest.mock import patch

from app import __version__
from app.memory_governor import (
    ECONOMY_ABSOLUTE_BYTES,
    ECONOMY_EMERGENCY_BYTES,
    MemoryGovernor,
)
from app.memory_metrics import ProcessMemoryInfo
from app.resource_profile import choose_profile

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 ** 2


class V1283StabilityTests(unittest.TestCase):
    def test_release_version(self):
        self.assertEqual(__version__, "1.28.5")

    def test_emergency_and_absolute_memory_states_block_new_heavy_work(self):
        profile = choose_profile(4, 4 * 1024**3, "economy")
        governor = MemoryGovernor(profile)
        for current, expected in (
            (ECONOMY_EMERGENCY_BYTES + 1, "emergency"),
            (ECONOMY_ABSOLUTE_BYTES + 1, "absolute"),
        ):
            with self.subTest(expected=expected), patch(
                "app.memory_governor.process_memory_info",
                return_value=ProcessMemoryInfo(current, current),
            ), patch(
                "app.memory_governor.system_memory_status",
                return_value=(4 * 1024**3, 500 * MIB),
            ):
                decision = governor.sample()
            self.assertEqual(decision.action, expected)
            self.assertTrue(decision.release_ocr)
            self.assertFalse(decision.allow_heavy_task)

    def test_finalize_batch_releases_engine_and_trims_only_at_safe_boundary(self):
        profile = choose_profile(4, 4 * 1024**3, "economy")
        governor = MemoryGovernor(profile)
        released = []
        low = ProcessMemoryInfo(650 * MIB, 1000 * MIB)
        with patch("app.memory_governor.gc.collect") as collect, patch(
            "app.memory_governor.trim_process_working_set", return_value=True
        ) as trim, patch(
            "app.memory_governor.process_memory_info", return_value=low
        ), patch(
            "app.memory_governor.system_memory_status", return_value=(4 * 1024**3, 800 * MIB)
        ):
            decision = governor.finalize_batch(lambda: released.append(True))
        self.assertEqual(released, [True])
        collect.assert_called()
        trim.assert_called_once()
        self.assertEqual(decision.action, "normal")

    def test_completed_dialog_resets_all_stale_batch_state_before_reuse(self):
        source = (ROOT / "app" / "import_dialog.py").read_text(encoding="utf-8")
        section = source.split("def prepare_for_new_batch", 1)[1].split("def retry_failed", 1)[0]
        for token in (
            "self._cancel_requested = False",
            "self._closing = False",
            "self._review_pending = False",
            "self._manual_review_active = False",
            "self._preflight_done_index = -1",
            "self._result_processed = False",
            "self._worker_finished = False",
            "self._continue_after_worker = False",
            "self.pdf_path = None",
            "self._failed_paths = []",
        ):
            self.assertIn(token, section)

    def test_economy_ocr_waits_for_heavy_slot_instead_of_failing_immediately(self):
        source = (ROOT / "app" / "import_workers.py").read_text(encoding="utf-8")
        self.assertIn('COORDINATOR.wait_start("ocr", timeout=30.0', source)
        self.assertIn("Ожидание свободного тяжёлого слота", source)
        self.assertIn("finalize_ocr_batch_memory", source)

    def test_ocr_work_remains_bounded_to_first_page_and_conditional_second_page(self):
        source = (ROOT / "app" / "ocr.py").read_text(encoding="utf-8")
        self.assertIn("page_indices = [0]", source)
        self.assertIn("len(text) < self.SECOND_PAGE_TEXT_THRESHOLD", source)
        self.assertNotIn("list(range(page_count))", source)


if __name__ == "__main__":
    unittest.main()
