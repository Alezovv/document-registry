from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.document_parser import DocumentParser, PARSER_VERSION
from app.models import Request
from app.parser_core import PageZone, RoleResolver, RoleType
from app.parser_correction_memory import ParserCorrectionMemory


def block(text, x1, y1, x2, y2, *, w=1000, h=1400, confidence=0.95, page=0):
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


class ParserCoreV1191Tests(unittest.TestCase):
    def test_parser_version(self):
        self.assertEqual(PARSER_VERSION, "1.22.1")

    def test_role_resolver_binds_sender_recipient_and_executor(self):
        selected = {
            "from_department": "ГУ МВД России",
            "from_position": "Начальник отдела",
            "from_person": "Петров П.П.",
            "to_person": "Иванов И.И.",
        }
        regions = {
            "from_person": [{"parser_zone": PageZone.SIGNATURE.value}],
            "to_person": [{"parser_zone": PageZone.ADDRESSEE.value}],
        }
        assignments, groups = RoleResolver.resolve(
            selected,
            field_regions=regions,
            blocks=[block("Исполнитель: Сидоров С.С.", 50, 1240, 350, 1280)],
            document_type="request",
        )
        by_role = {item.role: item for item in assignments}
        self.assertEqual(by_role[RoleType.SENDER.value].value, "Петров П.П.")
        self.assertEqual(by_role[RoleType.RECIPIENT.value].value, "Иванов И.И.")
        self.assertEqual(by_role[RoleType.EXECUTOR.value].value, "Сидоров С.С.")
        self.assertTrue(any(group.role == RoleType.SENDER.value for group in groups))

    def test_document_parser_exposes_role_diagnostics(self):
        parser = DocumentParser()
        parser.parse({
            "text": "ГУ МВД России\nКому: Иванову И.И.\nПросим предоставить сведения.\nНачальник отдела Петров П.П.\nИсполнитель: Сидоров С.С.",
            "blocks": [
                block("ГУ МВД России", 60, 70, 330, 110),
                block("Кому: Иванову И.И.", 600, 300, 900, 340),
                block("Просим предоставить сведения.", 100, 550, 850, 590),
                block("Начальник отдела Петров П.П.", 520, 930, 900, 980),
                block("Исполнитель: Сидоров С.С.", 50, 1240, 350, 1280),
            ],
        })
        self.assertIsInstance(parser.last_diagnostics.role_assignments, list)
        self.assertIsInstance(parser.last_diagnostics.entity_groups, list)
        self.assertTrue(any(item["role"] == RoleType.EXECUTOR.value for item in parser.last_diagnostics.role_assignments))

    def test_v1_correction_memory_migrates_without_losing_rule(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "parser_corrections.json"
            path.write_text(json.dumps({
                "version": 1,
                "fields": {
                    "from_department": {"rules": []},
                    "from_position": {"rules": []},
                    "from_person": {"rules": [{
                        "source": "Петр0в П.П.",
                        "source_key": "петров п п",
                        "target": "Петров П.П.",
                        "count": 2,
                    }]},
                    "to_person": {"rules": []},
                },
            }, ensure_ascii=False), encoding="utf-8")
            memory = ParserCorrectionMemory(path)
            self.assertEqual(memory.correct_value("from_person", "Петр0в П.П."), "Петров П.П.")
            before = Request(from_person="Иванв И.И.")
            after = Request(from_person="Иванов И.И.")
            self.assertEqual(memory.learn(before, after, context={
                "document_type": "request",
                "field_sources": {"from_person": "layout/heuristic"},
                "field_regions": {"from_person": [{"parser_zone": "signature"}]},
            }), 1)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["version"], 2)
            rules = stored["fields"]["from_person"]["rules"]
            learned = next(rule for rule in rules if rule["target"] == "Иванов И.И.")
            self.assertTrue(learned["contexts"])
            self.assertEqual(learned["contexts"][0]["document_type"], "request")
            self.assertEqual(learned["contexts"][0]["zone"], "signature")

    def test_context_never_stores_raw_ocr_text(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "parser_corrections.json"
            memory = ParserCorrectionMemory(path)
            memory.learn(
                Request(from_person="Сидрв С.С."),
                Request(from_person="Сидоров С.С."),
                context={
                    "document_type": "letter",
                    "field_sources": {"from_person": "layout/heuristic"},
                    "field_regions": {"from_person": [{"parser_zone": "signature", "text": "SECRET OCR"}]},
                    "raw_ocr_text": "SECRET OCR",
                },
            )
            payload = path.read_text(encoding="utf-8")
            self.assertNotIn("SECRET OCR", payload)


if __name__ == "__main__":
    unittest.main()
