from __future__ import annotations

import unittest

from app.document_parser import DocumentParser, PARSER_VERSION
from app.ner import HeuristicRussianNerEngine, NerEntity, NerLabel, select_trusted_ml_entities
from app.parser_core import CandidateResolver, FieldCandidate, PageZone


class CalibrationV1205Tests(unittest.TestCase):
    def test_parser_version(self):
        self.assertEqual(PARSER_VERSION, "1.22.1")

    def test_location_stays_diagnostic_only_in_production(self):
        ml = NerEntity("Москва", NerLabel.LOCATION.value, 0.99, zone=PageZone.HEADER.value, engine="onnx")
        baseline = NerEntity("Москва", NerLabel.LOCATION.value, 0.90, zone=PageZone.HEADER.value, engine=HeuristicRussianNerEngine.name)
        self.assertEqual(select_trusted_ml_entities([ml], [baseline]), [])

    def test_person_confirmation_requires_same_role_zone(self):
        ml = NerEntity("Иванов И.И.", NerLabel.PERSON.value, 0.92, zone=PageZone.SIGNATURE.value, engine="onnx")
        wrong_role = NerEntity("Иванов Иван Иванович", NerLabel.PERSON.value, 0.86, zone=PageZone.ADDRESSEE.value, engine=HeuristicRussianNerEngine.name)
        self.assertEqual(select_trusted_ml_entities([ml], [wrong_role]), [])

    def test_sender_organisation_confirmation_requires_sender_zone(self):
        ml = NerEntity("ГУ МВД России", NerLabel.ORGANIZATION.value, 0.91, zone=PageZone.HEADER.value, engine="onnx")
        body = NerEntity("ГУ МВД России", NerLabel.ORGANIZATION.value, 0.88, zone=PageZone.BODY.value, engine=HeuristicRussianNerEngine.name)
        self.assertEqual(select_trusted_ml_entities([ml], [body]), [])
        incoming = NerEntity("ГУ МВД России", NerLabel.ORGANIZATION.value, 0.88, zone=PageZone.INCOMING.value, engine=HeuristicRussianNerEngine.name)
        self.assertEqual(len(select_trusted_ml_entities([ml], [incoming])), 1)

    def test_nested_department_name_does_not_create_false_close_alternative(self):
        result = CandidateResolver.result_for_selected(
            "from_department",
            [
                FieldCandidate(
                    "from_department", "ГУ МВД России по Московской области",
                    confidence=0.88, source="layout/heuristic", zone=PageZone.HEADER.value,
                ),
                FieldCandidate(
                    "from_department", "ГУ МВД России",
                    confidence=0.89, source="ner", zone=PageZone.HEADER.value,
                ),
            ],
            selected_value="ГУ МВД России по Московской области",
            selected_confidence=0.88,
            selected_source="layout/heuristic",
        )
        self.assertEqual(result.decision, "autofill")
        self.assertEqual(result.alternatives, [])
        self.assertGreaterEqual(result.margin, 0.07)

    def test_dictionary_backed_department_confidence_crosses_review_boundary(self):
        score = DocumentParser._heuristic_confidence(
            "from_department",
            "ГУ МВД России по Московской области",
            [{"text": "ГУ МВД России по Московской области", "confidence": 0.93}],
            {"header": "ГУ МВД России по Московской области"},
        )
        self.assertGreaterEqual(score, 0.86)


if __name__ == "__main__":
    unittest.main()
