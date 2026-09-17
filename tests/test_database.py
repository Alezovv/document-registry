import tempfile
import unittest
from pathlib import Path

from app.database import Database
from app.models import Request


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.db = Database(data_dir=root / "data")

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_request(self, **overrides):
        values = {
            "received_date": "16.09.2026",
            "from_department": "Отдел кадров",
            "from_position": "Начальник",
            "from_person": "Иванов Иван",
            "to_person": "Петров Петр",
            "request_subject": "Тестовый запрос",
            "note": "",
            "status": "Новый",
        }
        values.update(overrides)
        return Request(**values)

    def test_schema_is_upgraded_after_initialization(self):
        with self.db._get_connection() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            history_columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(request_history)"
                ).fetchall()
            }
            request_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(requests)").fetchall()
            }

        self.assertEqual(version, self.db.SCHEMA_VERSION)
        self.assertIn("snapshot", history_columns)
        self.assertIn("due_date", request_columns)
        self.assertIn("completed_date", request_columns)


    def test_database_uses_wal_and_hot_path_indexes(self):
        with self.db._get_connection() as conn:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            request_indexes = {
                row["name"] for row in conn.execute("PRAGMA index_list(requests)").fetchall()
            }
            history_indexes = {
                row["name"] for row in conn.execute("PRAGMA index_list(request_history)").fetchall()
            }

        self.assertEqual(str(journal_mode).lower(), "wal")
        self.assertIn("idx_requests_pinned_id", request_indexes)
        self.assertIn("idx_requests_source_document", request_indexes)
        self.assertIn("idx_requests_due_date_status", request_indexes)
        self.assertIn("idx_history_request_id_id", history_indexes)

    def test_history_count_does_not_require_loading_history_rows(self):
        request_id = self.db.add_request(self.make_request())
        self.db.toggle_request_pinned(request_id, record_history=True)
        self.assertEqual(self.db.get_history_count(), 2)

    def test_connection_rolls_back_on_exception(self):
        with self.assertRaises(RuntimeError):
            with self.db._get_connection() as conn:
                conn.execute(
                    "INSERT INTO requests (received_date, status) VALUES (?, ?)",
                    ("16.09.2026", "Новый"),
                )
                raise RuntimeError("force rollback")

        with self.db._get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]

        self.assertEqual(count, 0)

    def test_database_persists_document_path_without_touching_filesystem(self):
        missing = Path(self.temp_dir.name) / "not-created.pdf"
        request = self.make_request(source_document=str(missing))
        request_id = self.db.add_request(request)

        stored = self.db.get_request(request_id)
        self.assertEqual(stored.source_document, str(missing))
        self.assertFalse(missing.exists())

    def test_create_update_delete_and_restore_without_document(self):
        request = self.make_request()
        request_id = self.db.add_request(request)

        created = self.db.get_request(request_id)
        self.assertEqual(created.request_subject, "Тестовый запрос")

        created.request_subject = "Измененный запрос"
        created.status = "В работе"
        self.db.update_request(created)

        updated = self.db.get_request(request_id)
        self.assertEqual(updated.request_subject, "Измененный запрос")
        self.assertEqual(updated.status, "В работе")

        self.assertTrue(self.db.delete_request(request_id))
        self.assertIsNone(self.db.get_request(request_id))

        delete_record = next(
            row for row in self.db.get_history()
            if row["request_id"] == request_id and row["action"] == "delete"
        )
        self.assertTrue(self.db.restore_request_from_history(delete_record["id"]))

        restored = self.db.get_request(request_id)
        self.assertEqual(restored.request_subject, "Измененный запрос")
        self.assertEqual(restored.status, "В работе")

    def test_toggle_pin_can_write_history_atomically(self):
        request_id = self.db.add_request(self.make_request())

        self.assertTrue(self.db.toggle_request_pinned(request_id, record_history=True))
        self.assertTrue(self.db.is_request_pinned(request_id))

        pin_records = [
            row for row in self.db.get_history()
            if row["request_id"] == request_id and row["action"] == "pin"
        ]
        self.assertEqual(len(pin_records), 1)

    def test_existing_v4_database_gets_deadline_columns(self):
        import sqlite3
        legacy_root = Path(self.temp_dir.name) / "legacy"
        legacy_root.mkdir(parents=True, exist_ok=True)
        legacy_db = legacy_root / "requests.db"
        conn = sqlite3.connect(legacy_db)
        try:
            conn.execute(
                "CREATE TABLE requests ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, received_date TEXT NOT NULL, "
                "from_department TEXT, from_position TEXT, from_person TEXT, to_person TEXT, "
                "request_subject TEXT, note TEXT DEFAULT '', status TEXT DEFAULT 'Новый', "
                "source_document TEXT, created_at TEXT, updated_at TEXT, is_pinned INTEGER DEFAULT 0)"
            )
            conn.execute(
                "CREATE TABLE request_history ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, action TEXT, "
                "description TEXT, created_at TEXT, snapshot TEXT)"
            )
            conn.execute("PRAGMA user_version = 4")
            conn.commit()
        finally:
            conn.close()

        migrated = Database(data_dir=legacy_root)
        with migrated._get_connection() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(requests)")}
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertIn("due_date", columns)
        self.assertIn("completed_date", columns)
        self.assertEqual(version, migrated.SCHEMA_VERSION)



    def test_distinct_request_values_are_recent_unique_and_trimmed(self):
        first = self.make_request(
            from_department="Отдел кадров",
            from_position="Специалист",
            from_person="Иванов Иван",
            to_person="Петров Петр",
        )
        second = self.make_request(
            from_department="Юридический отдел",
            from_position="Юрист",
            from_person="Сидоров Сергей",
            to_person="Иванов Иван",
        )
        third = self.make_request(
            from_department="Отдел кадров",
            from_position="",
            from_person="Иванов Иван",
            to_person="",
        )
        self.db.add_request(first)
        self.db.add_request(second)
        self.db.add_request(third)

        values = self.db.get_distinct_request_values(limit=10)

        self.assertEqual(values["from_department"][0], "Отдел кадров")
        self.assertEqual(set(values["from_department"]), {"Отдел кадров", "Юридический отдел"})
        self.assertNotIn("", values["from_position"])
        self.assertEqual(values["from_person"].count("Иванов Иван"), 1)



if __name__ == "__main__":
    unittest.main()
