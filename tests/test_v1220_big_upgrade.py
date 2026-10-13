from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.document_parser import DocumentParser, PARSER_VERSION
from app.hybrid_parser import HybridFieldResolver
from app.parser_core import FieldCandidate, FieldNormalizer, PageZone
from app.update_service import UpdateService
from app.updater_status import UpdaterStatusReporter
from tools.prepare_gold_cache import load_document_rows


class BigUpgradeV1220Tests(unittest.TestCase):
    def test_parser_version(self):
        self.assertEqual(PARSER_VERSION, "1.22.1")

    def test_subject_hybrid_cannot_replace_deterministic_baseline(self):
        candidates = [
            FieldCandidate(
                "request_subject",
                "Предоставить сведения о лечении пациента",
                confidence=0.72,
                source="layout/heuristic",
                zone=PageZone.BODY.value,
            ),
            FieldCandidate(
                "request_subject",
                "Направить копию постановления суда",
                confidence=0.99,
                source="filename",
                zone=PageZone.UNKNOWN.value,
            ),
        ]
        choice = HybridFieldResolver.choose(
            "request_subject",
            candidates,
            baseline_value="Предоставить сведения о лечении пациента",
            baseline_confidence=0.72,
            baseline_source="layout/heuristic",
        )
        self.assertFalse(choice.changed)
        self.assertEqual(choice.value, "Предоставить сведения о лечении пациента")
        self.assertEqual(choice.reason, "field_locked_to_baseline")

    def test_subject_hybrid_does_not_resurrect_rejected_empty_value(self):
        choice = HybridFieldResolver.choose(
            "request_subject",
            [FieldCandidate(
                "request_subject", "Случайная тема из имени файла",
                confidence=0.99, source="filename", zone=PageZone.UNKNOWN.value,
            )],
            baseline_value="", baseline_confidence=0.0, baseline_source="rejected",
        )
        self.assertFalse(choice.changed)
        self.assertEqual(choice.value, "")
        self.assertEqual(choice.reason, "field_locked_empty")

    def test_date_normalizer_repairs_stamp_ocr_confusions(self):
        self.assertEqual(DocumentParser._normalize_date_value("Вх. 17 . О9 . 2026"), "17.09.2026")
        self.assertEqual(DocumentParser._normalize_date_value("0З/1О/2О26"), "03.10.2026")
        self.assertEqual(DocumentParser._normalize_spaced_date("17 О9 26"), "17.09.2026")
        self.assertEqual(FieldNormalizer.normalize("received_date", "0З,1О,2О26"), "03.10.2026")

    def test_received_date_prefers_incoming_context_over_outgoing_date(self):
        header = """
        Исх. № 51 от 15.09.2026
        Вх. № 428 от 17.09.2026
        """
        self.assertEqual(DocumentParser._extract_received_date(header, header), "17.09.2026")

    def test_updater_status_is_consumed_only_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reporter = UpdaterStatusReporter(root, "1.22.0", show_window=False)
            reporter.stage("backup", "Создаём резервную копию...", 45)
            reporter.success()
            paths = SimpleNamespace(data=root / "data")
            service = UpdateService(paths)
            first = service.consume_last_install_status()
            second = service.consume_last_install_status()
            self.assertIsNotNone(first)
            self.assertEqual(first["state"], "success")
            self.assertEqual(first["version"], "1.22.0")
            self.assertIsNone(second)
            payload = json.loads((root / "data" / "update_status.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["notified"])

    def test_gold_cache_loader_reads_managed_document_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "requests.db"
            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, source_document TEXT)")
                conn.execute("INSERT INTO requests(id, source_document) VALUES(1, '@runtime/data/documents/a.pdf')")
                conn.execute("INSERT INTO requests(id, source_document) VALUES(2, '')")
                conn.commit()
            finally:
                conn.close()
            rows = load_document_rows(db)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], 1)


if __name__ == "__main__":
    unittest.main()
