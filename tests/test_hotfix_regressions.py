from pathlib import Path
import tempfile
import unittest

from app.database import Database
from app.document_service import DocumentService
from app.models import Request
from app.request_service import RequestService
from app.ocr import OCR
from app.settings_service import SettingsService


class HotfixRegressionTests(unittest.TestCase):
    def test_update_download_window_is_minimizable_native_window(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "app" / "ui" / "update_controller.py").read_text(encoding="utf-8")
        self.assertIn("Qt.WindowType.Window\n", source)
        self.assertIn("Qt.WindowType.WindowMinimizeButtonHint", source)
        self.assertIn("Qt.WindowType.WindowCloseButtonHint", source)
        self.assertNotIn("dialog.windowFlags()\n            | Qt.WindowType.WindowMinimizeButtonHint", source)

    def test_settings_paths_are_made_absolute_without_resolving_aliases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            try:
                alias.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are not available")

            documents = alias / "data" / "documents"
            service = SettingsService(
                alias / "data" / "settings.json",
                documents,
                runtime_root=alias,
            )
            self.assertEqual(service.default_documents_dir, documents)
            self.assertEqual(Path(service.defaults().documents_dir), documents)

    def test_extract_page_does_not_require_rect_on_test_double(self):
        class FakePage:
            def get_text(self, mode):
                self_mode = mode
                return [(0, 0, 90, 20, "bad", 0, 0)]

        ocr = OCR()
        ocr._ocr_page = lambda *args, **kwargs: (
            "Распознанный текст документа с достаточным количеством символов для проверки.",
            [],
        )
        text, _ = ocr._extract_page([FakePage()], 0)
        self.assertIn("Распознанный", text)

    def test_main_window_remains_thin(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "app" / "window.py").read_text(encoding="utf-8")
        self.assertLessEqual(len(source.splitlines()), 450)

    def test_document_service_returns_canonical_path_but_keeps_runtime_spelling(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            try:
                alias.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are not available")

            data_dir = alias / "data"
            documents = data_dir / "documents"
            database = Database(
                documents_dir=documents,
                data_dir=data_dir,
                db_path=data_dir / "requests.db",
            )
            service = DocumentService(database, documents)
            self.assertEqual(service.documents_dir, documents)

            pdf = documents / "portable.pdf"
            pdf.parent.mkdir(parents=True, exist_ok=True)
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            self.assertEqual(service.resolve_document_reference(pdf), pdf.resolve())

    def test_request_create_returns_canonical_stored_pdf_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            try:
                alias.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are not available")

            data_dir = alias / "data"
            documents = data_dir / "documents"
            database = Database(
                documents_dir=documents,
                data_dir=data_dir,
                db_path=data_dir / "requests.db",
            )
            service = RequestService(database)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.4\n%%EOF\n")
            request = Request(source_document=str(source))

            service.create_request(request)

            stored = Path(request.source_document)
            self.assertEqual(stored.parent, database.documents_dir.resolve())

    def test_repair_persists_current_runtime_spelling_after_move(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            try:
                alias.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are not available")

            old_runtime = alias / "OldRuntime"
            old_data = old_runtime / "data"
            old_documents = old_data / "documents"
            database = Database(
                documents_dir=old_documents,
                data_dir=old_data,
                db_path=old_data / "requests.db",
            )
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.4\n%%EOF\n")
            old_service = DocumentService(database, old_documents)
            stored = old_service.store_document(source).path
            request_id = database.add_request(Request(source_document=str(stored)))

            (real / "OldRuntime").rename(real / "NewRuntime")
            new_runtime = alias / "NewRuntime"
            new_data = new_runtime / "data"
            new_documents = new_data / "documents"
            moved_database = Database(
                documents_dir=new_documents,
                data_dir=new_data,
                db_path=new_data / "requests.db",
            )
            moved_service = DocumentService(moved_database, new_documents)

            # New databases already store @runtime/... so a folder move may
            # require no DB rewrite; repair still switches model paths to the
            # exact current runtime spelling.
            moved_service.repair_stored_document_paths()
            repaired = moved_database.get_request(request_id)
            self.assertIsNotNone(repaired)
            self.assertEqual(
                Path(repaired.source_document),
                new_documents / stored.name,
            )


    def test_unrelated_stale_reference_repairs_to_canonical_existing_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            try:
                alias.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are not available")

            data_dir = alias / "Runtime" / "data"
            documents = data_dir / "documents"
            database = Database(
                documents_dir=documents,
                data_dir=data_dir,
                db_path=data_dir / "requests.db",
            )
            documents.mkdir(parents=True, exist_ok=True)
            pdf = documents / "rebound.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            request_id = database.add_request(
                Request(
                    source_document=str(
                        root / "another-computer" / "App" / "data" / "documents" / pdf.name
                    )
                )
            )

            service = DocumentService(database, documents)
            self.assertEqual(
                service.repair_stored_document_paths(preserve_runtime_spelling=False),
                1,
            )
            repaired = database.get_request(request_id)
            self.assertIsNotNone(repaired)
            self.assertEqual(Path(repaired.source_document), pdf.resolve())

    def test_delete_history_restore_persists_canonical_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            try:
                alias.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are not available")

            data_dir = alias / "Runtime" / "data"
            documents = data_dir / "documents"
            database = Database(
                documents_dir=documents,
                data_dir=data_dir,
                db_path=data_dir / "requests.db",
            )
            requests = RequestService(database)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.4\n%%EOF\n")
            request = Request(source_document=str(source))
            requests.create_request(request)
            stored = Path(request.source_document)

            self.assertTrue(requests.delete_request(request.id))
            history_id = requests.get_history()[0]["id"]
            self.assertTrue(requests.restore_request_from_history(history_id))
            restored = requests.get_request(request.id)
            self.assertIsNotNone(restored)
            self.assertEqual(Path(restored.source_document), stored)

    def test_windows_build_files_keep_models_external_and_run_self_check(self):
        root = Path(__file__).resolve().parents[1]
        spec = (root / "packaging" / "DocumentRegistry.spec").read_text(encoding="utf-8")
        script = (root / "packaging" / "build.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("app_icon.ico", spec)
        self.assertNotIn("models", spec.lower())
        self.assertIn("Remove-Item", script)
        self.assertIn('Join-Path $Root "models"', script)
        self.assertIn("portable.flag", script)
        self.assertIn("--self-check", script)


if __name__ == "__main__":
    unittest.main()
