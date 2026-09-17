import sys
import types
import unittest
from unittest.mock import Mock, patch

from app.performance_mode_service import apply_performance_mode
from app.resource_profile import choose_profile


class PerformanceModeServiceTests(unittest.TestCase):
    def test_applies_profile_to_environment_and_diagnostics_immediately(self):
        profile = choose_profile(20, 16 * 1024**3, "performance")
        diagnostics = Mock()
        with patch("app.performance_mode_service.detect_resource_profile", return_value=profile), \
             patch("app.performance_mode_service.configure_process_environment") as configure:
            result = apply_performance_mode("performance", diagnostics=diagnostics)

        self.assertIs(result.profile, profile)
        configure.assert_called_once_with(profile)
        diagnostics.set_profile.assert_called_once_with(profile)
        self.assertFalse(result.model_reset)

    def test_loaded_ocr_engine_is_reset_when_engine_profile_changes(self):
        auto = choose_profile(20, 16 * 1024**3, "auto")
        performance = choose_profile(20, 16 * 1024**3, "performance")

        fake_ocr = type("FakeOCR", (), {
            "_shared_ocr": object(),
            "reset_shared_engine": Mock(return_value=True),
        })
        fake_module = types.SimpleNamespace(OCR=fake_ocr)

        with patch("app.performance_mode_service.detect_resource_profile", return_value=performance), \
             patch("app.performance_mode_service.configure_process_environment"), \
             patch.dict(sys.modules, {"app.ocr": fake_module}):
            result = apply_performance_mode("performance", current_profile=auto)

        self.assertTrue(result.model_reset)
        fake_ocr.reset_shared_engine.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
