from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.db_migrations import (
    CURRENT_SCHEMA_VERSION,
    capture_invariants,
    migrate_database,
    validate_database,
)
from app.updater_main import create_pre_update_backup, read_staged_manifest


class V1181AtomicMigrationTests(unittest.TestCase):
    def _make_legacy_database(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(
                """
                CREATE TABLE requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_date TEXT NOT NULL,
                    from_department TEXT,
                    from_position TEXT,
                    from_person TEXT,
                    to_person TEXT,
                    request_subject TEXT,
                    note TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'Новый',
                    source_document TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE request_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER,
                    action TEXT NOT NULL,
                    description TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO requests (
                    received_date, from_department, from_person, to_person,
                    request_subject, source_document
                ) VALUES (
                    '17.09.2026', 'ОМВД', 'Иванов И.И.', 'Петров П.П.',
                    'Проверка миграции', '@runtime/data/documents/test.pdf'
                );
                INSERT INTO request_history (request_id, action, description)
                VALUES (1, 'create', 'Создано');
                PRAGMA user_version = 1;
                """
            )
            conn.commit()

    def test_legacy_database_is_migrated_without_losing_rows_or_ids(self):
        with tempfile.TemporaryDirectory() as name:
            database = Path(name) / "requests.db"
            self._make_legacy_database(database)
            invariants = capture_invariants(database)

            report = migrate_database(
                database,
                target_schema=CURRENT_SCHEMA_VERSION,
                expected_invariants=invariants,
            )

            self.assertTrue(report.ok)
            self.assertEqual(report.schema_version, CURRENT_SCHEMA_VERSION)
            self.assertEqual(report.requests_count, 1)
            self.assertEqual(report.history_count, 1)
            with closing(sqlite3.connect(database)) as conn:
                row = conn.execute(
                    "SELECT id, request_subject, source_document, due_date, completed_date, is_pinned "
                    "FROM requests WHERE id = 1"
                ).fetchone()
                self.assertEqual(row[0], 1)
                self.assertEqual(row[1], "Проверка миграции")
                self.assertEqual(row[2], "@runtime/data/documents/test.pdf")
                self.assertEqual(row[3], "")
                self.assertEqual(row[4], "")
                self.assertEqual(row[5], 0)
                snapshot_column = {
                    item[1] for item in conn.execute("PRAGMA table_info(request_history)")
                }
                self.assertIn("snapshot", snapshot_column)

    def test_current_version_with_missing_legacy_column_is_repaired(self):
        with tempfile.TemporaryDirectory() as name:
            database = Path(name) / "requests.db"
            self._make_legacy_database(database)
            with closing(sqlite3.connect(database)) as conn:
                conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
                conn.commit()

            report = migrate_database(database, target_schema=CURRENT_SCHEMA_VERSION)
            self.assertTrue(report.ok)
            validate_database(database, expected_schema=CURRENT_SCHEMA_VERSION)

    def test_pre_update_backup_keeps_old_database_and_pdfs(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            database = root / "data" / "requests.db"
            self._make_legacy_database(database)
            documents = root / "data" / "documents"
            documents.mkdir(parents=True)
            original_pdf = documents / "archive.pdf"
            original_pdf.write_bytes(b"%PDF-test")
            (root / "data" / "settings.json").write_text(
                json.dumps({"documents_dir": "@runtime/data/documents"}),
                encoding="utf-8",
            )

            backup_dir, backup_db, old_schema = create_pre_update_backup(
                root,
                current_version="1.18.0",
                target_version="1.18.2",
            )

            self.assertEqual(old_schema, 1)
            self.assertIsNotNone(backup_db)
            self.assertTrue(backup_db.is_file())
            self.assertEqual((backup_dir / "documents" / "archive.pdf").read_bytes(), b"%PDF-test")
            with closing(sqlite3.connect(backup_db)) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)

    def test_internal_manifest_carries_database_schema(self):
        with tempfile.TemporaryDirectory() as name:
            stage = Path(name)
            (stage / "update_manifest.json").write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "version": "1.18.2",
                        "database_schema": CURRENT_SCHEMA_VERSION,
                    }
                ),
                encoding="utf-8",
            )
            manifest = read_staged_manifest(stage, "1.18.2")
            self.assertEqual(manifest["database_schema"], CURRENT_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()

class V1181UpdaterTransactionTests(unittest.TestCase):
    _make_legacy_database = V1181AtomicMigrationTests._make_legacy_database
    def _make_target(self, root: Path) -> None:
        (root / "_internal").mkdir(parents=True)
        (root / "DocumentRegistry.exe").write_bytes(b"old-exe")
        (root / "_internal" / "old.txt").write_text("old", encoding="utf-8")
        (root / "updater").mkdir(parents=True)
        (root / "updater" / "DocumentRegistryUpdater.exe").write_bytes(b"old-updater")
        (root / "version.json").write_text(
            json.dumps({"version": "1.18.0", "database_schema": 1}),
            encoding="utf-8",
        )
        self._make_legacy_database(root / "data" / "requests.db")
        docs = root / "data" / "documents"
        docs.mkdir(parents=True)
        (docs / "old.pdf").write_bytes(b"%PDF-old")
        (root / "data" / "settings.json").write_text(
            json.dumps({"documents_dir": "@runtime/data/documents"}),
            encoding="utf-8",
        )

    def _make_package(self, root: Path) -> Path:
        package = root / "update.zip"
        stage = root / "package_stage"
        payload = stage / "payload"
        (payload / "_internal").mkdir(parents=True)
        (payload / "DocumentRegistry.exe").write_bytes(b"new-exe")
        (payload / "_internal" / "new.txt").write_text("new", encoding="utf-8")
        (payload / "updater").mkdir(parents=True)
        (payload / "updater" / "DocumentRegistryUpdater.exe").write_bytes(b"new-updater")
        (payload / "version.json").write_text(
            json.dumps(
                {
                    "version": "1.18.2",
                    "database_schema": CURRENT_SCHEMA_VERSION,
                }
            ),
            encoding="utf-8",
        )
        (stage / "update_manifest.json").write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "version": "1.18.2",
                    "database_schema": CURRENT_SCHEMA_VERSION,
                }
            ),
            encoding="utf-8",
        )
        import zipfile
        with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in stage.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(stage).as_posix())
        return package

    def _migration_side_effect(self, payload, target, backup_db, target_schema, staging):
        if backup_db is None:
            return None
        candidate = staging / "test_candidate.db"
        import shutil
        shutil.copy2(backup_db, candidate)
        invariants = capture_invariants(candidate)
        migrate_database(candidate, target_schema=target_schema, expected_invariants=invariants)
        return candidate

    def test_failed_commit_restores_old_program_and_old_database(self):
        from unittest.mock import patch
        from app import updater_main

        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "DocumentRegistry"
            root.mkdir()
            self._make_target(root)
            package = self._make_package(Path(name))
            digest = updater_main.sha256_file(package)

            with patch.object(updater_main, "_run_staged_self_check"), patch.object(
                updater_main,
                "_prepare_migrated_database",
                side_effect=self._migration_side_effect,
            ), patch.object(
                updater_main,
                "_run_installed_checks",
                side_effect=updater_main.InstallError("forced failure"),
            ), patch.object(updater_main, "_restart_application"):
                with self.assertRaises(updater_main.InstallError):
                    updater_main.install_update(
                        package,
                        root,
                        digest,
                        "1.18.2",
                        0,
                        CURRENT_SCHEMA_VERSION,
                    )

            self.assertEqual((root / "DocumentRegistry.exe").read_bytes(), b"old-exe")
            self.assertTrue((root / "_internal" / "old.txt").is_file())
            self.assertFalse((root / "_internal" / "new.txt").exists())
            with closing(sqlite3.connect(root / "data" / "requests.db")) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT request_subject FROM requests").fetchone()[0], "Проверка миграции")
            self.assertEqual((root / "data" / "documents" / "old.pdf").read_bytes(), b"%PDF-old")
            backups = list((root / "backups" / "updates").glob("*"))
            self.assertEqual(len(backups), 1)
            with closing(sqlite3.connect(backups[0] / "database" / "requests.db")) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            # Regression: on Windows an unclosed FileHandler locks logs/updater.log
            # and TemporaryDirectory cleanup fails with WinError 32.
            self.assertEqual(updater_main.logger.handlers, [])

    def test_successful_commit_activates_new_database_and_keeps_old_backup(self):
        from unittest.mock import patch
        from app import updater_main

        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "DocumentRegistry"
            root.mkdir()
            self._make_target(root)
            package = self._make_package(Path(name))
            digest = updater_main.sha256_file(package)

            with patch.object(updater_main, "_run_staged_self_check"), patch.object(
                updater_main,
                "_prepare_migrated_database",
                side_effect=self._migration_side_effect,
            ), patch.object(updater_main, "_run_installed_checks"), patch.object(
                updater_main, "_restart_application"
            ), patch("app.updater_main.subprocess.Popen"):
                updater_main.install_update(
                    package,
                    root,
                    digest,
                    "1.18.2",
                    0,
                    CURRENT_SCHEMA_VERSION,
                )

            self.assertEqual((root / "DocumentRegistry.exe").read_bytes(), b"new-exe")
            self.assertTrue((root / "_internal" / "new.txt").is_file())
            self.assertFalse((root / ".update_rollback").exists())
            with closing(sqlite3.connect(root / "data" / "requests.db")) as conn:
                self.assertEqual(
                    conn.execute("PRAGMA user_version").fetchone()[0],
                    CURRENT_SCHEMA_VERSION,
                )
                self.assertEqual(conn.execute("SELECT request_subject FROM requests").fetchone()[0], "Проверка миграции")
            backups = list((root / "backups" / "updates").glob("*"))
            self.assertEqual(len(backups), 1)
            with closing(sqlite3.connect(backups[0] / "database" / "requests.db")) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual((backups[0] / "documents" / "old.pdf").read_bytes(), b"%PDF-old")
            self.assertEqual(updater_main.logger.handlers, [])
