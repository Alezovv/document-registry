import unittest

from app.document_parser import DocumentParser


class DocumentParserQualityTests(unittest.TestCase):
    def setUp(self):
        self.parser = DocumentParser()

    def test_received_date_accepts_hyphen_separator(self):
        self.assertEqual(
            DocumentParser._extract_received_date("Исх. от 16-09-2026", ""),
            "16.09.2026",
        )

    def test_received_date_repairs_common_ocr_digit_confusion(self):
        self.assertEqual(
            DocumentParser._extract_received_date("Дата: 1O.O9.2O26", ""),
            "10.09.2026",
        )

    def test_received_date_rejects_invalid_calendar_date(self):
        self.assertEqual(
            DocumentParser._extract_received_date("Дата: 39.19.2026", ""),
            "",
        )

    def test_explicit_labels_win_over_unrelated_header_values(self):
        request = self.parser.parse(
            """Исх. № 42 от 01.09.2026
Дата поступления: 16.09.2026
Отдел: ОП №8
Должность: Старший УУП
От кого: Евдокимова В.М.
На кого: Белков Владимир Николаевич
Суть запроса:
Прошу предоставить сведения о регистрации гражданина
в установленный законом срок.
Начальник отдела
Иванов И.И.
"""
        )
        self.assertEqual(request.received_date, "16.09.2026")
        self.assertEqual(request.from_department, "ОП №8")
        self.assertEqual(request.from_position, "Старший УУП")
        self.assertEqual(request.from_person, "Евдокимова В.М.")
        self.assertEqual(request.to_person, "Белков Владимир Николаевич")
        self.assertEqual(
            request.request_subject,
            "Предоставить сведения о регистрации гражданина в установленный законом срок",
        )

    def test_labels_and_values_on_separate_lines(self):
        request = self.parser.parse(
            """ДАТА ПОСТУПЛЕНИЯ
16 сентября 2026
ОТДЕЛ
ОП № 8
ДОЛЖНОСТЬ
Старший УУП
ОТ КОГ0
Евдокимова В М
НА КОГ0
Белков В Н
СУТЬ ЗАПРОСА
Предоставить сведения по материалу проверки
Исполнитель Петров П.П.
"""
        )
        self.assertEqual(request.received_date, "16.09.2026")
        self.assertEqual(request.from_department, "ОП №8")
        self.assertEqual(request.from_person, "Евдокимова В.М.")
        self.assertEqual(request.to_person, "Белков В.Н.")
        self.assertEqual(request.request_subject, "Предоставить сведения по материалу проверки")

    def test_recipient_can_be_extracted_from_titled_labeled_value(self):
        request = self.parser.parse(
            """Кому: Начальнику ОП №8 Белкову В.Н.
Суть запроса: Направить материалы проверки
"""
        )
        self.assertEqual(request.to_person, "Белков В.Н.")

    def test_generic_birth_date_is_not_registration_date(self):
        request = self.parser.parse(
            """Дата рождения: 01.01.1980
Дата поступления: 16.09.2026
Суть запроса: Предоставить сведения
"""
        )
        self.assertEqual(request.received_date, "16.09.2026")

    def test_filename_does_not_override_explicit_document_values(self):
        request = self.parser.parse(
            """Дата поступления: 16.09.2026
Отдел: ОП №8
От кого: Евдокимова В.М.
На кого: Белков В.Н.
Суть запроса: Предоставить сведения по материалу проверки
""",
            file_name="Другой отдел - 01.09.2026 - Петров П.П. на Сидоров С.С..pdf",
        )
        self.assertEqual(request.received_date, "16.09.2026")
        self.assertEqual(request.from_department, "ОП №8")
        self.assertEqual(request.from_person, "Евдокимова В.М.")
        self.assertEqual(request.to_person, "Белков В.Н.")

    def test_spatial_blocks_pair_label_with_value_to_the_right(self):
        data = {
            "text": "ОТДЕЛ\nОП №8\nОТ КОГО\nЕвдокимова В.М.",
            "blocks": [
                {"text": "ОТДЕЛ", "x1": 10, "x2": 80, "y1": 10, "y2": 30, "center_y": 20, "height": 20},
                {"text": "ОП №8", "x1": 140, "x2": 220, "y1": 10, "y2": 30, "center_y": 20, "height": 20},
                {"text": "ОТ КОГО", "x1": 10, "x2": 90, "y1": 50, "y2": 70, "center_y": 60, "height": 20},
                {"text": "Евдокимова В.М.", "x1": 140, "x2": 300, "y1": 50, "y2": 70, "center_y": 60, "height": 20},
            ],
        }
        request = self.parser.parse(data)
        self.assertEqual(request.from_department, "ОП №8")
        self.assertEqual(request.from_person, "Евдокимова В.М.")

    def test_subject_does_not_swallow_signature_or_contacts(self):
        request = self.parser.parse(
            """Суть запроса:
Предоставить сведения о регистрации гражданина.
Исполнитель Петров П.П.
Тел. 8 900 000 00 00
"""
        )
        self.assertEqual(request.request_subject, "Предоставить сведения о регистрации гражданина")

    def test_prepare_text_preserves_numero_sign_semantics(self):
        self.assertEqual(DocumentParser._prepare_ocr_text("ОП №8"), "ОП №8")


if __name__ == "__main__":
    unittest.main()
