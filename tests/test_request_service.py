import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.database import Database
from app.document_service import DocumentService, DocumentStorageError
from app.models import Request
from app.request_service import RequestService


class RequestServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = Database(data_dir=self.root / "data")
        self.documents = DocumentService(self.db, self.db.documents_dir)
        self.service = RequestService(self.db, self.documents)
        self.incoming = self.root / "incoming"
        self.incoming.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_pdf(self, name: str, payload: bytes) -> Path:
        path = self.incoming / name
        path.write_bytes(b"%PDF-1.4\n" + payload)
        return path

    def make_request(self, source="") -> Request:
        return Request(
            received_date="16.09.2026",
            from_department="Отдел",
            from_position="Начальник",
            from_person="Иванов",
            to_person="Петров",
            request_subject="Запрос",
            status="Новый",
            source_document=str(source) if source else "",
        )

    def test_create_copies_pdf_only_when_request_is_saved(self):
        source = self.write_pdf("create.pdf", b"payload")
        request = self.make_request(source)

        request_id = self.service.create_request(request)
        stored = Path(self.service.get_request(request_id).source_document)

        self.assertTrue(source.exists())
        self.assertTrue(stored.exists())
        self.assertEqual(stored.parent, self.db.documents_dir.resolve())
        self.assertNotEqual(stored, source.resolve())

    def test_delete_moves_pdf_to_request_specific_trash_and_restore_returns_it(self):
        source = self.write_pdf("tracked.pdf", b"tracked")
        request = self.make_request(source)
        request_id = self.service.create_request(request)
        stored = Path(request.source_document)

        self.assertTrue(self.service.delete_request(request_id))
        self.assertFalse(stored.exists())

        delete_record = next(
            row for row in self.service.get_history()
            if row["request_id"] == request_id and row["action"] == "delete"
        )
        self.assertTrue(self.service.restore_request_from_history(delete_record["id"]))

        restored = self.service.get_request(request_id)
        self.assertEqual(Path(restored.source_document), stored)
        self.assertTrue(stored.exists())
        self.assertIn(b"tracked", stored.read_bytes())

    def test_update_replacement_is_reversible_from_history(self):
        old_source = self.write_pdf("old.pdf", b"old-version")
        request = self.make_request(old_source)
        request_id = self.service.create_request(request)
        old_stored = Path(request.source_document)

        new_source = self.write_pdf("new.pdf", b"new-version")
        edited = self.service.get_request(request_id)
        edited.source_document = str(new_source)
        self.service.update_request(edited)
        new_stored = Path(edited.source_document)

        self.assertFalse(old_stored.exists())
        self.assertTrue(new_stored.exists())

        update_record = next(
            row for row in self.service.get_history()
            if row["request_id"] == request_id and row["action"] == "update"
        )
        self.service.restore_request_from_history(update_record["id"])

        restored = self.service.get_request(request_id)
        self.assertEqual(Path(restored.source_document), old_stored)
        self.assertTrue(old_stored.exists())
        self.assertFalse(new_stored.exists())
        self.assertIn(b"old-version", old_stored.read_bytes())

    def test_update_database_failure_rolls_back_file_moves_and_new_copy(self):
        old_source = self.write_pdf("old.pdf", b"old")
        request = self.make_request(old_source)
        request_id = self.service.create_request(request)
        old_stored = Path(request.source_document)

        new_source = self.write_pdf("new.pdf", b"new")
        edited = self.service.get_request(request_id)
        edited.source_document = str(new_source)

        with patch.object(self.db, "update_request", side_effect=RuntimeError("db failed")):
            with self.assertRaises(RuntimeError):
                self.service.update_request(edited)

        current = self.service.get_request(request_id)
        self.assertEqual(Path(current.source_document), old_stored)
        self.assertTrue(old_stored.exists())
        self.assertEqual(
            [p.name for p in self.db.documents_dir.iterdir() if p.is_file()],
            [old_stored.name],
        )


    def test_create_database_failure_removes_newly_copied_pdf(self):
        source = self.write_pdf("create-fail.pdf", b"payload")
        request = self.make_request(source)

        with patch.object(self.db, "add_request", side_effect=RuntimeError("db failed")):
            with self.assertRaises(RuntimeError):
                self.service.create_request(request)

        stored_files = [p for p in self.db.documents_dir.iterdir() if p.is_file()]
        self.assertEqual(stored_files, [])
        self.assertTrue(source.exists())

    def test_delete_database_failure_restores_pdf_to_original_location(self):
        source = self.write_pdf("delete-fail.pdf", b"payload")
        request = self.make_request(source)
        request_id = self.service.create_request(request)
        stored = Path(request.source_document)

        with patch.object(self.db, "delete_request", side_effect=RuntimeError("db failed")):
            with self.assertRaises(RuntimeError):
                self.service.delete_request(request_id)

        self.assertTrue(stored.exists())
        self.assertIsNotNone(self.service.get_request(request_id))


    def test_clear_history_also_clears_trash(self):
        source = self.write_pdf("trash-clear.pdf", b"payload")
        request_id = self.service.create_request(self.make_request(source))
        self.service.delete_request(request_id)

        before = self.service.get_trash_stats()
        self.assertGreater(before.file_count, 0)
        self.assertTrue(self.service.get_history())

        removed = self.service.clear_history()

        self.assertEqual(removed.file_count, before.file_count)
        self.assertEqual(self.service.get_history(), [])
        self.assertEqual(self.service.get_trash_stats().file_count, 0)

    def test_clear_history_file_cleanup_failure_keeps_trash_visible(self):
        source = self.write_pdf("trash-finalize-fail.pdf", b"payload")
        request_id = self.service.create_request(self.make_request(source))
        self.service.delete_request(request_id)
        before = self.service.get_trash_stats()

        with patch.object(
            self.documents,
            "finalize_staged_trash",
            side_effect=DocumentStorageError("disk busy"),
        ):
            with self.assertRaises(DocumentStorageError):
                self.service.clear_history()

        self.assertEqual(self.service.get_history(), [])
        self.assertEqual(self.service.get_trash_stats(), before)
        self.assertTrue(self.documents.trash_dir.exists())

    def test_clear_history_database_failure_restores_trash(self):
        source = self.write_pdf("trash-rollback.pdf", b"payload")
        request_id = self.service.create_request(self.make_request(source))
        self.service.delete_request(request_id)
        before = self.service.get_trash_stats()

        with patch.object(self.db, "clear_history", side_effect=RuntimeError("db failed")):
            with self.assertRaises(RuntimeError):
                self.service.clear_history()

        after = self.service.get_trash_stats()
        self.assertEqual(after, before)
        self.assertTrue(self.service.get_history())

    def test_toggle_pin_records_history_once(self):
        request_id = self.service.create_request(self.make_request())
        self.assertTrue(self.service.toggle_request_pinned(request_id))
        records = [
            row for row in self.service.get_history()
            if row["request_id"] == request_id and row["action"] == "pin"
        ]
        self.assertEqual(len(records), 1)

    def test_completion_date_is_maintained_automatically(self):
        request = self.make_request()
        request.status = "Исполнен"
        request_id = self.service.create_request(request)
        stored = self.service.get_request(request_id)
        self.assertTrue(stored.completed_date)

        stored.status = "В работе"
        self.service.update_request(stored)
        changed = self.service.get_request(request_id)
        self.assertEqual(changed.completed_date, "")



    def test_form_suggestions_come_from_existing_requests(self):
        request = self.make_request()
        request.from_department = "Архивный отдел"
        request.from_position = "Главный специалист"
        request.from_person = "Смирнов Алексей"
        request.to_person = "Кузнецова Мария"
        self.service.create_request(request)

        suggestions = self.service.get_form_suggestions()

        self.assertIn("Архивный отдел", suggestions["from_department"])
        self.assertIn("Главный специалист", suggestions["from_position"])
        self.assertIn("Смирнов Алексей", suggestions["from_person"])
        self.assertIn("Кузнецова Мария", suggestions["to_person"])


    def test_bulk_update_changes_status_due_date_and_pin_with_history(self):
        first = self.make_request()
        first.received_date = "01.09.2026"
        second = self.make_request()
        second.received_date = "10.09.2026"
        first_id = self.service.create_request(first)
        second_id = self.service.create_request(second)

        changed = self.service.bulk_update_requests(
            [first_id, second_id], status="Исполнен", due_mode="+7", pinned=True
        )
        self.assertEqual(changed, 2)

        first_saved = self.service.get_request(first_id)
        second_saved = self.service.get_request(second_id)
        self.assertEqual(first_saved.status, "Исполнен")
        self.assertEqual(second_saved.status, "Исполнен")
        self.assertTrue(first_saved.completed_date)
        self.assertTrue(second_saved.completed_date)
        self.assertEqual(first_saved.due_date, "08.09.2026")
        self.assertEqual(second_saved.due_date, "17.09.2026")
        self.assertTrue(first_saved.is_pinned)
        self.assertTrue(second_saved.is_pinned)

        history = [row for row in self.service.get_history() if row["action"] == "bulk_update"]
        self.assertEqual(len(history), 2)

    def test_single_bulk_actions_use_human_specific_history_codes(self):
        request = self.make_request()
        request_id = self.service.create_request(request)

        self.service.bulk_update_requests([request_id], status="В работе")
        self.service.bulk_update_requests([request_id], due_mode="today")
        self.service.bulk_update_requests([request_id], pinned=True)

        actions = [row["action"] for row in self.service.get_history()]
        self.assertIn("bulk_status", actions)
        self.assertIn("bulk_due", actions)
        self.assertIn("bulk_pin", actions)

    def test_bulk_clear_deadline_keeps_other_fields(self):
        request = self.make_request()
        request.due_date = "25.09.2026"
        request_id = self.service.create_request(request)

        self.service.bulk_update_requests([request_id], due_mode="clear")
        saved = self.service.get_request(request_id)
        self.assertEqual(saved.due_date, "")
        self.assertEqual(saved.status, "Новый")
        self.assertEqual(saved.from_person, "Иванов")



if __name__ == "__main__":
    unittest.main()
