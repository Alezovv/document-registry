from __future__ import annotations

import unittest

from app.document_parser import PARSER_VERSION
from app.parser_core import CandidateResolver, FieldCandidate, PageZone


class ResolverPolicyV1206Tests(unittest.TestCase):
    def test_parser_version(self):
        self.assertEqual(PARSER_VERSION, "1.22.1")

    def test_conflicting_ner_does_not_downgrade_deterministic_department(self):
        result = CandidateResolver.result_for_selected(
            "from_department",
            [
                FieldCandidate(
                    "from_department", "ГУ МВД России по Московской области",
                    confidence=0.88, source="layout/heuristic", zone=PageZone.HEADER.value,
                ),
                FieldCandidate(
                    "from_department", "Прокуратура Московской области",
                    confidence=0.96, source="ner", zone=PageZone.HEADER.value,
                ),
            ],
            selected_value="ГУ МВД России по Московской области",
            selected_confidence=0.88,
            selected_source="layout/heuristic",
        )
        self.assertEqual(result.decision, "autofill")
        self.assertGreaterEqual(result.margin, 0.07)
        self.assertTrue(result.alternatives)
        self.assertIn("advisory_ner_alternative", result.warnings)

    def test_conflicting_ner_does_not_downgrade_deterministic_addressee(self):
        result = CandidateResolver.result_for_selected(
            "to_person",
            [
                FieldCandidate(
                    "to_person", "Иванову И.И.",
                    confidence=0.90, source="label/layout", zone=PageZone.ADDRESSEE.value,
                ),
                FieldCandidate(
                    "to_person", "Петрову П.П.",
                    confidence=0.97, source="ner", zone=PageZone.ADDRESSEE.value,
                ),
            ],
            selected_value="Иванову И.И.",
            selected_confidence=0.90,
            selected_source="label/layout",
        )
        self.assertEqual(result.decision, "autofill")
        self.assertTrue(result.alternatives)

    def test_real_non_ner_competitor_still_forces_review(self):
        result = CandidateResolver.result_for_selected(
            "from_department",
            [
                FieldCandidate(
                    "from_department", "ГУ МВД России по Московской области",
                    confidence=0.89, source="layout/heuristic", zone=PageZone.HEADER.value,
                ),
                FieldCandidate(
                    "from_department", "Прокуратура Московской области",
                    confidence=0.88, source="filename", zone=PageZone.UNKNOWN.value,
                ),
            ],
            selected_value="ГУ МВД России по Московской области",
            selected_confidence=0.89,
            selected_source="layout/heuristic",
        )
        self.assertEqual(result.decision, "review")
        self.assertIn("close_alternative", result.warnings)


if __name__ == "__main__":
    unittest.main()
