from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.database import Database
from app.document_service import DocumentService
from app.models import Request


class V1146RegressionTests(unittest.TestCase):
    def test_missing_foreign_legacy_reference_keeps_original_slashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = Database(
                data_dir=root / "data",
                documents_dir=root / "data" / "documents",
                db_path=root / "data" / "requests.db",
            )
            request_id = db.add_request(Request(source_document=""))
            conn = sqlite3.connect(db.db_path)
            try:
                conn.execute(
                    "UPDATE requests SET source_document = ? WHERE id = ?",
                    ("Z:/lost/missing.pdf", request_id),
                )
                conn.commit()
            finally:
                conn.close()
            request = db.get_request(request_id)
            self.assertIsNotNone(request)
            self.assertEqual(request.source_document, "Z:/lost/missing.pdf")

    def test_first_automatic_runtime_rebind_reports_existing_token_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_root = root / "OldRuntime"
            old_data = old_root / "data"
            old_docs = old_data / "documents"
            db = Database(data_dir=old_data, documents_dir=old_docs, db_path=old_data / "requests.db")
            old_docs.mkdir(parents=True, exist_ok=True)
            pdf = old_docs / "portable.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            request_id = db.add_request(Request(source_document=str(pdf)))

            old_root.rename(root / "NewRuntime")
            new_root = root / "NewRuntime"
            new_data = new_root / "data"
            new_docs = new_data / "documents"
            moved_db = Database(data_dir=new_data, documents_dir=new_docs, db_path=new_data / "requests.db")
            service = DocumentService(moved_db, new_docs)

            self.assertEqual(service.repair_stored_document_paths(), 1)
            moved = moved_db.get_request(request_id)
            self.assertIsNotNone(moved)
            self.assertEqual(Path(moved.source_document), new_docs / pdf.name)
            self.assertEqual(service.repair_stored_document_paths(), 0)


if __name__ == "__main__":
    unittest.main()
