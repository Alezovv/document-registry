import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from app.ocr import OCR
from app.resource_profile import ResourceProfile


class OCRLazyTests(unittest.TestCase):
    def test_native_text_pdf_does_not_initialize_paddle_engine(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "native.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "Native PDF text that is long enough for direct extraction")
            document.save(pdf_path)
            document.close()

            ocr = OCR()
            with patch.object(ocr, "_get_engine", side_effect=AssertionError("OCR engine should stay lazy")):
                result = ocr.recognize_pdf(pdf_path)

            self.assertGreater(len(result["text"]), 30)

    def test_ocr_engine_is_shared_and_low_ram_disables_large_onnx_caches(self):
        calls = []

        def fake_paddle_ocr(**kwargs):
            calls.append(kwargs)
            return object()

        fake_module = types.SimpleNamespace(PaddleOCR=fake_paddle_ocr)
        old_engine = OCR._shared_ocr
        old_models = OCR._shared_models_dir
        old_signature = OCR._shared_engine_signature
        OCR._shared_ocr = None
        OCR._shared_models_dir = None
        OCR._shared_engine_signature = None
        try:
            with patch.dict(sys.modules, {"paddleocr": fake_module}):
                first = OCR()
                second = OCR()
                low_profile = ResourceProfile(
                    "test", 4, 8.0, 1, 145, 185, 2100, 3000
                )
                first.resource_profile = low_profile
                first.cpu_threads = 1
                second.resource_profile = low_profile
                second.cpu_threads = 1
                with patch.object(OCR, "_validate_models", return_value=None):
                    engine1 = first._get_engine()
                    engine2 = second._get_engine()

            self.assertIs(engine1, engine2)
            self.assertEqual(len(calls), 1)
            config = calls[0]["engine_config"]
            self.assertFalse(config["enable_cpu_mem_arena"])
            self.assertFalse(config["enable_mem_pattern"])
        finally:
            OCR._shared_ocr = old_engine
            OCR._shared_models_dir = old_models
            OCR._shared_engine_signature = old_signature

    def test_shared_engine_reloads_when_runtime_profile_changes(self):
        calls = []

        def fake_paddle_ocr(**kwargs):
            engine = object()
            calls.append((kwargs, engine))
            return engine

        fake_module = types.SimpleNamespace(PaddleOCR=fake_paddle_ocr)
        old_engine = OCR._shared_ocr
        old_models = OCR._shared_models_dir
        old_signature = OCR._shared_engine_signature
        OCR._shared_ocr = None
        OCR._shared_models_dir = None
        OCR._shared_engine_signature = None
        try:
            with patch.dict(sys.modules, {"paddleocr": fake_module}), patch.object(OCR, "_validate_models", return_value=None):
                economy = OCR()
                economy.resource_profile = ResourceProfile(
                    "test", 20, 16.0, 1, 155, 200, 2400, 3400, "economy", True
                )
                economy.cpu_threads = 1
                first = economy._get_engine()

                performance = OCR()
                performance.resource_profile = ResourceProfile(
                    "test", 20, 16.0, 3, 155, 200, 2400, 3400, "performance", False
                )
                performance.cpu_threads = 3
                second = performance._get_engine()

            self.assertIsNot(first, second)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][0]["engine_config"]["intra_op_num_threads"], 1)
            self.assertEqual(calls[1][0]["engine_config"]["intra_op_num_threads"], 3)
            self.assertTrue(calls[0][0]["engine_config"]["enable_cpu_mem_arena"] is False)
            self.assertTrue(calls[1][0]["engine_config"]["enable_cpu_mem_arena"] is True)
        finally:
            OCR._shared_ocr = old_engine
            OCR._shared_models_dir = old_models
            OCR._shared_engine_signature = old_signature

    def test_reset_shared_engine_reports_loaded_state(self):
        old_engine = OCR._shared_ocr
        old_models = OCR._shared_models_dir
        old_signature = OCR._shared_engine_signature
        try:
            OCR._shared_ocr = object()
            OCR._shared_models_dir = Path("models")
            OCR._shared_engine_signature = (2, False)
            self.assertTrue(OCR.reset_shared_engine())
            self.assertIsNone(OCR._shared_ocr)
            self.assertIsNone(OCR._shared_engine_signature)
            self.assertFalse(OCR.reset_shared_engine())
        finally:
            OCR._shared_ocr = old_engine
            OCR._shared_models_dir = old_models
            OCR._shared_engine_signature = old_signature

    def test_confidence_helpers_trigger_retry_signal_for_uncertain_text(self):
        blocks = [{"confidence": 0.40}, {"confidence": 0.60}, {"confidence": None}]
        confidence = OCR._average_confidence(blocks)
        self.assertAlmostEqual(confidence, 0.50)
        self.assertLess(confidence, OCR.RETRY_CONFIDENCE_THRESHOLD)
        self.assertGreater(
            OCR._combined_ocr_score(0.8, 0.9),
            OCR._combined_ocr_score(0.8, 0.4),
        )



class OCRQualityTests(unittest.TestCase):
    def test_retry_enhancement_returns_three_channel_uint8(self):
        import numpy as np

        image = np.zeros((20, 30, 3), dtype=np.uint8)
        image[:, :15] = 40
        image[:, 15:] = 170
        enhanced = OCR._enhance_image_for_retry(image)
        self.assertEqual(enhanced.shape, (20, 30, 3))
        self.assertEqual(enhanced.dtype, np.uint8)
        self.assertTrue(enhanced.flags["C_CONTIGUOUS"])
        self.assertGreater(int(enhanced.max()) - int(enhanced.min()), 150)

    def test_broken_native_text_layer_falls_back_to_ocr(self):
        class FakePage:
            def get_text(self, mode):
                self_mode = mode
                return [(0, 0, 100, 20, "\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd" * 4, 0, 0)]

        ocr = OCR()
        with patch.object(ocr, "_ocr_page", return_value=("Нормально распознанный текст документа", [])) as mocked:
            text, _ = ocr._extract_page([FakePage()], 0)
        mocked.assert_called_once()
        self.assertIn("Нормально", text)

    def test_retry_tuning_uses_more_permissive_detection_thresholds(self):
        import numpy as np

        class FakeEngine:
            def __init__(self):
                self.kwargs = None

            def predict(self, image, **kwargs):
                self.kwargs = kwargs
                return []

        engine = FakeEngine()
        ocr = OCR()
        dummy_pixmap = object()
        image = np.full((12, 12, 3), 240, dtype=np.uint8)
        with patch.object(ocr, "_render_page", return_value=dummy_pixmap), \
             patch.object(ocr, "_pixmap_to_numpy", return_value=image), \
             patch.object(ocr, "_enhance_image_for_retry", return_value=image), \
             patch.object(ocr, "_get_engine", return_value=engine):
            ocr._ocr_page(object(), 200, enhance=True, retry_tuning=True)

        self.assertIsNotNone(engine.kwargs)
        self.assertLess(engine.kwargs["text_det_box_thresh"], 0.6)
        self.assertGreater(engine.kwargs["text_rec_score_thresh"], 0.0)


if __name__ == "__main__":
    unittest.main()
