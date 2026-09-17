import unittest

from app.document_parser import DocumentParser


def block(text, x1, y1, x2, y2, *, w=1000, h=1400, confidence=0.93):
    return {
        "text": text,
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "center_y": (y1 + y2) / 2,
        "height": max(1, y2 - y1),
        "confidence": confidence,
        "page_index": 0,
        "page_width": w,
        "page_height": h,
        "source": "ocr",
    }


class DocumentParserRegressionTests(unittest.TestCase):
    def test_text_pdf_fallback(self):
        parser = DocumentParser()
        request = parser.parse(
            "ГУ МВД России по области\n"
            "Вх. № 123 от 17.09.2026\n"
            "Прошу предоставить медицинскую документацию в отношении Иванова Ивана Ивановича\n"
            "Начальник отдела\nПетров П.П."
        )
        self.assertEqual(request.received_date, "17.09.2026")
        self.assertIn("ГУ МВД", request.from_department)
        self.assertEqual(request.from_person, "Петров П.П.")
        self.assertTrue(request.request_subject.startswith("Предоставить"))
        self.assertGreater(parser.last_diagnostics.field_confidence["received_date"], 0.7)

    def test_incoming_stamp_uses_top_left_layout(self):
        parser = DocumentParser()
        blocks = [
            block("Вх. № 778", 35, 80, 190, 115),
            block("16.09.2026", 205, 82, 350, 115),
            block("Исх. № 55 от 12.09.2026", 610, 150, 940, 190),
            block("ГУ МВД России", 80, 220, 400, 260),
            block("Прошу предоставить сведения", 120, 600, 760, 640),
            block("Начальник Иванов И.И.", 520, 1180, 900, 1220),
        ]
        text = "\n".join(item["text"] for item in blocks)
        request = parser.parse({"text": text, "blocks": blocks})
        self.assertEqual(request.received_date, "16.09.2026")
        self.assertGreaterEqual(parser.last_diagnostics.field_confidence["received_date"], 0.8)

    def test_spatial_label_and_value(self):
        parser = DocumentParser()
        blocks = [
            block("Организация", 70, 120, 220, 155),
            block("ГБУЗ ГКБ № 1", 300, 120, 560, 155),
            block("От кого", 70, 1040, 200, 1075),
            block("Петров П.П.", 300, 1040, 520, 1075),
            block("Суть запроса", 70, 500, 245, 535),
            block("Предоставить копию медицинской карты", 300, 500, 850, 540),
        ]
        request = parser.parse({"text": "\n".join(b["text"] for b in blocks), "blocks": blocks})
        self.assertEqual(request.from_department, "ГБУЗ ГКБ №1")
        self.assertEqual(request.from_person, "Петров П.П.")
        self.assertIn("медицинской карты", request.request_subject)
        self.assertEqual(parser.last_diagnostics.field_sources["from_department"], "label/layout")

    def test_document_number_is_internal_diagnostic(self):
        parser = DocumentParser()
        request = parser.parse(
            "ГБУЗ ГКБ №1\nИсх. № 45-12/А от 15.09.2026\n"
            "Прошу направить сведения на гражданина Сидорова Сергея Петровича\n"
            "Главный врач\nИванов И.И."
        )
        self.assertEqual(parser.last_diagnostics.document_number, "45-12/А")
        self.assertFalse(hasattr(request, "document_number"))

    def test_gibdd_ocr_typo_is_kept_as_original_organisation_line(self):
        parser = DocumentParser()
        request = parser.parse(
            "ОГИБДЦ ОМВД России по району\n"
            "от 14.09.2026 № 17\n"
            "Прошу сообщить сведения по факту ДТП\n"
            "Инспектор ИАЗ\nСмирнов С.С."
        )
        self.assertTrue(request.from_department)
        self.assertIn("ОМВД", request.from_department)
        self.assertEqual(request.from_person, "Смирнов С.С.")

    def test_poor_scan_fuzzy_organisation(self):
        parser = DocumentParser()
        request = parser.parse(
            "ГИБДЦ по городскому округу\n"
            "от 14.09.2026 № 17\n"
            "Прошу сообщить сведения по факту ДТП\n"
            "Инспектор ИАЗ\nСмирнов С.С."
        )
        self.assertTrue(request.from_department.startswith("ГИБДЦ"))
        self.assertEqual(request.from_position.lower(), "инспектор иаз")

    def test_court_signature_owner(self):
        parser = DocumentParser()
        request = parser.parse(
            "Судебный участок № 4\nот 13.09.2026\n"
            "Прошу направить медицинскую документацию в отношении Орлова Олега Олеговича\n"
            "Мировой судья\nВладелец: Городилова Татьяна Олеговна"
        )
        self.assertIn("Судебный участок", request.from_department)
        self.assertEqual(request.from_position.lower(), "мировой судья")
        self.assertEqual(request.from_person, "Городилова Татьяна Олеговна")

    def test_hospital_without_person_does_not_invent_signer(self):
        parser = DocumentParser()
        request = parser.parse(
            "ГБУЗ Центральная районная больница\n"
            "от 11.09.2026 № 81\n"
            "Просим предоставить сведения о лечении пациента"
        )
        self.assertIn("ГБУЗ", request.from_department)
        self.assertEqual(request.from_person, "")

    def test_filename_remains_fallback(self):
        parser = DocumentParser()
        request = parser.parse(
            "Вх. № 99 от 17.09.2026\nПрошу сообщить сведения",
            "ГУ МВД от 15.09.2026 Петров П.П. - Иванов И.И. - об обстоятельствах дела.pdf",
        )
        self.assertEqual(request.received_date, "17.09.2026")
        self.assertEqual(request.from_person, "Петров П.П.")
        self.assertEqual(request.to_person, "Иванов И.И.")


if __name__ == "__main__":
    unittest.main()
