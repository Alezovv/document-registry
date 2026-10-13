from __future__ import annotations

import json
import os
from contextlib import closing
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import __version__
from app.backup_service import BackupService
from app.database import Database
from app.db_migrations import CURRENT_SCHEMA_VERSION, validate_database
from app.document_service import DocumentService
from app.gold_dataset import GoldDocument, evaluate_request, load_gold_dataset
from app.models import Request
from app.parser_review import compact_parser_diagnostics
from app.request_service import RequestService


class V1270DatabaseAndDuplicateTests(unittest.TestCase):
    def _services(self, root: Path):
        data = root / "data"
        docs_dir = data / "documents"
        db = Database(data_dir=data, documents_dir=docs_dir)
        docs = DocumentService(db, docs_dir)
        return db, docs, RequestService(db, docs)

    def test_release_and_schema_version(self):
        self.assertEqual(__version__, "1.28.5")
        self.assertEqual(CURRENT_SCHEMA_VERSION, 7)

    def test_schema_7_has_fingerprint_and_parser_metadata(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            db, _docs, _service = self._services(root)
            report = validate_database(db.db_path, expected_schema=7)
            self.assertTrue(report.ok)
            import sqlite3
            with closing(sqlite3.connect(db.db_path)) as conn:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("source_sha256", cols)
            self.assertIn("source_size_bytes", cols)
            self.assertIn("request_parser_metadata", tables)

    def test_schema_6_migrates_to_7_without_losing_existing_rows(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            data = root / "data"
            data.mkdir()
            db_path = data / "requests.db"
            from app.db_migrations import migrate_database
            migrate_database(db_path, target_schema=6)
            import sqlite3
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "INSERT INTO requests(received_date, request_subject, status) VALUES (?, ?, ?)",
                    ("18.09.2026", "До миграции", "Новый"),
                )
                conn.commit()
            db = Database(data_dir=data)
            self.assertEqual(db.get_request(1).request_subject, "До миграции")
            self.assertTrue(validate_database(db.db_path, expected_schema=7).ok)

    def test_duplicate_is_detected_by_content_before_ocr(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            db, _docs, service = self._services(root)
            incoming = root / "incoming"
            incoming.mkdir()
            first = incoming / "letter.pdf"
            second = incoming / "renamed-copy.pdf"
            first.write_bytes(b"same-pdf-content" * 1024)
            second.write_bytes(first.read_bytes())
            request = Request(received_date="18.09.2026", source_document=str(first))
            request_id = service.create_request(request)

            duplicate = service.find_duplicate_document(second)
            self.assertIsNotNone(duplicate)
            self.assertEqual(duplicate.id, request_id)
            self.assertEqual(len(duplicate.source_sha256), 64)
            self.assertGreater(duplicate.source_size_bytes, 0)

    def test_compact_parser_metadata_keeps_review_data_not_full_ocr(self):
        full = {
            "field_results": {
                "from_person": {
                    "value": "Иванов И.И.",
                    "confidence": 0.61,
                    "decision": "review",
                    "extractors": ["signature"],
                    "alternatives": [
                        {"value": "Иванов Иван", "confidence": 0.58, "source": "ner", "source_blocks": []}
                    ],
                    "source_blocks": [{"page": 2, "bbox": [1, 2, 3, 4], "text": "Иванов И.И.", "confidence": 0.9}],
                }
            },
            "field_regions": {"from_person": [{"page": 2, "bbox": [1, 2, 3, 4], "text": "Иванов И.И."}]},
            "ner_entities": [{"text": "X" * 100000}],
            "ocr_text": "Y" * 200000,
        }
        compact = compact_parser_diagnostics(full)
        encoded = json.dumps(compact, ensure_ascii=False)
        self.assertIn("Иванов", encoded)
        self.assertIn("field_regions", compact)
        self.assertNotIn("ner_entities", compact)
        self.assertNotIn("ocr_text", compact)
        self.assertLess(len(encoded), 10000)


class V1270BackupTests(unittest.TestCase):
    def test_created_backup_is_deep_verified_and_health_is_recorded(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            data = root / "data"
            docs_dir = data / "documents"
            db = Database(data_dir=data, documents_dir=docs_dir)
            docs = DocumentService(db, docs_dir)
            service = RequestService(db, docs)
            incoming = root / "incoming.pdf"
            incoming.write_bytes(b"backup-test" * 4096)
            service.create_request(Request(received_date="18.09.2026", source_document=str(incoming)))

            backup = BackupService(db, docs)
            result = backup.create_backup(root / "backup.zip")
            self.assertTrue(result.verified)
            self.assertTrue(result.verified_at)
            self.assertTrue(result.path.is_file())
            verification = backup.verify_backup(result.path)
            self.assertTrue(verification["ok"])
            health = backup.verification_info()
            self.assertTrue(health["ok"])
            self.assertEqual(health["name"], "backup.zip")


class V1270GoldDatasetTests(unittest.TestCase):
    def test_gold_loader_uses_only_reviewed_documents_and_evaluates_fields(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "gold.json"
            path.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "documents": [
                            {
                                "sha256": "a" * 64,
                                "file_name": "a.pdf",
                                "reviewed": True,
                                "expected": {
                                    "received_date": "18.09.2026",
                                    "from_person": "Иванов И.И.",
                                    "from_department": "",
                                    "from_position": "",
                                    "to_person": "",
                                    "request_subject": "",
                                },
                            },
                            {
                                "sha256": "b" * 64,
                                "file_name": "b.pdf",
                                "reviewed": False,
                                "expected": {"received_date": "17.09.2026"},
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            documents = load_gold_dataset(path)
            self.assertEqual(len(documents), 1)
            result = evaluate_request(
                Request(received_date="18.09.2026", from_person="Иванов Иван Иванович"),
                documents[0],
            )
            self.assertTrue(result["received_date"]["exact"])
            self.assertTrue(result["from_person"]["compatible"])

    def test_gold_document_type_is_small_and_privacy_safe(self):
        document = GoldDocument("c" * 64, "test.pdf", {"received_date": "18.09.2026"}, True)
        self.assertFalse(hasattr(document, "ocr_text"))
        self.assertFalse(hasattr(document, "pdf_bytes"))


class V1270UiAndMemorySourceTests(unittest.TestCase):
    def test_review_provenance_queue_and_search_features_are_wired(self):
        root = Path(__file__).resolve().parents[1]
        request_dialog = (root / "app" / "ui" / "dialogs" / "request_dialog.py").read_text(encoding="utf-8")
        import_dialog = (root / "app" / "import_dialog.py").read_text(encoding="utf-8")
        import_controller = (root / "app" / "ui" / "import_controller.py").read_text(encoding="utf-8")
        registry = (root / "app" / "ui" / "registry_page.py").read_text(encoding="utf-8")
        diagnostics = (root / "app" / "ui" / "diagnostics_dialog.py").read_text(encoding="utf-8")
        self.assertIn("Показать источник в PDF", request_dialog)
        self.assertIn("field_review_info", request_dialog)
        self.assertIn("Отменить ожидающие", import_dialog)
        self.assertIn("Повторить ошибки", import_dialog)
        self.assertIn("_preflight_document", import_controller)
        self.assertIn("find_duplicate_document", import_controller)
        self.assertIn("search_request_hits_full_text", registry)
        self.assertIn("self.sender_filter", registry)
        self.assertIn("self.recipient_filter", registry)
        self.assertIn("Перестроить поисковый индекс", diagnostics)
        self.assertIn("Проверить дубликаты", diagnostics)

    def test_low_memory_guards_and_gold_release_gate_are_present(self):
        root = Path(__file__).resolve().parents[1]
        database = (root / "app" / "database.py").read_text(encoding="utf-8")
        workers = (root / "app" / "import_workers.py").read_text(encoding="utf-8")
        dialog = (root / "app" / "import_dialog.py").read_text(encoding="utf-8")
        build = (root / "packaging" / "build.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('PRAGMA temp_store = FILE', database)
        self.assertIn('PRAGMA cache_size = -8192', database)
        self.assertIn('self.ocr_data = None', workers)
        self.assertIn('finalize_ocr_batch_memory()', dialog)
        self.assertIn('OCR.reset_shared_engine()', workers)
        self.assertIn('gold_regression_gate.py', build)
        self.assertIn('Gold Dataset regression gate', build)


if __name__ == "__main__":
    unittest.main()
