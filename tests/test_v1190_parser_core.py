from __future__ import annotations

import unittest

from app.document_parser import DocumentParser, PARSER_VERSION
from app.parser_core import (
    CandidateResolver,
    DocumentType,
    DocumentTypeClassifier,
    FieldCandidate,
    FieldNormalizer,
    LayoutAnalyzer,
    PageZone,
)


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


class ParserCoreV1190Tests(unittest.TestCase):
    def test_parser_version_changed(self):
        self.assertEqual(PARSER_VERSION, "1.22.1")

    def test_field_specific_date_normalization(self):
        self.assertEqual(FieldNormalizer.normalize("received_date", " 7.O9.2026 "), "07.09.2026")
        self.assertEqual(FieldNormalizer.normalize("received_date", "31.02.2026"), "31.02.2026")

    def test_layout_analyzer_assigns_broad_zones(self):
        self.assertEqual(LayoutAnalyzer.classify_block(block("ГУ МВД", 50, 40, 300, 80)), PageZone.HEADER.value)
        self.assertEqual(LayoutAnalyzer.classify_block(block("Кому", 650, 320, 900, 370)), PageZone.ADDRESSEE.value)
        self.assertEqual(LayoutAnalyzer.classify_block(block("Иванов И.И.", 600, 950, 900, 990)), PageZone.SIGNATURE.value)
        self.assertEqual(LayoutAnalyzer.classify_block(block("Исп. Петров", 50, 1240, 300, 1270)), PageZone.EXECUTOR_FOOTER.value)

    def test_document_type_classifier_distinguishes_request_and_response(self):
        request = DocumentTypeClassifier.classify("Просим предоставить сведения о лечении пациента")
        response = DocumentTypeClassifier.classify("В ответ на Ваш запрос сообщаем сведения о пациенте")
        self.assertEqual(request.value, DocumentType.REQUEST.value)
        self.assertEqual(response.value, DocumentType.RESPONSE.value)
        self.assertGreater(request.confidence, 0.5)
        self.assertGreater(response.confidence, 0.5)

    def test_candidate_resolver_deduplicates_and_keeps_provenance(self):
        result = CandidateResolver.resolve(
            "from_person",
            [
                FieldCandidate("from_person", "Иванов И.И.", confidence=0.76, source="layout/heuristic"),
                FieldCandidate("from_person", "Иванов И.И.", confidence=0.88, source="filename"),
                FieldCandidate("from_person", "Петров П.П.", confidence=0.70, source="ner"),
            ],
        )
        self.assertEqual(result.value, "Иванов И.И.")
        self.assertIn("filename", result.extractors)
        self.assertIn("layout/heuristic", result.extractors)
        self.assertEqual(result.alternatives[0].value, "Петров П.П.")

    def test_document_parser_exposes_structured_field_results_without_changing_request(self):
        parser = DocumentParser()
        request = parser.parse(
            {
                "text": "ГУ МВД России\nПросим предоставить сведения о лечении пациента Иванова И.И.\nНачальник отдела\nПетров П.П.",
                "blocks": [
                    block("ГУ МВД России", 60, 80, 330, 120),
                    block("Просим предоставить сведения о лечении пациента Иванова И.И.", 100, 520, 850, 570),
                    block("Начальник отдела", 500, 930, 690, 970),
                    block("Петров П.П.", 720, 930, 900, 970),
                ],
            }
        )
        diagnostics = parser.last_diagnostics
        self.assertEqual(diagnostics.document_type, DocumentType.REQUEST.value)
        self.assertIn("request_subject", diagnostics.field_results)
        self.assertEqual(
            diagnostics.field_results["request_subject"]["value"],
            request.request_subject,
        )
        self.assertTrue(diagnostics.layout_zone_counts)


if __name__ == "__main__":
    unittest.main()

class V1190UpdatePolishTests(unittest.TestCase):
    def test_update_download_window_can_be_minimized_without_cancel(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        text = (root / "app" / "ui" / "update_controller.py").read_text(encoding="utf-8")
        self.assertIn("Qt.WindowType.WindowMinimizeButtonHint", text)
        self.assertIn("Qt.WindowModality.NonModal", text)

    def test_build_manifest_has_drive_file_id_and_no_manual_json_rewrite_is_required(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        text = (root / "packaging" / "build.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("package_file_id = $GoogleDriveFileId", text)
        self.assertIn("[switch]$ManifestOnly", text)
        self.assertIn("-PackageFileId", text)
        self.assertIn("UTF8Encoding($false)", text)
