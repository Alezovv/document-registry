from __future__ import annotations

import json
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import zipfile

from app import __version__
from app.db_migrations import (
    CURRENT_SCHEMA_VERSION,
    capture_invariants,
    compare_database_for_rollback,
    downgrade_database,
    get_schema_version,
    migrate_database,
)
from app.update_service import UpdateService, sha256_file


class V1285VersionRollbackTests(unittest.TestCase):
    def test_release_version(self):
        self.assertEqual(__version__, "1.28.5")

    def _create_package(self, path: Path, version: str, schema: int, notes: str) -> str:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "update_manifest.json",
                json.dumps(
                    {
                        "format_version": 1,
                        "version": version,
                        "database_schema": schema,
                        "notes": notes,
                    },
                    ensure_ascii=False,
                ),
            )
            zf.writestr("payload/DocumentRegistry.exe", f"exe-{version}")
            zf.writestr("payload/_internal/core.dll", f"dll-{version}")
            zf.writestr("payload/version.json", json.dumps({"version": version}))
            zf.writestr("payload/updater/DocumentRegistryUpdater.exe", f"updater-{version}")
        return sha256_file(path)

    def test_forward_update_remains_main_mode_and_rollback_lists_older_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir(parents=True, exist_ok=True)
            service = UpdateService(SimpleNamespace(executable_dir=root, data=data))

            channel = root / "channel"
            channel.mkdir()
            pkg_129 = channel / "DocumentRegistry_1.29.0_update.zip"
            pkg_1284 = channel / "DocumentRegistry_1.28.4_update.zip"
            pkg_1270 = channel / "DocumentRegistry_1.27.0_update.zip"

            sha_129 = self._create_package(pkg_129, "1.29.0", 7, "Новое обновление 1.29.0")
            sha_1284 = self._create_package(pkg_1284, "1.28.4", 7, "Стабильная версия 1.28.4")
            sha_1270 = self._create_package(pkg_1270, "1.27.0", 6, "Версия 1.27.0 со схемой БД v6")

            manifest = {
                "format_version": 1,
                "version": "1.29.0",
                "package": pkg_129.name,
                "sha256": sha_129,
                "size": pkg_129.stat().st_size,
                "database_schema": 7,
                "notes": "Новое обновление 1.29.0",
                "versions": [
                    {
                        "version": "1.28.4",
                        "package": pkg_1284.name,
                        "sha256": sha_1284,
                        "size": pkg_1284.stat().st_size,
                        "database_schema": 7,
                        "notes": "Стабильная версия 1.28.4",
                    },
                    {
                        "version": "1.27.0",
                        "package": pkg_1270.name,
                        "sha256": sha_1270,
                        "size": pkg_1270.stat().st_size,
                        "database_schema": 6,
                        "notes": "Версия 1.27.0 со схемой БД v6",
                    },
                ],
            }
            (channel / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            forward = service.check_for_update(channel, "1.28.5")
            self.assertIsNotNone(forward)
            self.assertEqual(forward.version, "1.29.0")

            rollbacks = service.list_rollback_versions(channel, "1.28.5")
            self.assertEqual([item.version for item in rollbacks], ["1.28.4", "1.27.0"])
            self.assertEqual(rollbacks[0].notes, "Стабильная версия 1.28.4")
            self.assertEqual(rollbacks[1].notes, "Версия 1.27.0 со схемой БД v6")

    def test_database_difference_detection_backup_and_downgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir(parents=True, exist_ok=True)
            db_path = data / "requests.db"
            migrate_database(db_path, target_schema=CURRENT_SCHEMA_VERSION)
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "INSERT INTO requests (received_date, request_subject, source_sha256, source_size_bytes) "
                    "VALUES ('2026-10-13', 'Запрос для проверки отката', 'abc123', 512)"
                )
                conn.commit()

            service = UpdateService(SimpleNamespace(executable_dir=root, data=data))
            comp = compare_database_for_rollback(db_path, 6)
            self.assertTrue(comp.has_differences)
            self.assertTrue(comp.schema_differs)
            self.assertIn("source_sha256", comp.extra_request_columns)

            saved = service.save_database_before_rollback(
                current_version="1.28.5",
                target_version="1.27.0",
                target_schema=6,
                comparison=comp,
            )
            self.assertTrue(saved.db_backup_path.is_file())
            self.assertEqual(get_schema_version(saved.db_backup_path), 7)

            invariants = capture_invariants(db_path, max_schema=6)
            report = downgrade_database(db_path, target_schema=6, expected_invariants=invariants)
            self.assertTrue(report.ok)
            self.assertEqual(report.schema_version, 6)
            self.assertEqual(report.requests_count, 1)


if __name__ == "__main__":
    unittest.main()
