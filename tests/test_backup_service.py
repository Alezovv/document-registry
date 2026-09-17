import os
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from app.backup_service import BackupService
from app.database import Database
from app.document_service import DocumentService
from app.models import Request
from app.request_service import RequestService


class BackupServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = Database(data_dir=root / "data")
        self.documents = DocumentService(self.db, self.db.documents_dir)
        self.requests = RequestService(self.db, self.documents)
        self.backups = BackupService(self.db, self.documents)
        self.root = root

    def tearDown(self):
        self.temp.cleanup()

    def _request(self, source=""):
        return Request(
            received_date="16.09.2026",
            due_date="20.09.2026",
            from_department="Отдел",
            from_person="Иванов",
            to_person="Петров",
            request_subject="Проверка backup",
            source_document=str(source) if source else "",
        )

    def test_backup_and_restore_database_documents_and_trash(self):
        incoming = self.root / "incoming.pdf"
        incoming.write_bytes(b"%PDF-1.4\noriginal")
        request = self._request(incoming)
        request_id = self.requests.create_request(request)
        stored = Path(request.source_document)

        backup_path = self.root / "backup.zip"
        result = self.backups.create_backup(backup_path)
        self.assertEqual(result.document_count, 1)
        self.assertTrue(backup_path.exists())

        current = self.requests.get_request(request_id)
        current.request_subject = "Изменено после backup"
        self.requests.update_request(current)
        stored.write_bytes(b"%PDF-1.4\nchanged")

        self.backups.restore_backup(backup_path)

        restored = self.requests.get_request(request_id)
        self.assertEqual(restored.request_subject, "Проверка backup")
        self.assertEqual(restored.due_date, "20.09.2026")
        self.assertIn(b"original", stored.read_bytes())

    def test_backup_manifest_can_be_inspected(self):
        backup_path = self.root / "backup.zip"
        self.backups.create_backup(backup_path)
        manifest = self.backups.inspect_backup(backup_path)
        self.assertEqual(manifest["format_version"], 1)
        self.assertIn("created_at", manifest)

    def test_weekly_auto_backup_due_uses_single_rolling_file(self):
        path = self.root / "auto_registry_backup.zip"
        self.assertTrue(self.backups.is_backup_due(path, 7))

        self.backups.create_backup(path)
        self.assertFalse(self.backups.is_backup_due(path, 7))

        old = time.time() - 8 * 24 * 60 * 60
        os.utime(path, (old, old))
        self.assertTrue(self.backups.is_backup_due(path, 7))

        self.backups.create_backup(path)
        self.assertTrue(path.is_file())
        self.assertFalse(self.backups.is_backup_due(path, 7))
        self.assertEqual(len(list(path.parent.glob("auto_registry_backup*.zip"))), 1)


    def test_failed_auto_backup_keeps_previous_copy(self):
        path = self.root / "auto_registry_backup.zip"
        self.backups.create_backup(path)
        previous = path.read_bytes()

        with patch.object(self.backups, "_snapshot_database", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.backups.create_backup(path)

        self.assertEqual(path.read_bytes(), previous)


    def test_restore_rebinds_document_paths_from_another_computer(self):
        incoming = self.root / "portable.pdf"
        incoming.write_bytes(b"%PDF-1.4\nportable")
        request = self._request(incoming)
        request_id = self.requests.create_request(request)
        stored = Path(request.source_document)

        self.db.repair_source_document_path(
            request_id,
            r"C:\OldPC\registry\data\documents\portable.pdf",
        )
        backup_path = self.root / "foreign-path-backup.zip"
        self.backups.create_backup(backup_path)

        self.backups.restore_backup(backup_path)
        restored = self.requests.get_request(request_id)
        self.assertEqual(Path(restored.source_document), stored)
        self.assertTrue(stored.is_file())


if __name__ == "__main__":
    unittest.main()
