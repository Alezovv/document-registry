import os
from pathlib import Path
import tempfile
import unittest
import zipfile

from app.app_paths import AppPaths, get_app_paths
from app.database import Database
from app.diagnostics_service import DiagnosticsService
from app.document_service import DocumentService
from app.models import Request
from app.performance_diagnostics import PerformanceDiagnostics


class DiagnosticsTests(unittest.TestCase):
    def _paths(self, root: Path) -> AppPaths:
        return AppPaths(
            executable_dir=root,
            resources=root,
            runtime=root,
            data=root / "data",
            documents=root / "data" / "documents",
            backups=root / "backups",
            logs=root / "logs",
            paddle_cache=root / "data" / "paddle_cache",
            assets=root / "assets",
            models=root / "models",
        )

    def test_package_contains_no_database_contents_or_pdf(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self._paths(root)
            for folder in (paths.data, paths.documents, paths.backups, paths.logs, paths.paddle_cache):
                folder.mkdir(parents=True, exist_ok=True)
            db = Database(data_dir=paths.data, db_path=paths.database, documents_dir=paths.documents)
            db.add_request(Request(
                received_date="17.09.2026",
                from_person="Секретный Иван Иванович",
                request_subject="Очень секретное содержание записи",
                note="Личная заметка",
            ))
            (paths.documents / "Секретный Иван Иванович.pdf").write_bytes(b"%PDF-test")
            (paths.logs / "document-registry.log").write_text(
                "2026-09-17 12:00:00 | ERROR | app.test | Ошибка файла C:\\\\Docs\\\\Секретный Иван Иванович.pdf\n",
                encoding="utf-8",
            )
            service = DiagnosticsService(db, DocumentService(db, paths.documents), PerformanceDiagnostics(), paths)
            result = service.create_package(root)
            self.assertTrue(result.path and result.path.is_file())
            with zipfile.ZipFile(result.path) as archive:
                names = set(archive.namelist())
                self.assertIn("database.txt", names)
                self.assertNotIn("requests.db", names)
                self.assertFalse(any(name.lower().endswith(".pdf") for name in names))
                payload = "\n".join(
                    archive.read(name).decode("utf-8", errors="replace")
                    for name in names
                    if not name.endswith("/")
                )
            self.assertNotIn("Очень секретное содержание записи", payload)
            self.assertNotIn("Личная заметка", payload)
            self.assertNotIn("Секретный Иван Иванович.pdf", payload)

    def test_environment_override_keeps_portable_paths_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            previous = os.environ.get("DOCUMENT_REGISTRY_HOME")
            os.environ["DOCUMENT_REGISTRY_HOME"] = temp
            try:
                paths = get_app_paths()
                self.assertEqual(paths.runtime, Path(temp).resolve())
                self.assertEqual(paths.database, Path(temp).resolve() / "data" / "requests.db")
            finally:
                if previous is None:
                    os.environ.pop("DOCUMENT_REGISTRY_HOME", None)
                else:
                    os.environ["DOCUMENT_REGISTRY_HOME"] = previous


if __name__ == "__main__":
    unittest.main()
