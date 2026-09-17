import tempfile
import unittest
from pathlib import Path

from app.database import Database
from app.document_service import DocumentService
from app.models import Request
from app.request_service import RequestService
from app.settings_service import AppSettings, SettingsService


class PortabilityIntegrationTests(unittest.TestCase):
    def test_moving_whole_runtime_folder_repairs_settings_and_pdf_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            old_root = base / "OldDesktop" / "DocumentRegistry"
            old_data = old_root / "data"
            old_docs = old_data / "documents"
            old_docs.mkdir(parents=True)

            db = Database(data_dir=old_data, documents_dir=old_docs)
            documents = DocumentService(db, old_docs)
            requests = RequestService(db, documents)

            incoming = base / "incoming.pdf"
            incoming.write_bytes(b"%PDF-1.4\nportable move")
            request = Request(
                received_date="17.09.2026",
                from_department="Отдел",
                from_person="Иванов",
                to_person="Петров",
                request_subject="Проверка переноса",
                source_document=str(incoming),
            )
            request_id = requests.create_request(request)
            old_stored = Path(request.source_document)
            self.assertTrue(old_stored.is_file())

            settings_service = SettingsService(
                old_data / "settings.json",
                old_docs,
                runtime_root=old_root,
            )
            settings_service.save(AppSettings(documents_dir=str(old_docs)))

            new_root = base / "NewDesktop" / "Registry Anywhere"
            new_root.parent.mkdir(parents=True)
            old_root.rename(new_root)

            new_data = new_root / "data"
            new_docs = new_data / "documents"
            moved_settings = SettingsService(
                new_data / "settings.json",
                new_docs,
                runtime_root=new_root,
            ).load()
            self.assertEqual(Path(moved_settings.documents_dir), new_docs)

            moved_db = Database(data_dir=new_data, documents_dir=new_docs)
            moved_documents = DocumentService(moved_db, new_docs)
            self.assertEqual(moved_documents.repair_stored_document_paths(), 1)

            moved_request = moved_db.get_request(request_id)
            self.assertIsNotNone(moved_request)
            self.assertEqual(Path(moved_request.source_document), new_docs / old_stored.name)
            self.assertTrue(Path(moved_request.source_document).is_file())


if __name__ == "__main__":
    unittest.main()
