import sqlite3
import json
from pathlib import Path
from contextlib import contextmanager
from typing import Any

from app.app_paths import get_app_paths
from app.models import Request
from app.portable_paths import RUNTIME_TOKEN, decode_runtime_path, encode_runtime_path, portable_basename


_PATHS = get_app_paths()
BASE_DIR = _PATHS.runtime
DATA_DIR = _PATHS.data
DB_PATH = _PATHS.database
DOCUMENTS_DIR = _PATHS.documents
_UNSET = object()


class Database:
    """SQLite persistence only.

    Filesystem lifecycle is intentionally handled by DocumentService/RequestService.
    Keeping this class filesystem-free makes transactions predictable and testable.
    """

    SCHEMA_VERSION = 5

    def __init__(self, documents_dir=None, data_dir=None, db_path=None):
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self.base_dir = self.data_dir.parent if data_dir else BASE_DIR
        self.db_path = Path(db_path) if db_path else (
            self.data_dir / "requests.db" if data_dir else DB_PATH
        )
        self.documents_dir = Path(documents_dir) if documents_dir else (
            self.data_dir / "documents" if data_dir else DOCUMENTS_DIR
        )
        # Model-facing paths are canonical by default for backwards-compatible
        # API/tests. Portable startup can switch to preserving the current
        # runtime spelling after it has rebound the database to a moved folder.
        self._preserve_runtime_path_spelling = False

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.create_tables()
        self._upgrade_schema()
        self._configure_database()

    @contextmanager
    def _get_connection(self):
        """Create an isolated SQLite connection and rollback on failure."""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -16384")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def _configure_database(self) -> None:
        """Apply persistent SQLite tuning and create hot-path indexes."""
        with self._get_connection() as conn:
            # WAL keeps readers responsive while a short write transaction is active.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_requests_pinned_id "
                "ON requests(is_pinned DESC, id DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_request_id_id "
                "ON request_history(request_id, id DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_requests_source_document "
                "ON requests(source_document)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_requests_due_date_status "
                "ON requests(due_date, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_requests_department "
                "ON requests(from_department)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_requests_from_person "
                "ON requests(from_person)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_requests_to_person "
                "ON requests(to_person)"
            )
            conn.execute("PRAGMA optimize")

    def create_tables(self):
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_date TEXT NOT NULL,
                    due_date TEXT DEFAULT '',
                    completed_date TEXT DEFAULT '',
                    from_department TEXT,
                    from_position TEXT,
                    from_person TEXT,
                    to_person TEXT,
                    request_subject TEXT,
                    note TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'Новый',
                    source_document TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    is_pinned INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS request_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER,
                    action TEXT NOT NULL,
                    description TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        self._ensure_pinned_column()
        self._fix_status_encoding()

    def _upgrade_schema(self):
        with self._get_connection() as conn:
            cursor = conn.execute("PRAGMA table_info(request_history)")
            columns = [row["name"] for row in cursor.fetchall()]
            if columns and "snapshot" not in columns:
                conn.execute(
                    "ALTER TABLE request_history ADD COLUMN snapshot TEXT"
                )

            request_columns = [
                row["name"] for row in conn.execute("PRAGMA table_info(requests)").fetchall()
            ]
            if request_columns and "due_date" not in request_columns:
                conn.execute("ALTER TABLE requests ADD COLUMN due_date TEXT DEFAULT ''")
            if request_columns and "completed_date" not in request_columns:
                conn.execute("ALTER TABLE requests ADD COLUMN completed_date TEXT DEFAULT ''")

            version = conn.execute("PRAGMA user_version").fetchone()
            current_version = int(version[0] if version is not None else 0)

            if current_version < self.SCHEMA_VERSION:
                self._ensure_history_table(conn)
                conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    def _ensure_history_table(self, conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS request_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER,
                action TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                snapshot TEXT
            )
            """
        )

    def _ensure_pinned_column(self):
        with self._get_connection() as conn:
            columns = [
                row["name"]
                for row in conn.execute("PRAGMA table_info(requests)").fetchall()
            ]
            if "is_pinned" not in columns:
                conn.execute(
                    "ALTER TABLE requests ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0"
                )

    def _fix_status_encoding(self):
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    UPDATE requests
                    SET status = 'Новый'
                    WHERE status IN ('РќРѕРІС‹Р№', 'РќРѕРІС‹Р№')
                    """
                )
        except sqlite3.Error:
            pass

    def encode_runtime_reference(self, value: object) -> str:
        """Serialize a runtime-local filesystem reference without machine ties."""
        return encode_runtime_path(value, self.base_dir)

    def decode_runtime_reference(self, value: object, *, canonical: bool = False) -> str:
        """Decode a runtime token against this process' current portable root."""
        decoded = decode_runtime_path(value, self.base_dir)
        if decoded is None:
            return ""
        if canonical and str(value or "").strip().startswith(RUNTIME_TOKEN):
            try:
                if decoded.exists():
                    return str(decoded.resolve())
            except OSError:
                pass
        return str(decoded)

    def encode_source_document(self, value: object) -> str:
        """Return the portable storage representation for a PDF reference."""
        return self.encode_runtime_reference(value)

    def set_runtime_path_presentation(self, preserve_spelling: bool) -> None:
        """Choose how tokenized paths are exposed to the service/model layer.

        Storage is always portable.  ``preserve_spelling=True`` keeps the exact
        current runtime spelling (important after moving the whole folder); the
        default canonical mode preserves compatibility with callers that compare
        against ``Path.resolve()`` after ordinary create/backup/history flows.
        """
        self._preserve_runtime_path_spelling = bool(preserve_spelling)

    def decode_source_document(self, value: object) -> str:
        """Return a usable PDF reference for the current machine/runtime folder.

        Runtime tokens are expanded against the current portable root. Legacy
        non-token references are deliberately returned verbatim. A missing path
        from an imported old database is evidence that must not be rewritten
        merely because the current OS prefers another slash style.
        """
        raw = str(value or "").strip()
        if not raw:
            return ""
        if not raw.startswith(RUNTIME_TOKEN):
            return raw
        return self.decode_runtime_reference(
            raw, canonical=not self._preserve_runtime_path_spelling
        )

    def _portable_snapshot_data(self, snapshot_data: dict[str, Any]) -> dict[str, Any]:
        result = dict(snapshot_data)
        for field in ("source_document", "_trash_path"):
            value = result.get(field)
            if not value:
                continue
            encoded = self.encode_runtime_reference(value)
            if encoded.startswith(RUNTIME_TOKEN):
                result[field] = encoded
                continue

            # Old backups/history may still contain an absolute path from a
            # different PC.  Rebind only when we can prove the corresponding
            # local file exists; otherwise preserve the legacy reference.
            name = portable_basename(value)
            if not name:
                continue
            if field == "source_document":
                candidate = self.documents_dir / name
                try:
                    if candidate.is_file():
                        result[field] = self.encode_runtime_reference(candidate)
                except OSError:
                    pass
            else:
                trash_root = self.data_dir / "trash"
                try:
                    matches = [path for path in trash_root.rglob(name) if path.is_file()]
                except OSError:
                    matches = []
                if len(matches) == 1:
                    result[field] = self.encode_runtime_reference(matches[0])
        return result

    def has_runtime_source_document_tokens(self) -> bool:
        """Return True when at least one live request already uses portable storage."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM requests WHERE source_document LIKE '@runtime/%' LIMIT 1"
            ).fetchone()
        return row is not None

    def normalize_runtime_source_documents(self) -> int:
        """Rewrite legacy absolute paths inside this runtime as portable tokens."""
        updates: list[tuple[str, int]] = []
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT id, source_document FROM requests "
                "WHERE source_document IS NOT NULL AND source_document != ''"
            ).fetchall()
            for row in rows:
                raw = str(row["source_document"] or "")
                encoded = self.encode_source_document(raw)
                if encoded and encoded != raw and encoded.startswith(RUNTIME_TOKEN):
                    updates.append((encoded, int(row["id"])))
            if updates:
                conn.executemany(
                    "UPDATE requests SET source_document = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    updates,
                )
        return len(updates)

    def normalize_runtime_history_paths(self) -> int:
        """Convert runtime-local paths in existing history snapshots to tokens."""
        updates: list[tuple[str, int]] = []
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT id, snapshot FROM request_history WHERE snapshot IS NOT NULL AND snapshot != ''"
            ).fetchall()
            for row in rows:
                raw_snapshot = row["snapshot"]
                try:
                    snapshot_data = json.loads(raw_snapshot)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(snapshot_data, dict):
                    continue
                portable = self._portable_snapshot_data(snapshot_data)
                if portable != snapshot_data:
                    encoded = json.dumps(portable, ensure_ascii=False)
                    updates.append((encoded, int(row["id"])))
            if updates:
                conn.executemany(
                    "UPDATE request_history SET snapshot = ? WHERE id = ?",
                    updates,
                )
        return len(updates)

    def _row_to_request(self, row) -> Request:
        return Request(
            id=row["id"],
            received_date=(row["received_date"] or ""),
            due_date=(row["due_date"] or "") if "due_date" in row.keys() else "",
            completed_date=(row["completed_date"] or "") if "completed_date" in row.keys() else "",
            from_department=(row["from_department"] or ""),
            from_position=(row["from_position"] or ""),
            from_person=(row["from_person"] or ""),
            to_person=(row["to_person"] or ""),
            request_subject=(row["request_subject"] or ""),
            note=(row["note"] or ""),
            status=(row["status"] or "Новый"),
            source_document=self.decode_source_document(row["source_document"] or ""),
            is_pinned=bool(row["is_pinned"]),
        )

    def get_history(self):
        with self._get_connection() as conn:
            return conn.execute(
                """
                SELECT id, request_id, action, description, created_at, snapshot
                FROM request_history ORDER BY id DESC
                """
            ).fetchall()

    def get_history_count(self) -> int:
        with self._get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM request_history").fetchone()
            return int(row["count"] if row is not None else 0)

    def get_history_record(self, history_id: int):
        with self._get_connection() as conn:
            return conn.execute(
                """
                SELECT id, request_id, action, description, created_at, snapshot
                FROM request_history WHERE id = ?
                """,
                (history_id,),
            ).fetchone()

    def get_request_history(self, request_id: int):
        with self._get_connection() as conn:
            return conn.execute(
                """
                SELECT id, request_id, action, description, created_at, snapshot
                FROM request_history WHERE request_id = ? ORDER BY id DESC
                """,
                (request_id,),
            ).fetchall()

    def add_history(
        self,
        request_id: int,
        action: str,
        description: str,
        snapshot: str | None = None,
    ):
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO request_history (request_id, action, description, snapshot)
                VALUES (?, ?, ?, ?)
                """,
                (request_id, action, description, snapshot),
            )

    def clear_history(self) -> None:
        with self._get_connection() as conn:
            conn.execute("DELETE FROM request_history")

    def get_distinct_request_values(
        self,
        fields=("from_department", "from_position", "from_person", "to_person"),
        limit: int = 200,
    ) -> dict[str, list[str]]:
        """Return recent unique values for form autocomplete.

        Field names are restricted to a fixed allow-list so they can safely be
        interpolated into SQL. Values are ordered by most recently used.
        """
        allowed = {"from_department", "from_position", "from_person", "to_person"}
        requested = [field for field in fields if field in allowed]
        safe_limit = max(1, min(int(limit), 500))
        result: dict[str, list[str]] = {}
        with self._get_connection() as conn:
            for field in requested:
                rows = conn.execute(
                    f"""
                    SELECT {field} AS value
                    FROM requests
                    WHERE TRIM(COALESCE({field}, '')) != ''
                    GROUP BY {field}
                    ORDER BY MAX(id) DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
                result[field] = [str(row["value"]).strip() for row in rows if row["value"]]
        return result

    def get_requests(self):
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM requests
                ORDER BY is_pinned DESC, id DESC
                """
            ).fetchall()
            return [self._row_to_request(row) for row in rows]

    def get_requests_with_documents(self):
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM requests
                WHERE source_document IS NOT NULL AND source_document != ''
                ORDER BY is_pinned DESC, id DESC
                """
            ).fetchall()
            return [self._row_to_request(row) for row in rows]

    def get_request_by_source_document(self, source_document: str):
        encoded = self.encode_source_document(source_document)
        raw = str(source_document or "")
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE source_document IN (?, ?) LIMIT 1",
                (encoded, raw),
            ).fetchone()
            return None if row is None else self._row_to_request(row)

    def repair_source_document_path(self, request_id: int, source_document: str) -> None:
        """Maintenance-only path repair without creating user-visible history."""
        self.repair_source_document_paths([(request_id, source_document)])

    def repair_source_document_paths(self, updates: list[tuple[int, str]]) -> int:
        """Repair relocated PDF references in a single SQLite transaction."""
        if not updates:
            return 0
        with self._get_connection() as conn:
            conn.executemany(
                "UPDATE requests SET source_document = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                [(self.encode_source_document(source_document), request_id) for request_id, source_document in updates],
            )
        return len(updates)

    def get_request(self, request_id: int):
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
            return None if row is None else self._row_to_request(row)

    def add_request(self, request: Request):
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO requests (
                    received_date, due_date, completed_date, from_department, from_position,
                    from_person, to_person, request_subject, note, status,
                    source_document, is_pinned
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.received_date,
                    request.due_date or "",
                    request.completed_date or "",
                    request.from_department,
                    request.from_position,
                    request.from_person,
                    request.to_person,
                    request.request_subject,
                    request.note or "",
                    request.status or "Новый",
                    self.encode_source_document(request.source_document),
                    1 if request.is_pinned else 0,
                ),
            )
            request_id = cursor.lastrowid
            conn.execute(
                """
                INSERT INTO request_history (request_id, action, description, snapshot)
                VALUES (?, 'create', ?, NULL)
                """,
                (request_id, f"Создан запрос (ID: {request_id})"),
            )

        request.id = request_id
        return request_id

    def update_request(
        self,
        request: Request,
        action: str = "update",
        snapshot_extra: dict[str, Any] | None = None,
    ):
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE id = ?", (request.id,)
            ).fetchone()
            if row is None:
                raise ValueError("Запрос не найден.")

            old_values = dict(row)
            new_values = {
                "received_date": request.received_date or "",
                "due_date": request.due_date or "",
                "completed_date": request.completed_date or "",
                "from_department": request.from_department or "",
                "from_position": request.from_position or "",
                "from_person": request.from_person or "",
                "to_person": request.to_person or "",
                "request_subject": request.request_subject or "",
                "note": request.note or "",
                "status": request.status or "Новый",
                "source_document": self.encode_source_document(request.source_document),
            }

            field_names = {
                "received_date": "Дата поступления",
                "due_date": "Срок исполнения",
                "completed_date": "Дата исполнения",
                "from_department": "Отдел",
                "from_position": "Должность",
                "from_person": "ФИО",
                "to_person": "На кого",
                "request_subject": "Суть запроса",
                "note": "Примечание",
                "status": "Статус",
                "source_document": "Документ",
            }

            changes = []
            for field, title in field_names.items():
                old_value = str(old_values.get(field) or "")
                new_value = str(new_values[field])
                if old_value == new_value:
                    continue

                if field == "source_document":
                    old_display = portable_basename(old_value) if old_value else "нет"
                    new_display = portable_basename(new_value) if new_value else "нет"
                else:
                    old_display = old_value or "пусто"
                    new_display = new_value or "пусто"
                changes.append(
                    {"field": title, "old": old_display, "new": new_display}
                )

            conn.execute(
                """
                UPDATE requests
                SET received_date = ?, due_date = ?, completed_date = ?,
                    from_department = ?, from_position = ?, from_person = ?,
                    to_person = ?, request_subject = ?, note = ?,
                    status = ?, source_document = ?, is_pinned = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    request.received_date,
                    request.due_date or "",
                    request.completed_date or "",
                    request.from_department,
                    request.from_position,
                    request.from_person,
                    request.to_person,
                    request.request_subject,
                    request.note or "",
                    request.status or "Новый",
                    new_values["source_document"],
                    1 if request.is_pinned else 0,
                    request.id,
                ),
            )

            if changes:
                snapshot_data = dict(old_values)
                if snapshot_extra:
                    snapshot_data.update(snapshot_extra)
                snapshot_data = self._portable_snapshot_data(snapshot_data)
                conn.execute(
                    """
                    INSERT INTO request_history (request_id, action, description, snapshot)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        request.id,
                        action,
                        "__DIFF__" + json.dumps(changes, ensure_ascii=False),
                        json.dumps(snapshot_data, ensure_ascii=False),
                    ),
                )

    def delete_request(
        self,
        request_id: int,
        snapshot_extra: dict[str, Any] | None = None,
    ) -> bool:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None:
                return False

            old_values = dict(row)
            source_document = old_values.get("source_document") or ""
            sender_parts = [
                old_values.get("from_department"),
                old_values.get("from_person"),
            ]
            sender = " · ".join(
                str(part).strip() for part in sender_parts if part
            ) or "Отправитель не указан"
            target = old_values.get("to_person") or "Фигурант не указан"
            subject_raw = (old_values.get("request_subject") or "").replace(
                "\n", " "
            ).strip()
            subject_short = (
                subject_raw[:62] + "…"
                if len(subject_raw) > 65
                else (subject_raw or "Без описания")
            )
            doc_name = portable_basename(source_document) if source_document else ""
            doc_info = f" [Файл: {doc_name}]" if doc_name else ""
            description = (
                f"Удалён запрос #{request_id}: {sender} → {target} | "
                f"«{subject_short}»{doc_info}"
            )

            snapshot_data = dict(old_values)
            if snapshot_extra:
                snapshot_data.update(snapshot_extra)
            snapshot_data = self._portable_snapshot_data(snapshot_data)

            conn.execute(
                """
                INSERT INTO request_history (request_id, action, description, snapshot)
                VALUES (?, 'delete', ?, ?)
                """,
                (
                    request_id,
                    description,
                    json.dumps(snapshot_data, ensure_ascii=False),
                ),
            )
            conn.execute("DELETE FROM requests WHERE id = ?", (request_id,))
        return True

    def restore_request_from_history(
        self,
        history_id: int,
        source_document_override=_UNSET,
        snapshot_extra: dict[str, Any] | None = None,
    ) -> bool:
        record = self.get_history_record(history_id)
        if record is None or not record["snapshot"]:
            raise ValueError("Не найден снапшот для этой записи истории.")

        snapshot_data = json.loads(record["snapshot"])
        target_id = int(snapshot_data["id"])
        source_document = snapshot_data.get("source_document", "")
        if source_document_override is not _UNSET:
            source_document = source_document_override or ""

        existing = self.get_request(target_id)
        restored_request = Request(
            id=target_id,
            received_date=snapshot_data.get("received_date", ""),
            due_date=snapshot_data.get("due_date", ""),
            completed_date=snapshot_data.get("completed_date", ""),
            from_department=snapshot_data.get("from_department", ""),
            from_position=snapshot_data.get("from_position", ""),
            from_person=snapshot_data.get("from_person", ""),
            to_person=snapshot_data.get("to_person", ""),
            request_subject=snapshot_data.get("request_subject", ""),
            note=snapshot_data.get("note", ""),
            status=snapshot_data.get("status", "Новый"),
            source_document=self.decode_source_document(source_document),
            is_pinned=bool(snapshot_data.get("is_pinned", 0)),
        )

        if existing is None:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO requests (
                        id, received_date, due_date, completed_date, from_department, from_position,
                        from_person, to_person, request_subject, note, status,
                        source_document, is_pinned
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target_id,
                        restored_request.received_date,
                        restored_request.due_date,
                        restored_request.completed_date,
                        restored_request.from_department,
                        restored_request.from_position,
                        restored_request.from_person,
                        restored_request.to_person,
                        restored_request.request_subject,
                        restored_request.note,
                        restored_request.status,
                        self.encode_source_document(restored_request.source_document),
                        1 if restored_request.is_pinned else 0,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO request_history (request_id, action, description, snapshot)
                    VALUES (?, 'restore', ?, ?)
                    """,
                    (
                        target_id,
                        f"Документ восстановлен из истории (ID: {target_id})",
                        json.dumps(snapshot_data, ensure_ascii=False),
                    ),
                )
            return True

        self.update_request(
            restored_request,
            action="restore",
            snapshot_extra=snapshot_extra,
        )
        return True

    def attach_document(self, request_id: int, stored_path: str):
        request = self.get_request(request_id)
        if request is None:
            raise ValueError("Запрос не найден.")
        request.source_document = stored_path or ""
        self.update_request(request, action="attach")
        return request.source_document

    def detach_document(self, request_id: int):
        request = self.get_request(request_id)
        if request is None:
            return False
        request.source_document = ""
        self.update_request(request, action="detach")
        return True

    def bulk_update_requests(
        self,
        requests: list[Request],
        action: str = "bulk_update",
    ) -> int:
        """Update multiple request rows in one SQLite transaction.

        Each changed row still receives its own history snapshot, so bulk
        operations remain individually reversible from the History page.
        """
        items = [request for request in requests if request.id is not None]
        if not items:
            return 0

        field_names = {
            "received_date": "Дата поступления",
            "due_date": "Срок исполнения",
            "completed_date": "Дата исполнения",
            "from_department": "Отдел",
            "from_position": "Должность",
            "from_person": "ФИО",
            "to_person": "На кого",
            "request_subject": "Суть запроса",
            "note": "Примечание",
            "status": "Статус",
            "source_document": "Документ",
            "is_pinned": "Закрепление",
        }

        updated = 0
        with self._get_connection() as conn:
            for request in items:
                row = conn.execute(
                    "SELECT * FROM requests WHERE id = ?", (request.id,)
                ).fetchone()
                if row is None:
                    continue

                old_values = dict(row)
                new_values = {
                    "received_date": request.received_date or "",
                    "due_date": request.due_date or "",
                    "completed_date": request.completed_date or "",
                    "from_department": request.from_department or "",
                    "from_position": request.from_position or "",
                    "from_person": request.from_person or "",
                    "to_person": request.to_person or "",
                    "request_subject": request.request_subject or "",
                    "note": request.note or "",
                    "status": request.status or "Новый",
                    "source_document": self.encode_source_document(request.source_document),
                    "is_pinned": 1 if request.is_pinned else 0,
                }

                changes = []
                for field, title in field_names.items():
                    old_value = old_values.get(field)
                    new_value = new_values[field]
                    if field == "is_pinned":
                        old_norm = bool(old_value)
                        new_norm = bool(new_value)
                        if old_norm == new_norm:
                            continue
                        old_display = "Да" if old_norm else "Нет"
                        new_display = "Да" if new_norm else "Нет"
                    else:
                        old_norm = str(old_value or "")
                        new_norm = str(new_value or "")
                        if old_norm == new_norm:
                            continue
                        if field == "source_document":
                            old_display = portable_basename(old_norm) if old_norm else "нет"
                            new_display = portable_basename(new_norm) if new_norm else "нет"
                        else:
                            old_display = old_norm or "пусто"
                            new_display = new_norm or "пусто"
                    changes.append({"field": title, "old": old_display, "new": new_display})

                if not changes:
                    continue

                conn.execute(
                    """
                    UPDATE requests
                    SET received_date = ?, due_date = ?, completed_date = ?,
                        from_department = ?, from_position = ?, from_person = ?,
                        to_person = ?, request_subject = ?, note = ?,
                        status = ?, source_document = ?, is_pinned = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        new_values["received_date"],
                        new_values["due_date"],
                        new_values["completed_date"],
                        new_values["from_department"],
                        new_values["from_position"],
                        new_values["from_person"],
                        new_values["to_person"],
                        new_values["request_subject"],
                        new_values["note"],
                        new_values["status"],
                        new_values["source_document"],
                        new_values["is_pinned"],
                        request.id,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO request_history (request_id, action, description, snapshot)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        request.id,
                        action,
                        "__DIFF__" + json.dumps(changes, ensure_ascii=False),
                        json.dumps(self._portable_snapshot_data(old_values), ensure_ascii=False),
                    ),
                )
                updated += 1
        return updated

    def set_request_pinned(self, request_id: int, pinned: bool):
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE requests
                SET is_pinned = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (1 if pinned else 0, request_id),
            )

    def toggle_request_pinned(
        self,
        request_id: int,
        record_history: bool = False,
    ) -> bool:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT is_pinned FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None:
                return False

            new_value = not bool(row["is_pinned"])
            conn.execute(
                """
                UPDATE requests
                SET is_pinned = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (1 if new_value else 0, request_id),
            )
            if record_history:
                action = "pin" if new_value else "unpin"
                description = (
                    f"Запрос (ID: {request_id}) "
                    + ("закреплён" if new_value else "откреплён")
                )
                conn.execute(
                    """
                    INSERT INTO request_history (request_id, action, description, snapshot)
                    VALUES (?, ?, ?, NULL)
                    """,
                    (request_id, action, description),
                )
            return new_value

    def is_request_pinned(self, request_id: int) -> bool:
        request = self.get_request(request_id)
        return bool(request and request.is_pinned)

    def search_requests(self, field: str, value: str):
        allowed_fields = {
            "id",
            "received_date",
            "due_date",
            "completed_date",
            "from_department",
            "from_position",
            "from_person",
            "to_person",
            "request_subject",
            "status",
            "all",
        }
        if field not in allowed_fields:
            raise ValueError(f"Недопустимое поле поиска: {field}")

        value = value.strip()
        if not value:
            return self.get_requests()

        with self._get_connection() as conn:
            if field == "all":
                query = """
                    SELECT * FROM requests
                    WHERE CAST(id AS TEXT) LIKE ? OR received_date LIKE ?
                        OR due_date LIKE ? OR completed_date LIKE ?
                        OR from_department LIKE ? OR from_position LIKE ?
                        OR from_person LIKE ? OR to_person LIKE ?
                        OR request_subject LIKE ? OR note LIKE ? OR status LIKE ?
                    ORDER BY is_pinned DESC, id DESC
                """
                search_value = f"%{value}%"
                params = (search_value,) * 11
            else:
                query = (
                    f"SELECT * FROM requests WHERE CAST({field} AS TEXT) LIKE ? "
                    "ORDER BY is_pinned DESC, id DESC"
                )
                params = (f"%{value}%",)

            rows = conn.execute(query, params).fetchall()
            return [self._row_to_request(row) for row in rows]
