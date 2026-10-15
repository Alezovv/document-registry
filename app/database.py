import sqlite3
import json
import re
from pathlib import Path
from contextlib import contextmanager
from typing import Any

from app.app_paths import get_app_paths
from app.db_migrations import CURRENT_SCHEMA_VERSION, migrate_database
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

    SCHEMA_VERSION = CURRENT_SCHEMA_VERSION

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
        conn.create_function("CASEFOLD", 1, lambda value: str(value or "").casefold(), deterministic=True)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = FILE")
        # Keep SQLite cache bounded for long desktop sessions; OCR is the heavy
        # component and should get the available memory instead of per-query temp pages.
        conn.execute("PRAGMA cache_size = -8192")
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
                "CREATE INDEX IF NOT EXISTS idx_requests_source_fingerprint "
                "ON requests(source_sha256, source_size_bytes)"
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
                    is_pinned INTEGER NOT NULL DEFAULT 0,
                    source_sha256 TEXT NOT NULL DEFAULT '',
                    source_size_bytes INTEGER NOT NULL DEFAULT 0
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
        """Upgrade the SQLite schema through the versioned migration framework."""
        migrate_database(self.db_path, target_schema=self.SCHEMA_VERSION)

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
                    WHERE status IN ('РќРѕРІС‹Р№', 'Р СњР С•Р Р†РЎвЂ№Р в„–', 'Р СњР С•Р Р†РЎвЂ№Р в„–')
                    """
                )
                conn.execute(
                    """
                    UPDATE requests
                    SET status = 'В работе'
                    WHERE status IN ('Р’ СЂР°Р±РѕС‚Рµ', 'Р вЂ™ РЎР‚Р В°Р В±Р С•РЎвЂљР Вµ')
                    """
                )
                conn.execute(
                    """
                    UPDATE requests
                    SET status = 'Исполнен'
                    WHERE status IN ('РСЃРїРѕР»РЅРµРЅ', 'Р�СЃРїРѕР»РЅРµРЅ', 'Р пїЅРЎРѓР С—Р С•Р В»Р Р…Р ВµР Р…')
                    """
                )
                conn.execute(
                    """
                    UPDATE requests
                    SET status = 'Отклонен'
                    WHERE status IN ('РћС‚РєР»РѕРЅРµРЅ', 'Р С›РЎвЂљР С”Р В»Р С•Р Р…Р ВµР Р…', 'Отклонён')
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
            source_sha256=(row["source_sha256"] or "") if "source_sha256" in row.keys() else "",
            source_size_bytes=int(row["source_size_bytes"] or 0) if "source_size_bytes" in row.keys() else 0,
        )

    def get_history(self, limit: int = 2000, offset: int = 0, *, include_snapshot: bool = True):
        snapshot_col = "snapshot" if include_snapshot else "NULL AS snapshot"
        safe_limit = max(1, min(int(limit), 10000))
        safe_offset = max(0, int(offset))
        with self._get_connection() as conn:
            return conn.execute(
                f"""
                SELECT id, request_id, action, description, created_at, {snapshot_col}
                FROM request_history ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                (safe_limit, safe_offset),
            ).fetchall()

    def get_history_count(self) -> int:
        with self._get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM request_history").fetchone()
            return int(row["count"] if row is not None else 0)

    def trim_history(self, keep_count: int = 2000) -> int:
        """Keep only the latest keep_count records in request_history, trimming older ones."""
        safe_keep = max(1, int(keep_count))
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM request_history
                WHERE id NOT IN (
                    SELECT id FROM request_history ORDER BY id DESC LIMIT ?
                )
                """,
                (safe_keep,),
            )
            return cursor.rowcount

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
            # Automatic rotation: when count exceeds 2100, trim back to 2000
            row = conn.execute("SELECT COUNT(*) AS count FROM request_history").fetchone()
            if row and int(row["count"] or 0) > 2100:
                conn.execute(
                    """
                    DELETE FROM request_history
                    WHERE id NOT IN (
                        SELECT id FROM request_history ORDER BY id DESC LIMIT 2000
                    )
                    """
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

    @staticmethod
    def _sql_date_expr(column: str) -> str:
        return (
            f"CASE WHEN LENGTH(TRIM(COALESCE({column}, ''))) = 10 "
            f"THEN substr({column}, 7, 4) || '-' || substr({column}, 4, 2) || '-' || substr({column}, 1, 2) "
            "ELSE '' END"
        )

    def get_registry_status_counts(self) -> dict[str, int]:
        due_expr = self._sql_date_expr("due_date")
        active_overdue = f"(status NOT IN ('Исполнен', 'Отклонен') AND {due_expr} != '' AND {due_expr} < date('now','localtime'))"
        with self._get_connection() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'Новый' AND NOT {active_overdue} THEN 1 ELSE 0 END) AS new_count,
                    SUM(CASE WHEN status = 'В работе' AND NOT {active_overdue} THEN 1 ELSE 0 END) AS progress_count,
                    SUM(CASE WHEN status = 'Исполнен' THEN 1 ELSE 0 END) AS done_count,
                    SUM(CASE WHEN status = 'Отклонен' THEN 1 ELSE 0 END) AS rejected_count,
                    SUM(CASE WHEN {active_overdue} THEN 1 ELSE 0 END) AS overdue_count
                FROM requests
                """
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "Новый": int(row["new_count"] or 0),
            "В работе": int(row["progress_count"] or 0),
            "Исполнен": int(row["done_count"] or 0),
            "Отклонен": int(row["rejected_count"] or 0),
            "Просрочен": int(row["overdue_count"] or 0),
        }

    def get_deadline_summary(self) -> tuple[int, int]:
        due_expr = self._sql_date_expr("due_date")
        with self._get_connection() as conn:
            row = conn.execute(
                f"""
                SELECT
                    SUM(CASE WHEN status NOT IN ('Исполнен', 'Отклонен') AND {due_expr} != '' AND {due_expr} < date('now','localtime') THEN 1 ELSE 0 END) AS overdue,
                    SUM(CASE WHEN status NOT IN ('Исполнен', 'Отклонен') AND {due_expr} = date('now','localtime') THEN 1 ELSE 0 END) AS due_today
                FROM requests
                """
            ).fetchone()
        return int(row["overdue"] or 0), int(row["due_today"] or 0)

    def get_requests_missing_ocr_text_page(self, *, after_id: int = 0, limit: int = 25) -> list[Request]:
        safe_limit = max(1, min(int(limit), 100))
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT r.*
                FROM requests r
                LEFT JOIN request_search_content s ON s.request_id = r.id
                WHERE r.id > ?
                  AND TRIM(COALESCE(r.source_document, '')) != ''
                  AND TRIM(COALESCE(s.ocr_text, '')) = ''
                ORDER BY r.id ASC
                LIMIT ?
                """,
                (max(0, int(after_id)), safe_limit),
            ).fetchall()
        return [self._row_to_request(row) for row in rows]

    def count_requests_missing_fingerprint(self) -> int:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM requests WHERE TRIM(COALESCE(source_document,'')) != '' AND TRIM(COALESCE(source_sha256,'')) = ''"
            ).fetchone()
        return int(row[0] or 0)

    def query_requests_page(
        self,
        *,
        search_text: str = "",
        search_field: str = "Все поля",
        sender: str = "",
        recipient: str = "",
        status: str = "Все статусы",
        deadline: str = "Любой",
        date_from_iso: str = "",
        date_to_iso: str = "",
        sort_column: int | None = None,
        sort_ascending: bool = True,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[Request], int]:
        """Return one registry page while SQLite holds filtering/sorting state."""
        safe_limit = max(20, min(int(limit), 1000))
        safe_offset = max(0, int(offset))
        where: list[str] = []
        params: list[Any] = []
        due_expr = self._sql_date_expr("r.due_date")
        recv_expr = self._sql_date_expr("r.received_date")

        dup_subquery = (
            "(SELECT r_dup.id FROM requests r_dup "
            "JOIN (SELECT source_sha256, MIN(id) AS min_id FROM requests "
            "WHERE TRIM(COALESCE(source_sha256, '')) != '' "
            "GROUP BY source_sha256 HAVING COUNT(*) > 1) g_dup "
            "ON r_dup.source_sha256 = g_dup.source_sha256 "
            "WHERE r_dup.id != g_dup.min_id)"
        )

        if status == "Активные":
            where.append("r.status NOT IN ('Исполнен', 'Отклонен')")
        elif status == "Просрочен":
            where.append(f"r.status NOT IN ('Исполнен', 'Отклонен') AND {due_expr} != '' AND {due_expr} < date('now','localtime')")
        elif status in ("Новый", "В работе"):
            where.append(f"r.status = ? AND NOT ({due_expr} != '' AND {due_expr} < date('now','localtime'))")
            params.append(status)
        elif status in ("Дубликат", "Дубликаты"):
            where.append(f"r.id IN {dup_subquery}")
        elif status and status != "Все статусы":
            where.append("r.status = ?")
            params.append(status)

        if date_from_iso:
            where.append(f"{recv_expr} >= ?")
            params.append(date_from_iso)
        if date_to_iso:
            where.append(f"{recv_expr} <= ?")
            params.append(date_to_iso)

        if deadline == "Сегодня":
            where.append(f"r.status NOT IN ('Исполнен', 'Отклонен') AND {due_expr} = date('now','localtime')")
        elif deadline == "Ближайшие 7 дней":
            where.append(f"r.status NOT IN ('Исполнен', 'Отклонен') AND {due_expr} BETWEEN date('now','localtime') AND date('now','localtime','+6 day')")
        elif deadline == "Без срока":
            where.append("TRIM(COALESCE(r.due_date, '')) = ''")
        elif deadline == "Просрочен":
            where.append(f"r.status NOT IN ('Исполнен', 'Отклонен') AND {due_expr} != '' AND {due_expr} < date('now','localtime')")

        if sender.strip():
            needle_text = f"%{sender.strip().casefold()}%"
            where.append("(CASEFOLD(r.from_department) LIKE ? OR CASEFOLD(r.from_position) LIKE ? OR CASEFOLD(r.from_person) LIKE ?)")
            params.extend([needle_text] * 3)
        if recipient.strip():
            where.append("CASEFOLD(r.to_person) LIKE ?")
            params.append(f"%{recipient.strip().casefold()}%")

        text = search_text.strip()
        if text:
            like = f"%{text.casefold()}%"
            field_map = {
                "Дата поступления": "r.received_date",
                "Срок исполнения": "r.due_date",
                "Отдел": "r.from_department",
                "Должность": "r.from_position",
                "От кого": "r.from_person",
                "На кого": "r.to_person",
                "Суть запроса": "r.request_subject",
                "Статус": "r.status",
            }
            is_dup_search = any(term in text.casefold() for term in ("дубликат", "дубликаты", "дубль", "дубли", "копи"))
            dup_or = f" OR r.id IN {dup_subquery}" if is_dup_search else ""
            if search_field == "Все поля":
                where.append(
                    "(CAST(r.id AS TEXT) LIKE ? OR CASEFOLD(r.received_date) LIKE ? OR CASEFOLD(r.due_date) LIKE ? "
                    "OR CASEFOLD(r.completed_date) LIKE ? OR CASEFOLD(r.from_department) LIKE ? "
                    "OR CASEFOLD(r.from_position) LIKE ? OR CASEFOLD(r.from_person) LIKE ? "
                    "OR CASEFOLD(r.to_person) LIKE ? OR CASEFOLD(r.request_subject) LIKE ? "
                    f"OR CASEFOLD(r.note) LIKE ? OR CASEFOLD(r.status) LIKE ? OR CASEFOLD(COALESCE(s.ocr_text,'')) LIKE ?{dup_or})"
                )
                params.extend([like] * 12)
            elif search_field == "Статус":
                where.append(f"(CASEFOLD(r.status) LIKE ?{dup_or})")
                params.append(like)
            elif search_field in field_map:
                where.append(f"CASEFOLD({field_map[search_field]}) LIKE ?")
                params.append(like)

        where_sql = " WHERE " + " AND ".join(where) if where else ""
        sort_map = {
            2: recv_expr,
            3: due_expr,
            4: "CASEFOLD(r.from_department)",
            5: "CASEFOLD(r.to_person)",
            6: "CASEFOLD(r.request_subject)",
            7: "CASEFOLD(r.note)",
            8: "CASEFOLD(r.status)",
        }
        secondary = sort_map.get(sort_column)
        direction = "ASC" if sort_ascending else "DESC"
        if secondary:
            order_sql = f" ORDER BY r.is_pinned DESC, {secondary} {direction}, r.id DESC"
        else:
            order_sql = " ORDER BY r.is_pinned DESC, r.id DESC"

        base = " FROM requests r LEFT JOIN request_search_content s ON s.request_id = r.id"
        with self._get_connection() as conn:
            total = int(conn.execute("SELECT COUNT(*)" + base + where_sql, params).fetchone()[0])
            rows = conn.execute(
                "SELECT r.*" + base + where_sql + order_sql + " LIMIT ? OFFSET ?",
                params + [safe_limit, safe_offset],
            ).fetchall()
        return [self._row_to_request(row) for row in rows], total

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
                    source_document, is_pinned, source_sha256, source_size_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    str(request.source_sha256 or ""),
                    int(request.source_size_bytes or 0),
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
                "source_sha256": str(request.source_sha256 or ""),
                "source_size_bytes": int(request.source_size_bytes or 0),
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
                    source_sha256 = ?, source_size_bytes = ?,
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
                    new_values["source_sha256"],
                    new_values["source_size_bytes"],
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
            source_sha256=str(snapshot_data.get("source_sha256", "") or ""),
            source_size_bytes=int(snapshot_data.get("source_size_bytes", 0) or 0),
        )

        if existing is None:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO requests (
                        id, received_date, due_date, completed_date, from_department, from_position,
                        from_person, to_person, request_subject, note, status,
                        source_document, is_pinned, source_sha256, source_size_bytes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        restored_request.source_sha256,
                        restored_request.source_size_bytes,
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

    @staticmethod
    def _fts_query(value: str) -> str:
        tokens = [token for token in re.findall(r"[\w]+", str(value or ""), flags=re.UNICODE) if token]
        if not tokens:
            return ""
        # Prefix matching is friendlier for names/organisations while quoting
        # every token keeps FTS operators from being interpreted as user input.
        return " AND ".join('"' + token.replace('"', '""') + '"*' for token in tokens[:12])

    def set_request_ocr_text(self, request_id: int, ocr_text: str) -> None:
        text = str(ocr_text or "").strip()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO request_search_content(request_id, ocr_text, indexed_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(request_id) DO UPDATE SET
                    ocr_text = excluded.ocr_text,
                    indexed_at = CURRENT_TIMESTAMP
                """,
                (int(request_id), text),
            )

    def get_request_ocr_text(self, request_id: int) -> str:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT ocr_text FROM request_search_content WHERE request_id = ?",
                (int(request_id),),
            ).fetchone()
        return str(row["ocr_text"] or "") if row is not None else ""

    def get_search_index_stats(self) -> dict[str, int]:
        with self._get_connection() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
            indexed = int(conn.execute("SELECT COUNT(*) FROM request_fts").fetchone()[0])
            with_ocr = int(
                conn.execute(
                    "SELECT COUNT(*) FROM request_search_content WHERE TRIM(ocr_text) != ''"
                ).fetchone()[0]
            )
        return {"requests": total, "indexed": indexed, "with_ocr": with_ocr}

    def search_request_ids_full_text(self, value: str, *, limit: int = 5000) -> list[int]:
        text = str(value or "").strip()
        if not text:
            return [request.id for request in self.get_requests() if request.id is not None]
        safe_limit = max(1, min(int(limit), 10000))
        query = self._fts_query(text)
        is_dup_search = any(term in text.casefold() for term in ("дубликат", "дубликаты", "дубль", "дубли", "копи"))
        dup_subquery = (
            "SELECT r_dup.id FROM requests r_dup "
            "JOIN (SELECT source_sha256, MIN(id) AS min_id FROM requests "
            "WHERE TRIM(COALESCE(source_sha256, '')) != '' "
            "GROUP BY source_sha256 HAVING COUNT(*) > 1) g_dup "
            "ON r_dup.source_sha256 = g_dup.source_sha256 "
            "WHERE r_dup.id != g_dup.min_id"
        )
        with self._get_connection() as conn:
            dup_ids: list[int] = []
            if is_dup_search:
                dup_rows = conn.execute(dup_subquery).fetchall()
                dup_ids = [int(r["id"]) for r in dup_rows]

            if query:
                try:
                    rows = conn.execute(
                        """
                        SELECT CAST(request_id AS INTEGER) AS request_id
                        FROM request_fts
                        WHERE request_fts MATCH ?
                        ORDER BY bm25(request_fts), rowid DESC
                        LIMIT ?
                        """,
                        (query, safe_limit),
                    ).fetchall()
                    res_ids = [int(row["request_id"]) for row in rows]
                    if dup_ids:
                        seen = set(res_ids)
                        for did in dup_ids:
                            if did not in seen:
                                res_ids.append(did)
                                seen.add(did)
                    return res_ids
                except sqlite3.OperationalError:
                    # Keep search usable on an unusual SQLite build even if FTS
                    # parsing rejects a query. The ordinary table also contains
                    # OCR text, so the fallback remains functionally complete.
                    pass

            like_value = f"%{text}%"
            dup_or = ""
            if dup_ids:
                dup_or = " OR r.id IN (" + ",".join(str(d) for d in dup_ids) + ")"
            rows = conn.execute(
                f"""
                SELECT r.id AS request_id
                FROM requests r
                LEFT JOIN request_search_content s ON s.request_id = r.id
                WHERE CAST(r.id AS TEXT) LIKE ?
                    OR r.received_date LIKE ? OR r.due_date LIKE ? OR r.completed_date LIKE ?
                    OR r.from_department LIKE ? OR r.from_position LIKE ?
                    OR r.from_person LIKE ? OR r.to_person LIKE ?
                    OR r.request_subject LIKE ? OR r.note LIKE ? OR r.status LIKE ?
                    OR s.ocr_text LIKE ?
                    {dup_or}
                ORDER BY r.is_pinned DESC, r.id DESC
                LIMIT ?
                """,
                (like_value,) * 12 + (safe_limit,),
            ).fetchall()
            return [int(row["request_id"]) for row in rows]

    def set_document_fingerprint(self, request_id: int, sha256: str, size_bytes: int) -> None:
        digest = str(sha256 or "").strip().lower()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE requests SET source_sha256 = ?, source_size_bytes = ? WHERE id = ?",
                (digest, max(0, int(size_bytes or 0)), int(request_id)),
            )

    def find_request_by_fingerprint(
        self,
        sha256: str,
        size_bytes: int = 0,
        *,
        exclude_request_id: int | None = None,
    ) -> Request | None:
        digest = str(sha256 or "").strip().lower()
        if not digest:
            return None
        params: list[Any] = [digest]
        where = "source_sha256 = ?"
        if int(size_bytes or 0) > 0:
            where += " AND source_size_bytes = ?"
            params.append(int(size_bytes))
        if exclude_request_id is not None:
            where += " AND id != ?"
            params.append(int(exclude_request_id))
        with self._get_connection() as conn:
            row = conn.execute(
                f"SELECT * FROM requests WHERE {where} ORDER BY id DESC LIMIT 1",
                tuple(params),
            ).fetchone()
        return None if row is None else self._row_to_request(row)

    def get_requests_missing_fingerprint(self, *, limit: int | None = None) -> list[Request]:
        sql = (
            "SELECT * FROM requests WHERE source_document IS NOT NULL AND source_document != '' "
            "AND TRIM(COALESCE(source_sha256, '')) = '' ORDER BY id DESC"
        )
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (max(1, int(limit)),)
        with self._get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_request(row) for row in rows]

    def get_duplicate_fingerprint_groups(self) -> list[tuple[str, int, list[int]]]:
        with self._get_connection() as conn:
            groups = conn.execute(
                """
                SELECT source_sha256, source_size_bytes, COUNT(*) AS count
                FROM requests
                WHERE TRIM(COALESCE(source_sha256, '')) != ''
                GROUP BY source_sha256, source_size_bytes
                HAVING COUNT(*) > 1
                ORDER BY count DESC, source_sha256
                """
            ).fetchall()
            result: list[tuple[str, int, list[int]]] = []
            for group in groups:
                rows = conn.execute(
                    "SELECT id FROM requests WHERE source_sha256 = ? AND source_size_bytes = ? ORDER BY id",
                    (group["source_sha256"], group["source_size_bytes"]),
                ).fetchall()
                result.append((
                    str(group["source_sha256"]),
                    int(group["source_size_bytes"] or 0),
                    [int(row["id"]) for row in rows],
                ))
        return result

    def get_duplicate_map(self) -> dict[int, dict[str, Any]]:
        """Return a mapping of request_id -> duplicate info for all duplicate requests."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT r.id AS req_id, orig.id AS orig_id, orig.received_date AS orig_date,
                       orig.from_department AS orig_dept, orig.request_subject AS orig_subj,
                       g.total_count
                FROM requests r
                JOIN (
                    SELECT source_sha256, MIN(id) AS first_id, COUNT(*) AS total_count
                    FROM requests
                    WHERE TRIM(COALESCE(source_sha256, '')) != ''
                    GROUP BY source_sha256
                    HAVING COUNT(*) > 1
                ) g ON r.source_sha256 = g.source_sha256
                JOIN requests orig ON orig.id = g.first_id
                WHERE r.id != g.first_id
                """
            ).fetchall()
            return {
                int(row["req_id"]): {
                    "is_duplicate": True,
                    "original_id": int(row["orig_id"]),
                    "original_date": str(row["orig_date"] or ""),
                    "original_department": str(row["orig_dept"] or ""),
                    "original_subject": str(row["orig_subj"] or ""),
                    "total_count": int(row["total_count"]),
                }
                for row in rows
            }

    def get_request_duplicate_info(self, request_id: int) -> dict[str, Any] | None:
        """Return duplicate info for a single request, including original details if duplicate."""
        if not request_id:
            return None
        with self._get_connection() as conn:
            curr = conn.execute(
                "SELECT source_sha256, source_size_bytes FROM requests WHERE id = ?",
                (int(request_id),),
            ).fetchone()
            if not curr or not curr["source_sha256"]:
                return None
            sha256 = curr["source_sha256"]
            rows = conn.execute(
                """
                SELECT id, received_date, from_department, from_position, from_person,
                       to_person, request_subject, status, source_document
                FROM requests
                WHERE source_sha256 = ?
                ORDER BY id ASC
                """,
                (sha256,),
            ).fetchall()
            if len(rows) <= 1:
                return None
            orig = rows[0]
            orig_id = int(orig["id"])
            is_dup = int(request_id) != orig_id
            return {
                "is_duplicate": is_dup,
                "original_id": orig_id,
                "original_date": str(orig["received_date"] or ""),
                "original_department": str(orig["from_department"] or ""),
                "original_position": str(orig["from_position"] or ""),
                "original_person": str(orig["from_person"] or ""),
                "original_to_person": str(orig["to_person"] or ""),
                "original_subject": str(orig["request_subject"] or ""),
                "original_status": str(orig["status"] or ""),
                "total_count": len(rows),
                "other_ids": [int(r["id"]) for r in rows if int(r["id"]) != int(request_id)],
            }

    def set_parser_metadata(self, request_id: int, diagnostics: dict[str, Any], parser_version: str) -> None:
        payload = json.dumps(diagnostics or {}, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO request_parser_metadata(request_id, parser_version, diagnostics_json, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(request_id) DO UPDATE SET
                    parser_version = excluded.parser_version,
                    diagnostics_json = excluded.diagnostics_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (int(request_id), str(parser_version or ""), payload),
            )

    def get_parser_metadata(self, request_id: int) -> dict[str, Any]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT parser_version, diagnostics_json, updated_at FROM request_parser_metadata WHERE request_id = ?",
                (int(request_id),),
            ).fetchone()
        if row is None:
            return {}
        try:
            diagnostics = json.loads(str(row["diagnostics_json"] or "{}"))
        except json.JSONDecodeError:
            diagnostics = {}
        if not isinstance(diagnostics, dict):
            diagnostics = {}
        diagnostics.setdefault("parser_version", str(row["parser_version"] or ""))
        diagnostics["metadata_updated_at"] = str(row["updated_at"] or "")
        return diagnostics

    def rebuild_search_index(self) -> int:
        searchable = (
            "COALESCE(r.received_date, '') || ' ' || COALESCE(r.due_date, '') || ' ' || "
            "COALESCE(r.completed_date, '') || ' ' || COALESCE(r.from_department, '') || ' ' || "
            "COALESCE(r.from_position, '') || ' ' || COALESCE(r.from_person, '') || ' ' || "
            "COALESCE(r.to_person, '') || ' ' || COALESCE(r.request_subject, '') || ' ' || "
            "COALESCE(r.note, '') || ' ' || COALESCE(r.status, '') || ' ' || COALESCE(s.ocr_text, '')"
        )
        with self._get_connection() as conn:
            conn.execute("DELETE FROM request_fts")
            conn.execute(
                f"""
                INSERT INTO request_fts(rowid, request_id, searchable_text)
                SELECT r.id, r.id, {searchable}
                FROM requests r LEFT JOIN request_search_content s ON s.request_id = r.id
                """
            )
            count = int(conn.execute("SELECT COUNT(*) FROM request_fts").fetchone()[0])
        return count

    def search_request_hits_full_text(self, value: str, *, limit: int = 500) -> list[dict[str, Any]]:
        text = str(value or "").strip()
        if not text:
            return []
        query = self._fts_query(text)
        safe_limit = max(1, min(int(limit), 2000))
        is_dup_search = any(term in text.casefold() for term in ("дубликат", "дубликаты", "дубль", "дубли", "копи"))
        dup_subquery = (
            "SELECT r_dup.id FROM requests r_dup "
            "JOIN (SELECT source_sha256, MIN(id) AS min_id FROM requests "
            "WHERE TRIM(COALESCE(source_sha256, '')) != '' "
            "GROUP BY source_sha256 HAVING COUNT(*) > 1) g_dup "
            "ON r_dup.source_sha256 = g_dup.source_sha256 "
            "WHERE r_dup.id != g_dup.min_id"
        )
        if query:
            try:
                with self._get_connection() as conn:
                    rows = conn.execute(
                        """
                        SELECT CAST(request_id AS INTEGER) AS request_id,
                               snippet(request_fts, 1, '⟦', '⟧', ' … ', 18) AS snippet,
                               bm25(request_fts) AS rank
                        FROM request_fts
                        WHERE request_fts MATCH ?
                        ORDER BY rank, rowid DESC
                        LIMIT ?
                        """,
                        (query, safe_limit),
                    ).fetchall()
                    hits = [
                        {"request_id": int(row["request_id"]), "snippet": str(row["snippet"] or ""), "rank": float(row["rank"] or 0.0)}
                        for row in rows
                    ]
                    if is_dup_search:
                        dup_rows = conn.execute(dup_subquery).fetchall()
                        seen = {h["request_id"] for h in hits}
                        for r in dup_rows:
                            rid = int(r["id"])
                            if rid not in seen:
                                hits.append({"request_id": rid, "snippet": "⚠️ Дубликат", "rank": -0.5})
                                seen.add(rid)
                    return hits
            except sqlite3.OperationalError:
                pass
        ids = self.search_request_ids_full_text(text, limit=safe_limit)
        return [{"request_id": request_id, "snippet": "", "rank": 0.0} for request_id in ids]

    def quick_check(self) -> str:
        with self._get_connection() as conn:
            row = conn.execute("PRAGMA quick_check").fetchone()
        return str(row[0] if row else "unknown")

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

        is_dup_search = any(term in value.casefold() for term in ("дубликат", "дубликаты", "дубль", "дубли", "копи"))
        dup_subquery = (
            "(SELECT r_dup.id FROM requests r_dup "
            "JOIN (SELECT source_sha256, MIN(id) AS min_id FROM requests "
            "WHERE TRIM(COALESCE(source_sha256, '')) != '' "
            "GROUP BY source_sha256 HAVING COUNT(*) > 1) g_dup "
            "ON r_dup.source_sha256 = g_dup.source_sha256 "
            "WHERE r_dup.id != g_dup.min_id)"
        )
        with self._get_connection() as conn:
            if field == "all":
                dup_or = f" OR id IN {dup_subquery}" if is_dup_search else ""
                query = f"""
                    SELECT * FROM requests
                    WHERE CAST(id AS TEXT) LIKE ? OR received_date LIKE ?
                        OR due_date LIKE ? OR completed_date LIKE ?
                        OR from_department LIKE ? OR from_position LIKE ?
                        OR from_person LIKE ? OR to_person LIKE ?
                        OR request_subject LIKE ? OR note LIKE ? OR status LIKE ?
                        {dup_or}
                    ORDER BY is_pinned DESC, id DESC
                """
                search_value = f"%{value}%"
                params = (search_value,) * 11
            elif field == "status" and is_dup_search:
                query = (
                    f"SELECT * FROM requests WHERE (CAST(status AS TEXT) LIKE ? OR id IN {dup_subquery}) "
                    "ORDER BY is_pinned DESC, id DESC"
                )
                params = (f"%{value}%",)
            else:
                query = (
                    f"SELECT * FROM requests WHERE CAST({field} AS TEXT) LIKE ? "
                    "ORDER BY is_pinned DESC, id DESC"
                )
                params = (f"%{value}%",)

            rows = conn.execute(query, params).fetchall()
            return [self._row_to_request(row) for row in rows]
