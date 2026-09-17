import unittest
from unittest.mock import patch

from app.performance_diagnostics import (
    PerformanceDiagnostics,
    _process_working_set_bytes,
    format_memory,
)
from app.resource_profile import choose_profile


class PerformanceDiagnosticsTests(unittest.TestCase):
    def test_snapshot_exposes_selected_resource_profile(self):
        profile = choose_profile(8, 16 * 1024**3)
        diagnostics = PerformanceDiagnostics(profile)
        with patch("app.performance_diagnostics._memory_status", return_value=(16 * 1024**3, 8 * 1024**3)), \
             patch("app.performance_diagnostics._process_working_set_bytes", return_value=512 * 1024**2), \
             patch("app.performance_diagnostics._ocr_model_loaded", return_value=True):
            snapshot = diagnostics.snapshot()
        self.assertEqual(snapshot.profile_name, profile.name)
        self.assertEqual(snapshot.performance_mode, profile.performance_mode_label)
        self.assertEqual(snapshot.ocr_threads, profile.worker_threads)
        self.assertEqual(snapshot.process_ram_bytes, 512 * 1024**2)
        self.assertTrue(snapshot.ocr_model_loaded)



    def test_profile_can_be_updated_live(self):
        auto = choose_profile(20, 16 * 1024**3, "auto")
        performance = choose_profile(20, 16 * 1024**3, "performance")
        diagnostics = PerformanceDiagnostics(auto)
        diagnostics.set_profile(performance)
        with patch("app.performance_diagnostics._memory_status", return_value=(16 * 1024**3, 8 * 1024**3)), \
             patch("app.performance_diagnostics._process_working_set_bytes", return_value=512 * 1024**2), \
             patch("app.performance_diagnostics._ocr_model_loaded", return_value=False):
            snapshot = diagnostics.snapshot()
        self.assertEqual(snapshot.performance_mode, "Производительный")
        self.assertEqual(snapshot.ocr_threads, performance.worker_threads)

    def test_process_memory_uses_windows_reader_on_windows(self):
        expected = 734 * 1024**2
        with patch("app.performance_diagnostics.platform.system", return_value="Windows"), \
             patch("app.performance_diagnostics._windows_process_working_set_bytes", return_value=expected):
            self.assertEqual(_process_working_set_bytes(), expected)

    def test_format_memory_is_human_readable(self):
        self.assertEqual(format_memory(512 * 1024**2), "512 МБ")
        self.assertEqual(format_memory(8 * 1024**3), "8.0 ГБ")


if __name__ == "__main__":
    unittest.main()
