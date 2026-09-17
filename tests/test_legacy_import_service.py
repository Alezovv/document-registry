import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.backup_service import BackupService
from app.database import Database
from app.document_service import DocumentService
from app.legacy_import_service import LegacyImportError, LegacyImportService


class LegacyImportServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.current_data = self.root / "current" / "data"
        self.current_docs = self.current_data / "documents"
        self.database = Database(data_dir=self.current_data, documents_dir=self.current_docs)
        self.documents = DocumentService(self.database, self.current_docs)
        self.backup = BackupService(self.database, self.documents)
        self.service = LegacyImportService(self.database, self.documents, self.backup)

    def tearDown(self):
        self.temp.cleanup()

    def _make_legacy_db(self, with_document=True):
        legacy_root = self.root / "legacy"
        legacy_root.mkdir(parents=True, exist_ok=True)
        legacy_docs = legacy_root / "documents"
        legacy_docs.mkdir(exist_ok=True)
        db_path = legacy_root / "old_data.db"
        source_document = ""
        if with_document:
            pdf = legacy_docs / "old-request.pdf"
            pdf.write_bytes(b"%PDF-1.4\nlegacy\n%%EOF")
            source_document = str(pdf)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_date TEXT DEFAULT '',
                    from_department TEXT DEFAULT '',
                    from_position TEXT DEFAULT '',
                    from_person TEXT DEFAULT '',
                    to_person TEXT DEFAULT '',
                    request_subject TEXT DEFAULT '',
                    note TEXT DEFAULT '',
                    status TEXT DEFAULT 'Новый',
                    source_document TEXT DEFAULT '',
                    is_pinned INTEGER DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                INSERT INTO requests (
                    id, received_date, from_department, from_position,
                    from_person, to_person, request_subject, note,
                    status, source_document, is_pinned
                ) VALUES (7, '15.06.2026', 'ОП №8', 'Инспектор',
                          'Иванов И.И.', 'Петров П.П.', 'Запрос сведений', '',
                          'В работе', ?, 1)
                """,
                (source_document,),
            )
            conn.commit()
        finally:
            conn.close()
        return db_path, legacy_docs

    def test_inspect_is_read_only_and_detects_documents(self):
        db_path, legacy_docs = self._make_legacy_db()
        inspection = self.service.inspect(db_path)
        self.assertEqual(inspection.record_count, 1)
        self.assertEqual(inspection.document_references, 1)
        self.assertEqual(inspection.documents_found, 1)
        self.assertEqual(inspection.documents_missing, 0)
        self.assertEqual(inspection.suggested_documents_dir, legacy_docs.resolve())

    def test_migrate_builds_current_schema_and_copies_documents(self):
        # Existing data must be backed up before replacement.
        with self.database._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO requests(received_date, from_person, status)
                VALUES ('01.01.2025', 'Текущая запись', 'Новый')
                """
            )

        db_path, legacy_docs = self._make_legacy_db()
        backup_dir = self.root / "backups"
        result = self.service.migrate(db_path, legacy_docs, backup_dir=backup_dir)

        self.assertEqual(result.imported_records, 1)
        self.assertEqual(result.copied_documents, 1)
        self.assertEqual(result.missing_documents, 0)
        self.assertTrue(result.backup_path.is_file())

        requests = self.database.get_requests()
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.id, 7)
        self.assertEqual(request.received_date, '15.06.2026')
        self.assertEqual(request.due_date, '')
        self.assertEqual(request.completed_date, '')
        self.assertEqual(request.from_department, 'ОП №8')
        self.assertEqual(request.from_person, 'Иванов И.И.')
        self.assertTrue(request.is_pinned)
        self.assertTrue(Path(request.source_document).is_file())
        self.assertEqual(Path(request.source_document).parent, self.current_docs.resolve())

        conn = sqlite3.connect(self.database.db_path)
        try:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(requests)')}
            self.assertIn('created_at', columns)
            self.assertIn('updated_at', columns)
            self.assertIn('due_date', columns)
            self.assertIn('completed_date', columns)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], Database.SCHEMA_VERSION)
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0].lower(), 'ok')
        finally:
            conn.close()

    def test_missing_document_reference_is_preserved_and_reported(self):
        db_path, _ = self._make_legacy_db(with_document=False)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE requests SET source_document = 'Z:/lost/missing.pdf'")
            conn.commit()
        finally:
            conn.close()

        result = self.service.migrate(db_path, backup_dir=self.root / 'backups')
        self.assertEqual(result.copied_documents, 0)
        self.assertEqual(result.missing_documents, 1)
        request = self.database.get_requests()[0]
        self.assertEqual(request.source_document, 'Z:/lost/missing.pdf')

    def test_rejects_current_database(self):
        with self.assertRaises(LegacyImportError):
            self.service.inspect(self.database.db_path)

    def test_rejects_unrelated_sqlite_database(self):
        path = self.root / 'unrelated.db'
        conn = sqlite3.connect(path)
        try:
            conn.execute('CREATE TABLE something_else(id INTEGER PRIMARY KEY)')
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(LegacyImportError):
            self.service.inspect(path)


if __name__ == '__main__':
    unittest.main()
