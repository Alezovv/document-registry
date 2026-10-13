from __future__ import annotations

import unittest
from unittest.mock import patch

from app.document_parser import DocumentParser, PARSER_VERSION
from app.hybrid_parser import HybridChoice
from app.ner import HeuristicRussianNerEngine
from tools.benchmark_parser_gold import values_compatible


class GoldBenchmarkV1211Tests(unittest.TestCase):
    def test_parser_algorithm_version_stays_stable(self):
        # v1.22.0 changes production parser semantics and therefore bumps parser-cache compatibility.
        self.assertEqual(PARSER_VERSION, "1.22.1")

    def test_person_compatibility_accepts_initials_for_same_surname(self):
        self.assertTrue(values_compatible("to_person", "Иванов И.И.", "Иванов Иван Иванович"))
        self.assertFalse(values_compatible("to_person", "Иванов И.И.", "Петров Иван Иванович"))

    def test_subject_compatibility_requires_strong_token_overlap(self):
        self.assertTrue(values_compatible(
            "request_subject",
            "Предоставить сведения о лечении пациента",
            "Просим предоставить сведения о лечении пациента Иванова",
        ))
        self.assertFalse(values_compatible(
            "request_subject",
            "Предоставить сведения о лечении пациента",
            "Направить копию постановления суда",
        ))

    def test_hybrid_disabled_keeps_baseline_value(self):
        original_choose = __import__("app.hybrid_parser", fromlist=["HybridFieldResolver"]).HybridFieldResolver.choose

        def force_change(field_name, candidates, *, baseline_value, baseline_confidence, baseline_source):
            if field_name == "request_subject":
                return HybridChoice(
                    field=field_name,
                    value="ПРИНУДИТЕЛЬНОЕ ИЗМЕНЕНИЕ",
                    confidence=0.99,
                    source="layout/heuristic",
                    changed=True,
                    reason="test",
                    candidate_count=len(list(candidates)),
                )
            return original_choose(
                field_name, candidates,
                baseline_value=baseline_value,
                baseline_confidence=baseline_confidence,
                baseline_source=baseline_source,
            )

        text = "Просим предоставить сведения о лечении пациента Иванова."
        with patch("app.document_parser.HybridFieldResolver.choose", side_effect=force_change):
            production = DocumentParser(
                ner_engine=HeuristicRussianNerEngine(), hybrid_enabled=True
            ).parse(text)
            baseline = DocumentParser(
                ner_engine=HeuristicRussianNerEngine(), hybrid_enabled=False
            ).parse(text)

        self.assertEqual(production.request_subject, "ПРИНУДИТЕЛЬНОЕ ИЗМЕНЕНИЕ")
        self.assertNotEqual(baseline.request_subject, "ПРИНУДИТЕЛЬНОЕ ИЗМЕНЕНИЕ")


if __name__ == "__main__":
    unittest.main()
