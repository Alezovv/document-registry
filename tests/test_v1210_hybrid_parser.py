from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.document_parser import DocumentParser, PARSER_VERSION, get_parser_cache_version
from app.hybrid_parser import HybridFieldResolver
from app.models import Request
from app.parser_core import FieldCandidate, PageZone
from app.parser_correction_memory import ParserCorrectionMemory


class HybridParserV1210Tests(unittest.TestCase):
    def test_parser_version(self):
        self.assertEqual(PARSER_VERSION, "1.22.1")

    def test_exact_correction_memory_can_override_heuristic_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            memory = ParserCorrectionMemory(Path(temp) / "parser_corrections.json")
            before = Request(from_person="Иванов Иван")
            after = Request(from_person="Иванов Иван Иванович")
            self.assertEqual(memory.learn(before, after), 1)
            candidates = [FieldCandidate(
                "from_person", "Иванов Иван", confidence=0.82,
                source="layout/heuristic", zone=PageZone.SIGNATURE.value,
            )]
            candidates = HybridFieldResolver.add_correction_memory_candidates(
                "from_person", candidates, memory, context={"document_type": "request"}
            )
            choice = HybridFieldResolver.choose(
                "from_person", candidates,
                baseline_value="Иванов Иван", baseline_confidence=0.82,
                baseline_source="layout/heuristic",
            )
            self.assertTrue(choice.changed)
            self.assertEqual(choice.value, "Иванов Иван Иванович")
            self.assertEqual(choice.source, "correction_memory")

    def test_ner_cannot_override_existing_deterministic_value(self):
        candidates = [
            FieldCandidate(
                "to_person", "Иванов И.И.", confidence=0.86,
                source="layout/heuristic", zone=PageZone.ADDRESSEE.value,
            ),
            FieldCandidate(
                "to_person", "Петров П.П.", confidence=0.98,
                source="ner", zone=PageZone.ADDRESSEE.value,
                extractors=["ner", "ml", "heuristic-ru-v1"],
                metadata={"support_sources": ["ml", "heuristic-ru-v1"]},
            ),
        ]
        choice = HybridFieldResolver.choose(
            "to_person", candidates,
            baseline_value="Иванов И.И.", baseline_confidence=0.86,
            baseline_source="layout/heuristic",
        )
        self.assertFalse(choice.changed)
        self.assertEqual(choice.value, "Иванов И.И.")

    def test_cache_version_changes_with_memory_fingerprint(self):
        first = get_parser_cache_version("aaa")
        second = get_parser_cache_version("bbb")
        self.assertNotEqual(first, second)
        self.assertIn("memory:aaa", first)

    def test_document_parser_exposes_hybrid_diagnostics(self):
        parser = DocumentParser()
        parser.parse("Просим предоставить сведения о пациенте")
        diagnostics = parser.last_diagnostics
        self.assertIn("request_subject", diagnostics.hybrid_actions)
        self.assertIn("request_subject", diagnostics.hybrid_candidate_counts)
        self.assertIsInstance(diagnostics.hybrid_changed_fields, list)


if __name__ == "__main__":
    unittest.main()
