from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

import numpy as np

from app.document_parser import DocumentParser, PARSER_VERSION, get_parser_cache_version
from app.ner import HeuristicRussianNerEngine, NerEngine, NerEntity, NerLabel
from app.ner_onnx import BertWordPieceTokenizer, OnnxRussianNerEngine
from app.ner_runtime import (
    FallbackNerEngine,
    create_default_ner_engine,
    current_engine_cache_key,
    engine_cache_key,
    reset_default_ner_engine_for_tests,
)


class _Input:
    def __init__(self, name: str):
        self.name = name


class _FakeSession:
    def get_inputs(self):
        return [_Input("input_ids"), _Input("attention_mask"), _Input("token_type_ids")]

    def run(self, _outputs, feed):
        length = int(feed["input_ids"].shape[1])
        logits = np.zeros((1, length, 11), dtype=np.float32)
        logits[:, :, 0] = 5.0
        # [CLS], Иванов, [SEP]
        logits[0, 1, 0] = 0.0
        logits[0, 1, 9] = 8.0
        return [logits]


class _FailingEngine(NerEngine):
    name = "failing"

    def __init__(self):
        self.calls = 0

    def extract(self, text, blocks=()):
        self.calls += 1
        raise RuntimeError("synthetic failure")


class _MetricsEngine(NerEngine):
    name = "metrics-test"

    def __init__(self):
        self.last_metrics = {}

    def extract(self, text, blocks=()):
        self.last_metrics = {
            "engine": self.name,
            "active_engine": self.name,
            "loaded": True,
            "inference_ms": 12.5,
            "rss_delta_mb": 18.0,
        }
        return [NerEntity("Иванов И.И.", NerLabel.PERSON.value, 0.95, engine=self.name)]


class NerV1201Tests(unittest.TestCase):
    def tearDown(self):
        reset_default_ner_engine_for_tests()

    def test_parser_version(self):
        self.assertEqual(PARSER_VERSION, "1.22.1")

    def test_wordpiece_tokenizer_preserves_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            vocab = Path(tmp) / "vocab.txt"
            vocab.write_text("[PAD]\n[UNK]\n[CLS]\n[SEP]\nИван\n##ов\n,\n", encoding="utf-8")
            tokenizer = BertWordPieceTokenizer(vocab)
            encoded = tokenizer.encode("Иванов,")
            self.assertEqual(encoded.tokens, ["[CLS]", "Иван", "##ов", ",", "[SEP]"])
            self.assertEqual(encoded.offsets[1:4], [(0, 4), (4, 6), (6, 7)])

    def test_onnx_engine_is_lazy_and_maps_per_to_person(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp)
            vocab = model_dir / "vocab.txt"
            vocab.write_text("[PAD]\n[UNK]\n[CLS]\n[SEP]\nИванов\n", encoding="utf-8")
            engine = OnnxRussianNerEngine(model_dir)
            self.assertFalse(engine._loaded)
            engine._loaded = True
            engine._np = np
            engine._session = _FakeSession()
            engine._tokenizer = BertWordPieceTokenizer(vocab)
            engine._id2label = {0: "O", 9: "B-PER"}
            engine._model_path = model_dir / "model_int8.onnx"
            entities = engine.extract("Иванов")
            self.assertEqual(len(entities), 1)
            self.assertEqual(entities[0].text, "Иванов")
            self.assertEqual(entities[0].label, NerLabel.PERSON.value)
            self.assertGreater(entities[0].confidence, 0.9)
            self.assertEqual(engine.last_metrics["active_engine"], engine.name)

    def test_missing_model_uses_heuristic_in_auto_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_default_ner_engine(mode="auto", model_root=tmp)
            self.assertIsInstance(engine, HeuristicRussianNerEngine)
            self.assertEqual(engine_cache_key(mode="auto", model_root=tmp), "heuristic-ru-v1")

    def test_runtime_failure_falls_back_and_is_not_retried(self):
        primary = _FailingEngine()
        engine = FallbackNerEngine(primary, HeuristicRussianNerEngine())
        blocks = [{
            "text": "Иванов И.И.",
            "x1": 10, "y1": 10, "x2": 100, "y2": 30,
            "page_width": 1000, "page_height": 1400,
            "confidence": 0.95, "page_index": 0,
        }]
        first = engine.extract("", blocks)
        second = engine.extract("", blocks)
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(primary.calls, 1)
        self.assertTrue(engine.last_metrics["fallback_used"])
        self.assertIn("synthetic failure", engine.last_metrics["fallback_reason"])

    def test_parser_exposes_runtime_metrics(self):
        parser = DocumentParser(ner_engine=_MetricsEngine())
        parser.parse("Просим предоставить сведения об Иванове И.И.")
        self.assertEqual(parser.last_diagnostics.ner_engine, "metrics-test")
        self.assertEqual(parser.last_diagnostics.ner_metrics["inference_ms"], 12.5)
        self.assertEqual(parser.last_diagnostics.ner_metrics["rss_delta_mb"], 18.0)

    def test_parser_cache_version_contains_backend_key(self):
        version = get_parser_cache_version()
        self.assertEqual(version, f"{PARSER_VERSION}|{current_engine_cache_key()}")


if __name__ == "__main__":
    unittest.main()
