from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest

from app.document_parser import DocumentParser, PARSER_VERSION
from app.models import Request
from app.parser_cache import ParserCache
from app.pdf_highlight_service import PdfHighlightPreview


def block(text, x1, y1, x2, y2, *, w=1000, h=1400, page=0):
    return {
        "text": text,
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "center_y": (y1 + y2) / 2,
        "height": max(1, y2 - y1),
        "confidence": 0.95,
        "page_index": page,
        "page_width": w,
        "page_height": h,
        "source": "ocr",
    }


class V116FeatureTests(unittest.TestCase):
    def test_parser_cache_survives_pdf_move_and_invalidates_parser_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "PC-A" / "Desktop" / "DocumentRegistry" / "data" / "documents"
            first.mkdir(parents=True)
            pdf = first / "letter.pdf"
            pdf.write_bytes(b"%PDF-1.4\nportable-content\n%%EOF\n")
            cache = ParserCache(root / "cache")
            ocr = {"text": "ГУ МВД России\nПросим предоставить сведения", "blocks": []}
            key = cache.save_ocr(pdf, ocr)
            request = Request(from_department="ГУ МВД России", request_subject="Предоставить сведения")
            cache.save_parse(
                pdf,
                request,
                {"field_regions": {}},
                parser_version=PARSER_VERSION,
                cache_key=key,
            )

            moved = root / "PC-B" / "Desktop" / "DocumentRegistry" / "data" / "documents"
            moved.mkdir(parents=True)
            moved_pdf = moved / pdf.name
            pdf.replace(moved_pdf)

            _key, cached_ocr = cache.load_ocr(moved_pdf)
            self.assertEqual(cached_ocr["text"], ocr["text"])
            cached = cache.load_parse(moved_pdf, parser_version=PARSER_VERSION)
            self.assertIsNotNone(cached)
            self.assertEqual(cached[0].from_department, "ГУ МВД России")
            self.assertIsNone(cache.load_parse(moved_pdf, parser_version="future-parser"))

    def test_parser_diagnostics_expose_normalized_source_regions(self):
        parser = DocumentParser()
        blocks = [
            block("Вх. № 15 от 17.09.2026", 40, 80, 360, 120),
            block("ГУ МВД России", 60, 180, 350, 220),
            block("Просим предоставить медицинскую документацию", 100, 600, 850, 640),
            block("Петров П.П.", 700, 1180, 900, 1220),
        ]
        request = parser.parse({"text": "\n".join(item["text"] for item in blocks), "blocks": blocks})
        self.assertEqual(request.received_date, "17.09.2026")
        regions = parser.last_diagnostics.field_regions
        self.assertIn("received_date", regions)
        self.assertIn("request_subject", regions)
        for items in regions.values():
            for item in items:
                self.assertEqual(len(item["rect_norm"]), 4)
                self.assertTrue(all(0.0 <= value <= 1.0 for value in item["rect_norm"]))

    def test_highlight_preview_never_modifies_original_pdf(self):
        try:
            import pymupdf
        except Exception:
            self.skipTest("PyMuPDF unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.pdf"
            document = pymupdf.open()
            page = document.new_page(width=600, height=800)
            page.insert_text((72, 100), "Document text")
            document.save(source)
            document.close()
            original = source.read_bytes()

            preview = PdfHighlightPreview()
            try:
                result = preview.build(
                    source,
                    {"request_subject": [{"page_index": 0, "rect_norm": [0.1, 0.08, 0.7, 0.16]}]},
                )
                self.assertIsNotNone(result)
                self.assertTrue(result.is_file())
                self.assertNotEqual(result, source)
                self.assertEqual(source.read_bytes(), original)
            finally:
                preview.close()

    def test_import_dialog_uses_native_minimize_and_close_cancels_queue(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "app" / "import_dialog.py").read_text(encoding="utf-8")
        self.assertIn("WindowMinimizeButtonHint", text)
        self.assertIn("def changeEvent", text)
        self.assertIn("self.send_to_background(hide=True)", text)
        self.assertIn("def _request_stop_and_close", text)
        self.assertIn("self.pdf_queue.clear()", text)
        self.assertIn("requestInterruption()", text)
        self.assertNotIn('QPushButton("Свернуть")', text)
        self.assertNotIn('QPushButton("Отмена")', text)

    def test_import_pipeline_uses_content_cache_before_ocr(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "app" / "import_workers.py").read_text(encoding="utf-8")
        self.assertIn("cache.load_ocr", text)
        self.assertIn("cache.load_parse", text)
        self.assertIn("PARSER_VERSION", text)
        self.assertIn("OCR взят из локального кэша", text)


if __name__ == "__main__":
    unittest.main()
