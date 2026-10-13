from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.document_parser import DocumentParser
from app.models import Request
from app.parser_correction_memory import ParserCorrectionMemory


class ParserV1151Tests(unittest.TestCase):
    def test_long_subject_keeps_meaningful_paragraph_beyond_300_chars(self):
        text = "\n".join(
            [
                "Просим предоставить сведения о прохождении лечения гражданина Иванова Ивана Ивановича,",
                "включая даты обращения, установленный диагноз, проведенные обследования и назначенное лечение,",
                "а также сведения о выданных медицинских документах и рекомендациях после выписки.",
                "Материалы необходимы для проведения проверки по зарегистрированному обращению гражданина,",
                "установления обстоятельств события и подготовки мотивированного ответа заявителю.",
                "Дополнительно просим сообщить, обращался ли указанный гражданин повторно после выписки,",
                "и направить имеющиеся сведения о повторных консультациях и назначениях специалистов.",
                "Начальник отдела",
                "Петров П.П.",
                "Исполнитель: Сидоров С.С.",
            ]
        )
        subject = DocumentParser._extract_subject(text)
        self.assertGreater(len(subject), 300)
        self.assertIn("повторных консультациях", subject.lower())
        self.assertNotIn("начальник отдела", subject.lower())
        self.assertNotIn("исполнитель", subject.lower())

    def test_subject_is_capped_safely_instead_of_becoming_whole_document(self):
        body = "Просим предоставить " + ("подробные сведения по материалам проверки " * 80)
        subject = DocumentParser._extract_subject(body)
        self.assertLessEqual(len(subject), 1200)
        self.assertTrue(subject.lower().startswith("предоставить"))

    def test_exact_user_correction_is_reused_and_persisted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "data" / "parser_corrections.json"
            memory = ParserCorrectionMemory(path)
            before = Request(from_department="ОМВД Росии по району")
            after = Request(from_department="ОМВД России по району")
            self.assertEqual(memory.learn(before, after), 1)
            self.assertTrue(path.is_file())

            reloaded = ParserCorrectionMemory(path)
            parsed = Request(from_department="ОМВД Росии по району")
            corrected = reloaded.apply(parsed)
            self.assertEqual(corrected.from_department, "ОМВД России по району")

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], ParserCorrectionMemory.VERSION)


    def test_two_confirmations_enable_safe_fuzzy_reuse(self):
        with tempfile.TemporaryDirectory() as temp:
            memory = ParserCorrectionMemory(Path(temp) / "parser_corrections.json")
            target = "ОМВД России по району"
            memory.learn(
                Request(from_department="ОМВД Росии по району"),
                Request(from_department=target),
            )
            memory.learn(
                Request(from_department="ОМВД Россни по району"),
                Request(from_department=target),
            )
            self.assertEqual(
                memory.correct_value("from_department", "ОМВД Росси по району"),
                target,
            )

    def test_memory_does_not_learn_request_subject(self):
        with tempfile.TemporaryDirectory() as temp:
            memory = ParserCorrectionMemory(Path(temp) / "parser_corrections.json")
            before = Request(request_subject="Предоставить сведения")
            after = Request(request_subject="Предоставить все медицинские сведения")
            self.assertEqual(memory.learn(before, after), 0)
            self.assertFalse(memory.path.exists())

    def test_number_change_is_not_learned(self):
        with tempfile.TemporaryDirectory() as temp:
            memory = ParserCorrectionMemory(Path(temp) / "parser_corrections.json")
            before = Request(from_department="ОП №8")
            after = Request(from_department="ОП №9")
            self.assertEqual(memory.learn(before, after), 0)
            self.assertEqual(memory.correct_value("from_department", "ОП №8"), "ОП №8")

    def test_memory_values_are_merged_into_form_suggestions(self):
        with tempfile.TemporaryDirectory() as temp:
            memory = ParserCorrectionMemory(Path(temp) / "parser_corrections.json")
            memory.learn(
                Request(from_position="Началник отдела"),
                Request(from_position="Начальник отдела"),
            )
            merged = memory.merge_suggestions(
                {"from_position": ["Главный специалист", "Начальник отдела"]}
            )
            self.assertEqual(merged["from_position"][0], "Начальник отдела")
            self.assertEqual(merged["from_position"].count("Начальник отдела"), 1)


if __name__ == "__main__":
    unittest.main()
