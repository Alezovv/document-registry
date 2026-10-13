from __future__ import annotations

import unittest

from app.ner import (
    HeuristicRussianNerEngine,
    NerEntity,
    NerLabel,
    compare_entity_sets,
    entities_compatible,
    select_trusted_ml_entities,
)
from app.parser_core import PageZone
from app.document_parser import PARSER_VERSION


class NerGuardrailsV1203Tests(unittest.TestCase):
    def test_parser_version(self):
        self.assertEqual(PARSER_VERSION, "1.22.1")

    def test_full_name_and_initials_are_compatible_support(self):
        ml = NerEntity(
            "Иванов Иван Иванович", NerLabel.PERSON.value, 0.91,
            zone=PageZone.SIGNATURE.value, engine="onnx",
        )
        baseline = NerEntity(
            "Иванов И.И.", NerLabel.PERSON.value, 0.86,
            zone=PageZone.SIGNATURE.value, engine=HeuristicRussianNerEngine.name,
        )
        self.assertTrue(entities_compatible(ml, baseline))
        comparison = compare_entity_sets([ml], [baseline])
        self.assertEqual(comparison["agreement_ratio"], 0.0)
        self.assertEqual(comparison["compatible_overlap"], 1)
        self.assertEqual(comparison["compatible_agreement_ratio"], 1.0)

    def test_noisy_body_person_is_not_trusted(self):
        ml = NerEntity(
            "Совершенно Лишняя Фамилия", NerLabel.PERSON.value, 0.99,
            zone=PageZone.BODY.value, engine="onnx",
        )
        self.assertEqual(select_trusted_ml_entities([ml], []), [])

    def test_high_confidence_signature_person_is_trusted(self):
        ml = NerEntity(
            "Петров Петр Петрович", NerLabel.PERSON.value, 0.96,
            zone=PageZone.SIGNATURE.value, engine="onnx",
        )
        # Production mode is confirmation-only, so standalone ML evidence is
        # diagnostic but cannot influence registry fields.
        self.assertEqual(select_trusted_ml_entities([ml], []), [])
        trusted = select_trusted_ml_entities([ml], [], allow_standalone=True)
        self.assertEqual(len(trusted), 1)
        self.assertEqual(trusted[0].metadata.get("trust_reason"), "high_confidence_role_zone")

    def test_baseline_supported_ml_entity_is_trusted_below_high_threshold(self):
        ml = NerEntity(
            "Сидоров Сергей Сергеевич", NerLabel.PERSON.value, 0.80,
            zone=PageZone.SIGNATURE.value, engine="onnx",
        )
        baseline = NerEntity(
            "Сидоров С.С.", NerLabel.PERSON.value, 0.86,
            zone=PageZone.SIGNATURE.value, engine=HeuristicRussianNerEngine.name,
        )
        trusted = select_trusted_ml_entities([ml], [baseline])
        self.assertEqual(len(trusted), 1)
        self.assertEqual(trusted[0].metadata.get("trust_reason"), "baseline_support_same_role_zone")


if __name__ == "__main__":
    unittest.main()
