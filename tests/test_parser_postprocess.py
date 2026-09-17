import unittest

from app.models import Request
from app.parser_postprocess import best_known_value, correct_request_with_known_values


class ParserPostprocessTests(unittest.TestCase):
    def test_known_spelling_repairs_small_ocr_error(self):
        self.assertEqual(
            best_known_value("Евдоким0ва В.М.", ["Евдокимова В.М."]),
            "Евдокимова В.М.",
        )

    def test_different_department_number_is_never_substituted(self):
        self.assertEqual(
            best_known_value("ОП №8", ["ОП №9"]),
            "ОП №8",
        )

    def test_request_fields_use_existing_registry_spelling(self):
        request = Request(
            from_department="Отдел кадр0в",
            from_person="Евдоким0ва В.М.",
            to_person="Белк0в В.Н.",
        )
        suggestions = {
            "from_department": ["Отдел кадров"],
            "from_person": ["Евдокимова В.М."],
            "to_person": ["Белков В.Н."],
        }
        corrected = correct_request_with_known_values(request, suggestions)
        self.assertEqual(corrected.from_department, "Отдел кадров")
        self.assertEqual(corrected.from_person, "Евдокимова В.М.")
        self.assertEqual(corrected.to_person, "Белков В.Н.")


if __name__ == "__main__":
    unittest.main()
