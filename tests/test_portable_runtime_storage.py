from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from app.backup_service import BackupService
from app.database import Database
from app.document_service import DocumentService
from app.models import Request
from app.request_service import RequestService
from app.settings_service import AppSettings, SettingsService


class PortableRuntimeStorageTests(unittest.TestCase):
    @staticmethod
    def _raw_source(db_path: Path, request_id: int) -> str:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT source_document FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
            return str(row[0] or "")
        finally:
            conn.close()

    def test_request_database_stores_runtime_token_not_machine_path(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "UserA" / "Desktop" / "DocumentRegistry"
            data = runtime / "data"
            docs = data / "documents"
            db = Database(documents_dir=docs, data_dir=data, db_path=data / "requests.db")
            service = RequestService(db)

            source = Path(temp) / "incoming.pdf"
            source.write_bytes(b"%PDF-1.4\n%%EOF\n")
            request = Request(source_document=str(source))
            request_id = service.create_request(request)

            raw = self._raw_source(db.db_path, request_id)
            self.assertEqual(raw, f"@runtime/data/documents/{Path(request.source_document).name}")
            self.assertNotIn("UserA", raw)
            self.assertNotIn(str(runtime), raw)

    def test_whole_desktop_folder_can_move_to_another_computer_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_runtime = root / "PC-A" / "Users" / "Alice" / "Desktop" / "DocumentRegistry"
            old_data = old_runtime / "data"
            old_docs = old_data / "documents"
            db = Database(documents_dir=old_docs, data_dir=old_data, db_path=old_data / "requests.db")
            requests = RequestService(db)
            settings = SettingsService(
                old_data / "settings.json", old_docs, runtime_root=old_runtime
            )
            settings.save(AppSettings(documents_dir=str(old_docs)))

            source = root / "letter.pdf"
            source.write_bytes(b"%PDF-1.4\n%%EOF\n")
            request = Request(source_document=str(source))
            request_id = requests.create_request(request)
            filename = Path(request.source_document).name

            new_runtime = root / "PC-B" / "Users" / "Operator" / "Desktop" / "DocumentRegistry"
            new_runtime.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_runtime), str(new_runtime))
            new_data = new_runtime / "data"
            new_docs = new_data / "documents"

            moved_db = Database(
                documents_dir=new_docs, data_dir=new_data, db_path=new_data / "requests.db"
            )
            moved_documents = DocumentService(moved_db, new_docs)
            moved_documents.repair_stored_document_paths(preserve_runtime_spelling=True)
            moved_request = moved_db.get_request(request_id)
            self.assertIsNotNone(moved_request)
            self.assertEqual(Path(moved_request.source_document), new_docs / filename)
            self.assertTrue(Path(moved_request.source_document).is_file())
            self.assertEqual(
                self._raw_source(moved_db.db_path, request_id),
                f"@runtime/data/documents/{filename}",
            )

            moved_settings = SettingsService(
                new_data / "settings.json", new_docs, runtime_root=new_runtime
            ).load()
            self.assertEqual(Path(moved_settings.documents_dir), new_docs)

    def test_backup_restored_to_another_runtime_keeps_database_portable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_runtime = root / "FirstPC" / "Desktop" / "DocumentRegistry"
            source_data = source_runtime / "data"
            source_docs = source_data / "documents"
            source_db = Database(
                documents_dir=source_docs,
                data_dir=source_data,
                db_path=source_data / "requests.db",
            )
            source_documents = DocumentService(source_db, source_docs)
            source_requests = RequestService(source_db, source_documents)

            incoming = root / "scan.pdf"
            incoming.write_bytes(b"%PDF-1.4\n%%EOF\n")
            request = Request(source_document=str(incoming))
            request_id = source_requests.create_request(request)

            backup_path = root / "portable.zip"
            BackupService(source_db, source_documents).create_backup(backup_path)

            target_runtime = root / "SecondPC" / "Desktop" / "DocumentRegistry"
            target_data = target_runtime / "data"
            target_docs = target_data / "documents"
            target_db = Database(
                documents_dir=target_docs,
                data_dir=target_data,
                db_path=target_data / "requests.db",
            )
            target_documents = DocumentService(target_db, target_docs)
            BackupService(target_db, target_documents).restore_backup(backup_path)

            restored = target_db.get_request(request_id)
            self.assertIsNotNone(restored)
            self.assertTrue(Path(restored.source_document).is_file())
            self.assertEqual(Path(restored.source_document).parent, target_docs.resolve())
            self.assertTrue(self._raw_source(target_db.db_path, request_id).startswith("@runtime/"))

    def test_history_snapshot_does_not_keep_runtime_absolute_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "Desktop" / "DocumentRegistry"
            data = runtime / "data"
            docs = data / "documents"
            db = Database(documents_dir=docs, data_dir=data, db_path=data / "requests.db")
            requests = RequestService(db)
            source = Path(temp) / "delete-me.pdf"
            source.write_bytes(b"%PDF-1.4\n%%EOF\n")
            request = Request(source_document=str(source))
            requests.create_request(request)
            requests.delete_request(request.id)

            history = db.get_history()[0]
            snapshot = json.loads(history["snapshot"])
            self.assertTrue(snapshot["source_document"].startswith("@runtime/"))
            self.assertTrue(snapshot["_trash_path"].startswith("@runtime/"))
            self.assertNotIn(str(runtime), history["snapshot"])

    def test_legacy_foreign_absolute_path_in_backup_rebinds_locally(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_runtime = root / "SourcePC" / "Desktop" / "DocumentRegistry"
            source_data = source_runtime / "data"
            source_docs = source_data / "documents"
            source_db = Database(
                documents_dir=source_docs, data_dir=source_data, db_path=source_data / "requests.db"
            )
            source_documents = DocumentService(source_db, source_docs)
            source_requests = RequestService(source_db, source_documents)
            incoming = root / "legacy.pdf"
            incoming.write_bytes(b"%PDF-1.4\n%%EOF\n")
            request = Request(source_document=str(incoming))
            request_id = source_requests.create_request(request)
            stored_name = Path(request.source_document).name

            conn = sqlite3.connect(source_db.db_path)
            try:
                conn.execute(
                    "UPDATE requests SET source_document = ? WHERE id = ?",
                    (rf"Z:\OldUser\Desktop\DocumentRegistry\data\documents\{stored_name}", request_id),
                )
                conn.commit()
            finally:
                conn.close()

            backup_path = root / "legacy-backup.zip"
            BackupService(source_db, source_documents).create_backup(backup_path)

            target_runtime = root / "TargetPC" / "Desktop" / "DocumentRegistry"
            target_data = target_runtime / "data"
            target_docs = target_data / "documents"
            target_db = Database(
                documents_dir=target_docs, data_dir=target_data, db_path=target_data / "requests.db"
            )
            target_documents = DocumentService(target_db, target_docs)
            BackupService(target_db, target_documents).restore_backup(backup_path)

            restored = target_db.get_request(request_id)
            self.assertIsNotNone(restored)
            self.assertEqual(Path(restored.source_document), (target_docs / stored_name).resolve())
            self.assertEqual(
                self._raw_source(target_db.db_path, request_id),
                f"@runtime/data/documents/{stored_name}",
            )

    def test_replacement_history_restore_remains_reversible_with_tokens(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "Desktop" / "DocumentRegistry"
            data = runtime / "data"
            docs = data / "documents"
            db = Database(documents_dir=docs, data_dir=data, db_path=data / "requests.db")
            requests = RequestService(db)

            first = Path(temp) / "first.pdf"
            second = Path(temp) / "second.pdf"
            first.write_bytes(b"%PDF-1.4\nfirst\n%%EOF\n")
            second.write_bytes(b"%PDF-1.4\nsecond\n%%EOF\n")
            request = Request(source_document=str(first))
            requests.create_request(request)
            old_stored = Path(request.source_document)

            request.source_document = str(second)
            requests.update_request(request)
            history_id = requests.get_history()[0]["id"]
            self.assertTrue(requests.restore_request_from_history(history_id))

            restored = requests.get_request(request.id)
            self.assertIsNotNone(restored)
            self.assertEqual(Path(restored.source_document), old_stored)
            self.assertTrue(self._raw_source(db.db_path, request.id).startswith("@runtime/"))


if __name__ == "__main__":
    unittest.main()
