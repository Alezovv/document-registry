import unittest

from app.document_parser import DocumentParser


def block(text, x1, y1, x2, y2, *, w=1000, h=1400, confidence=0.94, page=0):
    return {
        "text": text,
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "center_y": (y1 + y2) / 2,
        "height": max(1, y2 - y1),
        "confidence": confidence,
        "page_index": page,
        "page_width": w,
        "page_height": h,
        "source": "ocr",
    }


class ParserV115Tests(unittest.TestCase):
    def test_subject_is_filled_for_informational_letter_without_request_word(self):
        parser = DocumentParser()
        request = parser.parse(
            "Администрация Центрального района\n"
            "Сообщаем, что работы по отлову безнадзорных животных выполнены в полном объеме.\n"
            "Приложение: акт на 2 л.\n"
            "Директор\nСидоров С.С."
        )
        self.assertIn("отлову безнадзорных животных", request.request_subject.lower())
        self.assertNotIn("приложение", request.request_subject.lower())

    def test_subject_has_safe_generic_fallback_for_report_style_letter(self):
        parser = DocumentParser()
        request = parser.parse(
            "Прокуратура района\n"
            "В ходе проверки установлено нарушение сроков предоставления медицинской документации.\n"
            "Материалы проверки прилагаются.\n"
            "Прокурор\nИванов И.И."
        )
        self.assertTrue(request.request_subject)
        self.assertIn("нарушение сроков", request.request_subject.lower())

    def test_layout_fragments_are_rejoined_for_subject_context(self):
        parser = DocumentParser()
        blocks = [
            block("ГУ МВД России", 70, 120, 300, 155),
            block("Просим", 110, 480, 210, 515),
            block("предоставить сведения", 225, 480, 520, 515),
            block("о лечении пациента", 110, 530, 390, 565),
            block("Иванова Ивана Ивановича", 405, 530, 760, 565),
            block("Начальник отдела", 510, 1160, 720, 1195),
            block("Петров П.П.", 740, 1160, 900, 1195),
        ]
        text = "\n".join(item["text"] for item in blocks)
        request = parser.parse({"text": text, "blocks": blocks})
        self.assertTrue(request.request_subject)
        self.assertIn("предоставить сведения", request.request_subject.lower())
        self.assertIn("лечении пациента", request.request_subject.lower())

    def test_multiline_letterhead_is_kept_as_department(self):
        parser = DocumentParser()
        request = parser.parse(
            "ГУ МВД России\n"
            "ПО МОСКОВСКОЙ ОБЛАСТИ\n"
            "Исх. № 15 от 16.09.2026\n"
            "Прошу сообщить сведения по материалу проверки\n"
            "Начальник отдела\nПетров П.П."
        )
        self.assertIn("ГУ МВД России", request.from_department)
        self.assertIn("МОСКОВСКОЙ ОБЛАСТИ", request.from_department)

    def test_executor_below_signature_does_not_replace_signer(self):
        parser = DocumentParser()
        request = parser.parse(
            "ОМВД России по району\n"
            "Прошу направить сведения по факту ДТП\n"
            "Начальник отдела\n"
            "Иванов И.И.\n"
            "Исполнитель\n"
            "Петров П.П.\n"
            "тел. 8 000 000-00-00"
        )
        self.assertEqual(request.from_person, "Иванов И.И.")


    def test_multiline_spatial_subject_label_keeps_wrapped_value(self):
        parser = DocumentParser()
        blocks = [
            block("Суть запроса", 70, 480, 240, 515),
            block("Предоставить сведения о лечении", 300, 480, 760, 515),
            block("пациента за период госпитализации", 300, 530, 790, 565),
            block("Начальник", 520, 1120, 690, 1155),
            block("Иванов И.И.", 720, 1120, 900, 1155),
        ]
        request = parser.parse({"text": "\n".join(b["text"] for b in blocks), "blocks": blocks})
        self.assertIn("сведения о лечении", request.request_subject.lower())
        self.assertIn("период госпитализации", request.request_subject.lower())
        self.assertNotIn("начальник", request.request_subject.lower())

    def test_header_only_document_does_not_invent_subject(self):
        parser = DocumentParser()
        request = parser.parse(
            "ГУ МВД России по области\n"
            "Исх. № 15 от 16.09.2026\n"
            "Начальник отдела\nИванов И.И."
        )
        self.assertEqual(request.request_subject, "")

    def test_subject_candidate_scores_are_available_for_diagnostics(self):
        parser = DocumentParser()
        parser.parse(
            "ГБУЗ ГКБ №1\n"
            "Просим предоставить копию медицинской карты пациента.\n"
            "Главный врач\nИванов И.И."
        )
        scores = parser.last_diagnostics.field_candidate_scores.get("request_subject", [])
        self.assertTrue(scores)
        self.assertGreater(scores[0], 80)


if __name__ == "__main__":
    unittest.main()
