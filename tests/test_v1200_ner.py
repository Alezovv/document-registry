from __future__ import annotations

import unittest

from app.document_parser import DocumentParser, PARSER_VERSION
from app.ner import (
    HeuristicRussianNerEngine,
    NerCandidateAdapter,
    NerLabel,
    NerRoleAdapter,
)
from app.parser_core import PageZone, RoleType


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


class NerV1200Tests(unittest.TestCase):
    def test_parser_version(self):
        self.assertEqual(PARSER_VERSION, "1.22.1")

    def test_baseline_extracts_person_org_and_location_with_layout(self):
        engine = HeuristicRussianNerEngine()
        entities = engine.extract("", [
            block("ГУ МВД России по г. Москве", 50, 60, 450, 100),
            block("Иванов И.И.", 650, 300, 850, 340),
            block("Петров П.П.", 650, 930, 850, 970),
        ])
        labels = {item.label for item in entities}
        self.assertIn(NerLabel.PERSON.value, labels)
        self.assertIn(NerLabel.ORGANIZATION.value, labels)
        self.assertIn(NerLabel.LOCATION.value, labels)
        recipient = next(item for item in entities if item.text == "Иванов И.И.")
        signer = next(item for item in entities if item.text == "Петров П.П.")
        self.assertEqual(recipient.zone, PageZone.ADDRESSEE.value)
        self.assertEqual(signer.zone, PageZone.SIGNATURE.value)

    def test_ner_entities_map_to_candidates_without_direct_form_writes(self):
        engine = HeuristicRussianNerEngine()
        entities = engine.extract("", [
            block("ГУ МВД России", 50, 60, 350, 100),
            block("Иванов И.И.", 650, 300, 850, 340),
            block("Петров П.П.", 650, 930, 850, 970),
        ])
        candidates = NerCandidateAdapter.to_field_candidates(entities)
        self.assertEqual(candidates["to_person"][0].value, "Иванов И.И.")
        self.assertEqual(candidates["from_person"][0].value, "Петров П.П.")
        self.assertEqual(candidates["from_department"][0].value, "ГУ МВД России")
        self.assertTrue(all(item.source == "ner" for items in candidates.values() for item in items))

    def test_body_person_becomes_mentioned_person_not_sender(self):
        engine = HeuristicRussianNerEngine()
        entities = engine.extract("", [
            block("Просим предоставить сведения об Иванове И.И.", 100, 560, 800, 600),
        ])
        assignments, _groups = NerRoleAdapter.resolve(entities, document_type="request")
        roles = {item.role for item in assignments}
        self.assertIn(RoleType.MENTIONED_PERSON.value, roles)
        self.assertNotIn(RoleType.SENDER.value, roles)

    def test_parser_exposes_ner_diagnostics_and_provenance(self):
        parser = DocumentParser(ner_engine=HeuristicRussianNerEngine())
        request = parser.parse({
            "text": "ГУ МВД России\nКому: Иванов И.И.\nПросим предоставить сведения.\nНачальник отдела\nПетров П.П.",
            "blocks": [
                block("ГУ МВД России", 60, 70, 330, 110),
                block("Кому: Иванов И.И.", 600, 300, 900, 340),
                block("Просим предоставить сведения.", 100, 550, 850, 590),
                block("Начальник отдела", 500, 900, 690, 940),
                block("Петров П.П.", 720, 930, 900, 970),
            ],
        })
        diagnostics = parser.last_diagnostics
        self.assertEqual(diagnostics.ner_engine, "heuristic-ru-v1")
        self.assertGreaterEqual(diagnostics.ner_entity_counts.get("PERSON", 0), 2)
        self.assertGreaterEqual(diagnostics.ner_entity_counts.get("ORGANIZATION", 0), 1)
        self.assertTrue(diagnostics.ner_entities)
        self.assertIn("ner", diagnostics.field_results["to_person"]["extractors"])
        self.assertEqual(request.to_person, "Иванов И.И.")

    def test_ner_does_not_override_existing_filename_or_label_result(self):
        parser = DocumentParser()
        request = parser.parse({
            "text": "Кому: Иванов И.И.\nПетров П.П.",
            "blocks": [
                block("Кому: Иванов И.И.", 600, 300, 900, 340),
                block("Петров П.П.", 650, 930, 850, 970),
            ],
        })
        self.assertEqual(request.to_person, "Иванов И.И.")
        self.assertNotEqual(parser.last_diagnostics.field_sources["to_person"], "ner")


if __name__ == "__main__":
    unittest.main()
