import tempfile
import time
import unittest
from pathlib import Path

from app.database import Database
from app.document_service import DocumentService, InvalidDocumentError
from app.models import Request
from app.request_service import RequestService


class DocumentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = Database(data_dir=self.root / "data")
        self.documents = DocumentService(self.db, self.db.documents_dir)
        self.requests = RequestService(self.db, self.documents)
        self.incoming_dir = self.root / "incoming"
        self.incoming_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_request(self, source_document=""):
        return Request(
            received_date="16.09.2026",
            from_department="Отдел кадров",
            from_position="Начальник",
            from_person="Иванов Иван",
            to_person="Петров Петр",
            request_subject="Тестовый запрос",
            note="",
            status="Новый",
            source_document=source_document,
        )

    def write_pdf(self, path: Path, payload: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4\n" + payload)
        return path

    def test_hash_cache_invalidates_when_file_changes(self):
        path = self.write_pdf(self.incoming_dir / "one.pdf", b"first")
        first_hash = self.documents.calculate_file_hash(path)
        time.sleep(0.002)
        path.write_bytes(b"%PDF-1.4\nsecond-content")
        second_hash = self.documents.calculate_file_hash(path)
        self.assertNotEqual(first_hash, second_hash)

    def test_store_document_rejects_non_pdf(self):
        path = self.incoming_dir / "note.txt"
        path.write_text("x", encoding="utf-8")
        with self.assertRaises(InvalidDocumentError):
            self.documents.store_document(path)

    def test_same_filename_different_content_gets_unique_storage_paths(self):
        first_source = self.write_pdf(self.incoming_dir / "a" / "report.pdf", b"one")
        second_source = self.write_pdf(self.incoming_dir / "b" / "report.pdf", b"two")

        first = self.documents.store_document(first_source)
        second = self.documents.store_document(second_source)

        self.assertNotEqual(first.path, second.path)
        self.assertEqual(first.path.name, "report.pdf")
        self.assertEqual(second.path.name, "report (2).pdf")
        self.assertIn(b"one", first.path.read_bytes())
        self.assertIn(b"two", second.path.read_bytes())

    def test_same_name_different_content_is_not_a_duplicate(self):
        first_source = self.write_pdf(self.incoming_dir / "a" / "report.pdf", b"one")
        request = self.make_request(str(first_source))
        self.requests.create_request(request)

        second_source = self.write_pdf(self.incoming_dir / "b" / "report.pdf", b"two")
        duplicate = self.documents.find_duplicate_document(second_source)
        self.assertIsNone(duplicate)

    def test_duplicate_is_found_by_hash_even_with_different_name(self):
        source = self.write_pdf(self.incoming_dir / "source.pdf", b"same")
        request = self.make_request(str(source))
        request_id = self.requests.create_request(request)

        other = self.write_pdf(self.incoming_dir / "different-name.pdf", b"same")
        duplicate = self.documents.find_duplicate_document(other)
        self.assertEqual(duplicate.id, request_id)

    def test_duplicate_search_can_exclude_current_request(self):
        source = self.write_pdf(self.incoming_dir / "source.pdf", b"same")
        request = self.make_request(str(source))
        request_id = self.requests.create_request(request)
        stored = self.requests.get_request(request_id).source_document

        duplicate = self.documents.find_duplicate_document(
            stored,
            exclude_request_id=request_id,
        )
        self.assertIsNone(duplicate)


    def test_legacy_flat_trash_is_discovered_for_restore(self):
        original = self.db.documents_dir / "legacy.pdf"
        self.documents.trash_dir.mkdir(parents=True, exist_ok=True)
        legacy = self.documents.trash_dir / original.name
        self.write_pdf(legacy, b"legacy")

        found = self.documents.find_latest_trashed(original, owner_id=123)
        self.assertEqual(found, legacy)


    def test_document_entries_capture_metadata_in_one_snapshot(self):
        first = self.write_pdf(self.db.documents_dir / "A.pdf", b"aaaa")
        second = self.write_pdf(self.db.documents_dir / "b.pdf", b"bbbbbb")

        entries = self.documents.list_document_entries()

        self.assertEqual([entry.path.name for entry in entries], ["A.pdf", "b.pdf"])
        sizes = {entry.path.name: entry.size_bytes for entry in entries}
        self.assertEqual(sizes[first.name], first.stat().st_size)
        self.assertEqual(sizes[second.name], second.stat().st_size)

    def test_hash_cache_is_bounded(self):
        self.documents._hash_cache_limit = 3
        for index in range(6):
            path = self.write_pdf(self.incoming_dir / f"cache-{index}.pdf", str(index).encode())
            self.assertIsNotNone(self.documents.calculate_file_hash(path))
        self.assertLessEqual(len(self.documents._hash_cache), 3)

    def test_list_documents_is_sorted_case_insensitively(self):
        self.write_pdf(self.db.documents_dir / "b.pdf", b"b")
        self.write_pdf(self.db.documents_dir / "A.pdf", b"a")
        names = [path.name for path in self.documents.list_documents()]
        self.assertEqual(names, ["A.pdf", "b.pdf"])

    def test_repair_stored_document_path_after_app_move(self):
        source = self.write_pdf(self.incoming_dir / "portable.pdf", b"portable")
        request = self.make_request(str(source))
        request_id = self.requests.create_request(request)
        stored = Path(request.source_document)

        self.db.repair_source_document_path(
            request_id,
            r"C:\old-computer\DocumentRegistry\data\documents\portable.pdf",
        )
        self.assertEqual(self.documents.repair_stored_document_paths(), 1)
        repaired = self.requests.get_request(request_id)
        self.assertEqual(Path(repaired.source_document), stored)

    def test_resolve_windows_reference_by_basename(self):
        stored = self.write_pdf(self.db.documents_dir / "letter.pdf", b"x")
        resolved = self.documents.resolve_document_reference(
            r"D:\Old Registry\data\documents\letter.pdf"
        )
        self.assertEqual(resolved, stored.resolve())


if __name__ == "__main__":
    unittest.main()
