from __future__ import annotations

import unittest

from app.document_parser import DocumentParser, PARSER_VERSION
from app.ner import NerEngine, NerEntity, NerLabel, compare_entity_sets
from app.parser_core import CandidateResolver, FieldCandidate, PageZone


def block(text, x1, y1, x2, y2, *, w=1000, h=1400, confidence=0.96, page=0):
    return {
        "text": text,
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "page_width": w,
        "page_height": h,
        "confidence": confidence,
        "page_index": page,
    }


class _FakeMlEngine(NerEngine):
    name = "fake-ml"

    def __init__(self):
        self.last_metrics = {}

    def extract(self, text, blocks=()):
        materialized = list(blocks)
        source = next((item for item in materialized if "Иванов И.И." in str(item.get("text", ""))), {})
        self.last_metrics = {
            "engine": self.name,
            "active_engine": self.name,
            "loaded": True,
            "inference_ms": 4.2,
        }
        return [NerEntity(
            "Иванов И.И.",
            NerLabel.PERSON.value,
            0.91,
            zone=PageZone.ADDRESSEE.value,
            source_blocks=[source] if source else [],
            engine=self.name,
        )]


class CalibrationV1202Tests(unittest.TestCase):
    def test_parser_version(self):
        self.assertEqual(PARSER_VERSION, "1.22.1")

    def test_layout_prior_prevents_addressee_from_becoming_signer(self):
        result = CandidateResolver.resolve("from_person", [
            FieldCandidate(
                "from_person", "Неверный Н.Н.", confidence=0.88, source="ner",
                zone=PageZone.ADDRESSEE.value, extractors=["ner", "fake-ml"],
            ),
            FieldCandidate(
                "from_person", "Петров П.П.", confidence=0.84, source="ner",
                zone=PageZone.SIGNATURE.value, extractors=["ner", "fake-ml"],
            ),
        ])
        self.assertEqual(result.value, "Петров П.П.")
        self.assertIn("zone:signature", result.evidence)

    def test_independent_engine_agreement_raises_calibrated_score(self):
        single = CandidateResolver.resolve("to_person", [
            FieldCandidate(
                "to_person", "Иванов И.И.", confidence=0.80, source="ner",
                zone=PageZone.ADDRESSEE.value, extractors=["ner", "fake-ml"],
                metadata={"ner_engine": "fake-ml"},
            ),
        ])
        agreed = CandidateResolver.resolve("to_person", [
            FieldCandidate(
                "to_person", "Иванов И.И.", confidence=0.80, source="ner",
                zone=PageZone.ADDRESSEE.value, extractors=["ner", "fake-ml"],
                metadata={"ner_engine": "fake-ml"},
            ),
            FieldCandidate(
                "to_person", "Иванов И.И.", confidence=0.79, source="ner",
                zone=PageZone.ADDRESSEE.value, extractors=["ner", "heuristic-ru-v1"],
                metadata={"ner_engine": "heuristic-ru-v1"},
            ),
        ])
        self.assertGreater(agreed.confidence, single.confidence)
        self.assertTrue(any(item.startswith("agreement:") for item in agreed.evidence))

    def test_close_competitor_forces_review(self):
        result = CandidateResolver.resolve("from_person", [
            FieldCandidate(
                "from_person", "Иванов И.И.", confidence=0.85,
                source="layout/heuristic", zone=PageZone.SIGNATURE.value,
            ),
            FieldCandidate(
                "from_person", "Петров П.П.", confidence=0.82,
                source="layout/heuristic", zone=PageZone.SIGNATURE.value,
            ),
        ])
        self.assertEqual(result.decision, "review")
        self.assertLess(result.margin, 0.07)
        self.assertIn("close_alternative", result.warnings)

    def test_entity_comparison_is_not_silently_treated_as_accuracy(self):
        primary = [
            NerEntity("Иванов И.И.", NerLabel.PERSON.value, 0.9, engine="ml"),
            NerEntity("ГУ МВД", NerLabel.ORGANIZATION.value, 0.8, engine="ml"),
        ]
        baseline = [
            NerEntity("Иванов И.И.", NerLabel.PERSON.value, 0.86, engine="heuristic"),
            NerEntity("Москва", NerLabel.LOCATION.value, 0.8, engine="heuristic"),
        ]
        comparison = compare_entity_sets(primary, baseline, primary_name="ml")
        self.assertEqual(comparison["overlap"], 1)
        self.assertEqual(comparison["union"], 3)
        self.assertAlmostEqual(comparison["agreement_ratio"], 1 / 3, places=3)
        self.assertEqual(comparison["by_label"]["PERSON"]["overlap"], 1)

    def test_parser_runs_cheap_shadow_baseline_for_ml_and_exposes_decisions(self):
        parser = DocumentParser(ner_engine=_FakeMlEngine())
        parser.parse({
            "text": "Кому: Иванов И.И.\nПросим предоставить сведения.",
            "blocks": [
                block("Кому: Иванов И.И.", 620, 300, 900, 340),
                block("Просим предоставить сведения.", 100, 560, 850, 600),
            ],
        })
        diagnostics = parser.last_diagnostics
        self.assertEqual(diagnostics.ner_engine, "fake-ml")
        self.assertEqual(diagnostics.ner_comparison["overlap"], 1)
        self.assertGreaterEqual(diagnostics.ner_hybrid_entity_counts.get("PERSON", 0), 2)
        self.assertIn("to_person", diagnostics.field_decisions)
        self.assertIn("decision", diagnostics.field_results["to_person"])
        self.assertIn("margin", diagnostics.field_results["to_person"])
        self.assertIn("evidence", diagnostics.field_results["to_person"])


if __name__ == "__main__":
    unittest.main()
