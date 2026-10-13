import unittest

from app.document_parser import DocumentParser, PARSER_VERSION


class V1221DateRegressionTests(unittest.TestCase):
    def test_parser_version(self):
        self.assertEqual(PARSER_VERSION, "1.22.1")

    def test_neighbouring_incoming_marker_does_not_steal_previous_line_date(self):
        header = """
        Исх. № 51 от 15.09.2026
        Входящий штамп
        № 428
        """
        self.assertEqual(DocumentParser._extract_received_date(header, header), "15.09.2026")

    def test_same_line_incoming_marker_still_wins(self):
        header = """
        Исх. № 51 от 15.09.2026
        Вх. № 428 от 17 . О9 . 2026
        """
        self.assertEqual(DocumentParser._extract_received_date(header, header), "17.09.2026")

    def test_ocr_repair_is_kept_without_cross_line_ranking(self):
        header = "Вх. № 4 от 0З/1О/2О26"
        self.assertEqual(DocumentParser._extract_received_date(header, header), "03.10.2026")


if __name__ == "__main__":
    unittest.main()
