import os
import unittest
from unittest.mock import patch

from app.resource_profile import choose_profile, configure_process_environment, resource_profile_from_environment


class ResourceProfileTests(unittest.TestCase):
    def test_weak_pc_stays_single_threaded(self):
        profile = choose_profile(2, 4 * 1024**3)
        self.assertEqual(profile.worker_threads, 1)
        self.assertLessEqual(profile.ocr_dpi, 145)

    def test_midrange_pc_uses_more_resources(self):
        weak = choose_profile(4, 8 * 1024**3)
        fast = choose_profile(12, 24 * 1024**3)
        self.assertGreater(fast.worker_threads, weak.worker_threads)
        self.assertGreater(fast.ocr_dpi, weak.ocr_dpi)

    def test_even_powerful_pc_keeps_headroom(self):
        profile = choose_profile(32, 64 * 1024**3)
        self.assertLessEqual(profile.worker_threads, 4)
        self.assertLess(profile.worker_threads, profile.logical_cpus)

    def test_low_ram_profile_uses_memory_efficient_ocr_cache_policy(self):
        low = choose_profile(4, 8 * 1024**3)
        high = choose_profile(32, 64 * 1024**3)
        self.assertTrue(low.memory_efficient_ocr)
        self.assertFalse(high.memory_efficient_ocr)

    def test_16gb_class_machine_uses_fast_memory_cache(self):
        profile = choose_profile(20, int(15.7 * 1024**3))
        self.assertFalse(profile.memory_efficient_ocr)
        self.assertEqual(profile.worker_threads, 2)

    def test_manual_modes_change_load_without_changing_quality_dpi(self):
        economy = choose_profile(20, 16 * 1024**3, "economy")
        auto = choose_profile(20, 16 * 1024**3, "auto")
        performance = choose_profile(20, 16 * 1024**3, "performance")
        self.assertEqual(economy.worker_threads, 1)
        self.assertEqual(auto.worker_threads, 2)
        self.assertGreater(performance.worker_threads, auto.worker_threads)
        self.assertEqual(economy.ocr_dpi, auto.ocr_dpi)
        self.assertEqual(performance.ocr_dpi, auto.ocr_dpi)
        self.assertTrue(economy.memory_efficient_ocr)
        self.assertFalse(performance.memory_efficient_ocr)


    def test_configure_environment_overwrites_stale_runtime_mode(self):
        performance = choose_profile(20, 16 * 1024**3, "performance")
        stale = {
            "DOCUMENT_REGISTRY_PERFORMANCE_MODE": "economy",
            "DOCUMENT_REGISTRY_OCR_THREADS": "1",
            "DOCUMENT_REGISTRY_OCR_MEMORY_EFFICIENT": "1",
        }
        with patch.dict(os.environ, stale, clear=False):
            configure_process_environment(performance)
            effective = resource_profile_from_environment()
            self.assertEqual(effective.performance_mode, "performance")
            self.assertEqual(effective.worker_threads, performance.worker_threads)
            self.assertFalse(effective.memory_efficient_ocr)



if __name__ == "__main__":
    unittest.main()
