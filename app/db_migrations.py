from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Callable, Iterable

CURRENT_SCHEMA_VERSION = 7

PRESERVED_REQUEST_COLUMNS = (
    "id", "received_date", "due_date", "completed_date", "from_department",
    "from_position", "from_person", "to_person", "request_subject", "note",
    "status", "source_document", "created_at", "updated_at", "is_pinned",
    "source_sha256", "source_size_bytes",
)
PRESERVED_HISTORY_COLUMNS = (
    "id", "request_id", "action", "description", "created_at", "snapshot",
)


class DatabaseMigrationError(RuntimeError):
    """Raised when a database cannot be migrated or validated safely."""


@dataclass(frozen=True, slots=True)
class DatabaseValidationReport:
    path: str
    schema_version: int
    integrity_check: str
    foreign_key_errors: int
    requests_count: int
    history_count: int
    request_ids_sha256: str
    history_ids_sha256: str

    @property
    def ok(self) -> bool:
        return self.integrity_check.lower() == "ok" and self.foreign_key_errors == 0

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["ok"] = self.ok
        return payload


@dataclass(frozen=True, slots=True)
class DatabaseRollbackComparison:
    current_db_path: str
    current_schema: int
    target_schema: int
    schema_differs: bool
    extra_tables: tuple[str, ...]
    extra_request_columns: tuple[str, ...]
    extra_history_columns: tuple[str, ...]
    affected_records_count: int
    current_requests_count: int
    current_history_count: int
    reference_db_path: str = ""
    reference_requests_count: int | None = None
    reference_history_count: int | None = None
    data_differs_from_reference: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def has_differences(self) -> bool:
        return bool(
            self.schema_differs
            or self.extra_tables
            or self.extra_request_columns
            or self.extra_history_columns
            or self.data_differs_from_reference
            or self.warnings
        )

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["has_differences"] = self.has_differences
        return payload

    def format_warning_text(
        self,
        *,
        current_version: str = "",
        target_version: str = "",
    ) -> str:
        header_parts: list[str] = []
        if current_version and target_version:
            header_parts.append(
                f"Переход с версии {current_version} (схема БД v{self.current_schema}) "
                f"на более старую версию {target_version} (схема БД v{self.target_schema})."
            )
        else:
            header_parts.append(
                f"Текущая схема БД: v{self.current_schema}, целевая схема БД: v{self.target_schema}."
            )
        if self.warnings:
            bullet_lines = "\n".join(f"• {line}" for line in self.warnings)
            header_parts.append("Обнаружены различия в базе данных:\n" + bullet_lines)
        header_parts.append(
            "Текущая база данных (с которой выполняется переход на более старую версию) "
            "будет автоматически сохранена в резервную копию перед откатом."
        )
        return "\n\n".join(header_parts)


def request_columns_for_schema(schema_version: int) -> tuple[str, ...]:
    if schema_version <= 0:
        return ()
    columns: list[str] = ["id", "received_date"]
    if schema_version >= 3:
        columns.append("due_date")
    if schema_version >= 4:
        columns.append("completed_date")
    columns.extend(
        [
            "from_department",
            "from_position",
            "from_person",
            "to_person",
            "request_subject",
            "note",
            "status",
            "source_document",
            "created_at",
            "updated_at",
        ]
    )
    if schema_version >= 5:
        columns.append("is_pinned")
    if schema_version >= 7:
        columns.extend(["source_sha256", "source_size_bytes"])
    return tuple(columns)


def history_columns_for_schema(schema_version: int) -> tuple[str, ...]:
    if schema_version <= 0:
        return ()
    columns: list[str] = ["id", "request_id", "action", "description", "created_at"]
    if schema_version >= 2:
        columns.append("snapshot")
    return tuple(columns)


def expected_tables_for_schema(schema_version: int) -> set[str]:
    if schema_version <= 0:
        return set()
    tables = {"requests", "request_history"}
    if schema_version >= 6:
        tables.update({"request_search_content", "request_fts"})
    if schema_version >= 7:
        tables.add("request_parser_metadata")
    return tables


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    if name not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migration_0_to_1(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS requests (
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


def _migration_1_to_2(conn: sqlite3.Connection) -> None:
    _migration_0_to_1(conn)
    _ensure_column(conn, "request_history", "snapshot", "TEXT")


def _migration_2_to_3(conn: sqlite3.Connection) -> None:
    _migration_0_to_1(conn)
    _ensure_column(conn, "requests", "due_date", "TEXT DEFAULT ''")


def _migration_3_to_4(conn: sqlite3.Connection) -> None:
    _migration_0_to_1(conn)
    _ensure_column(conn, "requests", "completed_date", "TEXT DEFAULT ''")


def _migration_4_to_5(conn: sqlite3.Connection) -> None:
    _migration_0_to_1(conn)
    _ensure_column(conn, "requests", "is_pinned", "INTEGER NOT NULL DEFAULT 0")


def _searchable_text_sql(prefix: str) -> str:
    fields = (
        "received_date", "due_date", "completed_date", "from_department",
        "from_position", "from_person", "to_person", "request_subject", "note", "status",
    )
    return " || ' ' || ".join(f"COALESCE({prefix}.{field}, '')" for field in fields)


def _drop_fts_triggers(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS trg_requests_fts_insert;
        DROP TRIGGER IF EXISTS trg_requests_fts_update;
        DROP TRIGGER IF EXISTS trg_requests_fts_delete;
        DROP TRIGGER IF EXISTS trg_search_content_fts_insert;
        DROP TRIGGER IF EXISTS trg_search_content_fts_update;
        DROP TRIGGER IF EXISTS trg_search_content_fts_delete;
        """
    )


def _create_search_index(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS request_search_content (
            request_id INTEGER PRIMARY KEY,
            ocr_text TEXT NOT NULL DEFAULT '',
            indexed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(request_id) REFERENCES requests(id) ON DELETE CASCADE
        )
        """
    )
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS request_fts USING fts5(
                request_id UNINDEXED,
                searchable_text,
                tokenize='unicode61 remove_diacritics 2'
            )
            """
        )
    except sqlite3.OperationalError as error:
        raise DatabaseMigrationError(
            "Текущая SQLite-сборка не поддерживает FTS5, необходимый для полнотекстового поиска."
        ) from error

    new_text = _searchable_text_sql("new")
    request_text = _searchable_text_sql("r")
    conn.executescript(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_requests_fts_insert
        AFTER INSERT ON requests
        BEGIN
            INSERT OR REPLACE INTO request_fts(rowid, request_id, searchable_text)
            VALUES (
                new.id,
                new.id,
                {new_text} || ' ' || COALESCE(
                    (SELECT ocr_text FROM request_search_content WHERE request_id = new.id), ''
                )
            );
        END;

        CREATE TRIGGER IF NOT EXISTS trg_requests_fts_update
        AFTER UPDATE OF received_date, due_date, completed_date, from_department,
            from_position, from_person, to_person, request_subject, note, status
        ON requests
        BEGIN
            DELETE FROM request_fts WHERE rowid = old.id;
            INSERT INTO request_fts(rowid, request_id, searchable_text)
            VALUES (
                new.id,
                new.id,
                {new_text} || ' ' || COALESCE(
                    (SELECT ocr_text FROM request_search_content WHERE request_id = new.id), ''
                )
            );
        END;

        CREATE TRIGGER IF NOT EXISTS trg_requests_fts_delete
        AFTER DELETE ON requests
        BEGIN
            DELETE FROM request_fts WHERE rowid = old.id;
            DELETE FROM request_search_content WHERE request_id = old.id;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_search_content_fts_insert
        AFTER INSERT ON request_search_content
        BEGIN
            DELETE FROM request_fts WHERE rowid = new.request_id;
            INSERT INTO request_fts(rowid, request_id, searchable_text)
            SELECT r.id, r.id, {request_text} || ' ' || COALESCE(new.ocr_text, '')
            FROM requests r WHERE r.id = new.request_id;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_search_content_fts_update
        AFTER UPDATE OF ocr_text ON request_search_content
        BEGIN
            DELETE FROM request_fts WHERE rowid = new.request_id;
            INSERT INTO request_fts(rowid, request_id, searchable_text)
            SELECT r.id, r.id, {request_text} || ' ' || COALESCE(new.ocr_text, '')
            FROM requests r WHERE r.id = new.request_id;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_search_content_fts_delete
        AFTER DELETE ON request_search_content
        BEGIN
            DELETE FROM request_fts WHERE rowid = old.request_id;
            INSERT INTO request_fts(rowid, request_id, searchable_text)
            SELECT r.id, r.id, {request_text}
            FROM requests r WHERE r.id = old.request_id;
        END;
        """
    )

    conn.execute("DELETE FROM request_fts")
    conn.execute(
        f"""
        INSERT INTO request_fts(rowid, request_id, searchable_text)
        SELECT r.id, r.id,
            {request_text} || ' ' || COALESCE(s.ocr_text, '')
        FROM requests r
        LEFT JOIN request_search_content s ON s.request_id = r.id
        """
    )


def _migration_5_to_6(conn: sqlite3.Connection) -> None:
    _migration_4_to_5(conn)
    _create_search_index(conn)


def _migration_6_to_7(conn: sqlite3.Connection) -> None:
    _migration_5_to_6(conn)
    _ensure_column(conn, "requests", "source_sha256", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "requests", "source_size_bytes", "INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_requests_source_fingerprint "
        "ON requests(source_sha256, source_size_bytes)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS request_parser_metadata (
            request_id INTEGER PRIMARY KEY,
            parser_version TEXT NOT NULL DEFAULT '',
            diagnostics_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(request_id) REFERENCES requests(id) ON DELETE CASCADE
        )
        """
    )


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    0: _migration_0_to_1,
    1: _migration_1_to_2,
    2: _migration_2_to_3,
    3: _migration_3_to_4,
    4: _migration_4_to_5,
    5: _migration_5_to_6,
    6: _migration_6_to_7,
}


def _ids_digest(conn: sqlite3.Connection, table: str) -> str:
    digest = hashlib.sha256()
    try:
        rows: Iterable[tuple] = conn.execute(f"SELECT id FROM {table} ORDER BY id")
    except sqlite3.Error:
        return ""
    for row in rows:
        digest.update(str(row[0]).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _rows_digest(conn: sqlite3.Connection, table: str, columns: Iterable[str]) -> str:
    selected = tuple(str(column) for column in columns)
    digest = hashlib.sha256()
    if not selected:
        return digest.hexdigest()
    quoted = ", ".join('"' + column.replace('"', '""') + '"' for column in selected)
    try:
        rows = conn.execute(f"SELECT {quoted} FROM {table} ORDER BY id")
    except sqlite3.Error:
        return ""
    for row in rows:
        payload = json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), default=str)
        digest.update(payload.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def get_schema_version(path: str | Path) -> int:
    database = Path(path)
    if not database.is_file():
        return 0
    try:
        with closing(sqlite3.connect(database, timeout=10.0)) as conn:
            row = conn.execute("PRAGMA user_version").fetchone()
            return int(row[0] if row else 0)
    except sqlite3.Error as error:
        raise DatabaseMigrationError(f"Не удалось прочитать версию схемы БД: {error}") from error


def capture_invariants(
    path: str | Path,
    *,
    max_schema: int | None = None,
) -> dict[str, object]:
    database = Path(path)
    empty_digest = hashlib.sha256(b"").hexdigest()
    if not database.is_file():
        return {
            "requests_count": 0,
            "history_count": 0,
            "request_ids_sha256": empty_digest,
            "history_ids_sha256": empty_digest,
            "requests_columns": [],
            "history_columns": [],
            "requests_data_sha256": empty_digest,
            "history_data_sha256": empty_digest,
        }
    allowed_req = (
        set(request_columns_for_schema(max_schema))
        if max_schema is not None
        else set(PRESERVED_REQUEST_COLUMNS)
    )
    allowed_hist = (
        set(history_columns_for_schema(max_schema))
        if max_schema is not None
        else set(PRESERVED_HISTORY_COLUMNS)
    )
    try:
        with closing(sqlite3.connect(database, timeout=10.0)) as conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            request_columns = (
                [
                    column
                    for column in PRESERVED_REQUEST_COLUMNS
                    if column in allowed_req and column in _column_names(conn, "requests")
                ]
                if "requests" in tables
                else []
            )
            history_columns = (
                [
                    column
                    for column in PRESERVED_HISTORY_COLUMNS
                    if column in allowed_hist and column in _column_names(conn, "request_history")
                ]
                if "request_history" in tables
                else []
            )
            requests_count = (
                int(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
                if "requests" in tables
                else 0
            )
            history_count = (
                int(conn.execute("SELECT COUNT(*) FROM request_history").fetchone()[0])
                if "request_history" in tables
                else 0
            )
            return {
                "requests_count": requests_count,
                "history_count": history_count,
                "request_ids_sha256": (
                    _ids_digest(conn, "requests") if "requests" in tables else empty_digest
                ),
                "history_ids_sha256": (
                    _ids_digest(conn, "request_history") if "request_history" in tables else empty_digest
                ),
                "requests_columns": request_columns,
                "history_columns": history_columns,
                "requests_data_sha256": (
                    _rows_digest(conn, "requests", request_columns)
                    if "requests" in tables
                    else empty_digest
                ),
                "history_data_sha256": (
                    _rows_digest(conn, "request_history", history_columns)
                    if "request_history" in tables
                    else empty_digest
                ),
            }
    except sqlite3.Error as error:
        raise DatabaseMigrationError(f"Не удалось снять контрольные показатели БД: {error}") from error


def _user_tables(conn: sqlite3.Connection) -> set[str]:
    raw_tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    filtered: set[str] = set()
    for name in raw_tables:
        if name == "sqlite_sequence":
            continue
        if name.startswith("request_fts_"):
            continue
        filtered.add(name)
    return filtered


def compare_database_for_rollback(
    path: str | Path,
    target_schema: int,
    *,
    reference_db_path: str | Path | None = None,
) -> DatabaseRollbackComparison:
    """Inspect differences between the current DB and a rollback target version."""
    database = Path(path)
    target_schema = max(0, int(target_schema))
    ref_path = Path(reference_db_path) if reference_db_path else None
    if not database.is_file():
        return DatabaseRollbackComparison(
            current_db_path=str(database),
            current_schema=0,
            target_schema=target_schema,
            schema_differs=False,
            extra_tables=(),
            extra_request_columns=(),
            extra_history_columns=(),
            affected_records_count=0,
            current_requests_count=0,
            current_history_count=0,
            reference_db_path=str(ref_path) if ref_path and ref_path.is_file() else "",
        )

    warnings: list[str] = []
    affected_records = 0
    try:
        with closing(sqlite3.connect(database, timeout=10.0)) as conn:
            version_row = conn.execute("PRAGMA user_version").fetchone()
            current_schema = int(version_row[0] if version_row else 0)
            tables = _user_tables(conn)
            allowed_tables = expected_tables_for_schema(target_schema)
            extra_tables = tuple(sorted(tables - allowed_tables))

            req_cols = _column_names(conn, "requests") if "requests" in tables else set()
            hist_cols = _column_names(conn, "request_history") if "request_history" in tables else set()
            target_req_cols = set(request_columns_for_schema(target_schema))
            target_hist_cols = set(history_columns_for_schema(target_schema))

            extra_req_cols = tuple(sorted(req_cols - target_req_cols))
            extra_hist_cols = tuple(sorted(hist_cols - target_hist_cols))

            current_requests_count = (
                int(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
                if "requests" in tables
                else 0
            )
            current_history_count = (
                int(conn.execute("SELECT COUNT(*) FROM request_history").fetchone()[0])
                if "request_history" in tables
                else 0
            )

            schema_differs = current_schema != target_schema
            if schema_differs:
                if current_schema > target_schema:
                    warnings.append(
                        f"Версия схемы текущей БД (v{current_schema}) новее схемы выбранной версии (v{target_schema})."
                    )
                else:
                    warnings.append(
                        f"Версия схемы текущей БД (v{current_schema}) отличается от схемы выбранной версии (v{target_schema})."
                    )

            if extra_tables:
                warnings.append(
                    "В текущей БД присутствуют таблицы более новой версии: "
                    + ", ".join(extra_tables)
                    + "."
                )
            if extra_req_cols:
                warnings.append(
                    "В таблице запросов (requests) есть поля более новой версии: "
                    + ", ".join(extra_req_cols)
                    + "."
                )
            if extra_hist_cols:
                warnings.append(
                    "В таблице истории (request_history) есть поля более новой версии: "
                    + ", ".join(extra_hist_cols)
                    + "."
                )

            if target_schema < 7:
                if "source_sha256" in req_cols or "source_size_bytes" in req_cols:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM requests "
                        "WHERE TRIM(COALESCE(source_sha256, '')) <> '' OR COALESCE(source_size_bytes, 0) > 0"
                    ).fetchone()
                    fp_count = int(row[0] if row else 0)
                    affected_records += fp_count
                if "request_parser_metadata" in tables:
                    row = conn.execute("SELECT COUNT(*) FROM request_parser_metadata").fetchone()
                    meta_count = int(row[0] if row else 0)
                    affected_records += meta_count
            if target_schema < 6 and "request_search_content" in tables:
                row = conn.execute(
                    "SELECT COUNT(*) FROM request_search_content WHERE TRIM(COALESCE(ocr_text, '')) <> ''"
                ).fetchone()
                ocr_count = int(row[0] if row else 0)
                affected_records += ocr_count
            if target_schema < 5 and "is_pinned" in req_cols:
                row = conn.execute(
                    "SELECT COUNT(*) FROM requests WHERE COALESCE(is_pinned, 0) <> 0"
                ).fetchone()
                pin_count = int(row[0] if row else 0)
                affected_records += pin_count
                if pin_count:
                    warnings.append(
                        f"Закреплённые запросы ({pin_count} шт.) не поддерживаются в схеме v{target_schema}."
                    )
            if target_schema < 4 and "completed_date" in req_cols:
                row = conn.execute(
                    "SELECT COUNT(*) FROM requests WHERE TRIM(COALESCE(completed_date, '')) <> ''"
                ).fetchone()
                comp_count = int(row[0] if row else 0)
                affected_records += comp_count
                if comp_count:
                    warnings.append(
                        f"Поле даты исполнения заполнено у {comp_count} записей и не поддерживается в схеме v{target_schema}."
                    )
            if target_schema < 3 and "due_date" in req_cols:
                row = conn.execute(
                    "SELECT COUNT(*) FROM requests WHERE TRIM(COALESCE(due_date, '')) <> ''"
                ).fetchone()
                due_count = int(row[0] if row else 0)
                affected_records += due_count
                if due_count:
                    warnings.append(
                        f"Поле контрольного срока заполнено у {due_count} записей и не поддерживается в схеме v{target_schema}."
                    )
            if target_schema < 2 and "snapshot" in hist_cols:
                row = conn.execute(
                    "SELECT COUNT(*) FROM request_history WHERE TRIM(COALESCE(snapshot, '')) <> ''"
                ).fetchone()
                snap_count = int(row[0] if row else 0)
                affected_records += snap_count

            if affected_records > 0 and (extra_tables or extra_req_cols or extra_hist_cols):
                warnings.append(
                    f"Записей, использующих структуры более новой версии БД: {affected_records}."
                )
    except sqlite3.Error as error:
        raise DatabaseMigrationError(f"Не удалось проанализировать различия БД: {error}") from error

    ref_requests_count: int | None = None
    ref_history_count: int | None = None
    data_differs = False
    if ref_path is not None and ref_path.is_file():
        try:
            compare_schema = min(current_schema, target_schema, get_schema_version(ref_path))
            current_inv = capture_invariants(database, max_schema=max(1, compare_schema))
            ref_inv = capture_invariants(ref_path, max_schema=max(1, compare_schema))
            ref_requests_count = int(ref_inv["requests_count"])
            ref_history_count = int(ref_inv["history_count"])
            if (
                current_inv["requests_count"] != ref_inv["requests_count"]
                or current_inv["history_count"] != ref_inv["history_count"]
                or current_inv["requests_data_sha256"] != ref_inv["requests_data_sha256"]
                or current_inv["history_data_sha256"] != ref_inv["history_data_sha256"]
            ):
                data_differs = True
                warnings.append(
                    "Данные в текущей БД отличаются от сохранённой копии БД предыдущей версии "
                    f"(записей сейчас: {current_requests_count}, в копии: {ref_requests_count}; "
                    f"записей истории сейчас: {current_history_count}, в копии: {ref_history_count})."
                )
        except Exception:
            pass

    return DatabaseRollbackComparison(
        current_db_path=str(database),
        current_schema=current_schema,
        target_schema=target_schema,
        schema_differs=schema_differs,
        extra_tables=extra_tables,
        extra_request_columns=extra_req_cols,
        extra_history_columns=extra_hist_cols,
        affected_records_count=affected_records,
        current_requests_count=current_requests_count,
        current_history_count=current_history_count,
        reference_db_path=str(ref_path) if ref_path and ref_path.is_file() else "",
        reference_requests_count=ref_requests_count,
        reference_history_count=ref_history_count,
        data_differs_from_reference=data_differs,
        warnings=tuple(warnings),
    )


def validate_database(
    path: str | Path,
    *,
    expected_schema: int | None = None,
    expected_invariants: dict[str, object] | None = None,
) -> DatabaseValidationReport:
    database = Path(path)
    if not database.is_file():
        raise DatabaseMigrationError(f"Файл БД не найден: {database}")

    try:
        with closing(sqlite3.connect(database, timeout=10.0)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            missing_tables = {"requests", "request_history"} - tables
            if missing_tables:
                raise DatabaseMigrationError(
                    "В БД отсутствуют обязательные таблицы: " + ", ".join(sorted(missing_tables))
                )

            version_row = conn.execute("PRAGMA user_version").fetchone()
            schema_version = int(version_row[0] if version_row else 0)
            validation_schema = int(expected_schema) if expected_schema is not None else schema_version

            if validation_schema >= 6:
                required_search_tables = {"request_search_content", "request_fts"}
                missing_search_tables = required_search_tables - tables
                if missing_search_tables:
                    raise DatabaseMigrationError(
                        "В БД отсутствуют таблицы полнотекстового поиска: "
                        + ", ".join(sorted(missing_search_tables))
                    )

            if validation_schema >= 7:
                if "request_parser_metadata" not in tables:
                    raise DatabaseMigrationError("В БД отсутствует таблица метаданных parser-а: request_parser_metadata")

            required_request_columns = {
                "id",
                "received_date",
                "from_department",
                "from_position",
                "from_person",
                "to_person",
                "request_subject",
                "note",
                "status",
                "source_document",
                "created_at",
                "updated_at",
            }
            if validation_schema >= 3:
                required_request_columns.add("due_date")
            if validation_schema >= 4:
                required_request_columns.add("completed_date")
            if validation_schema >= 5:
                required_request_columns.add("is_pinned")
            if validation_schema >= 7:
                required_request_columns.update({"source_sha256", "source_size_bytes"})
            missing_request_columns = required_request_columns - _column_names(conn, "requests")
            if missing_request_columns:
                raise DatabaseMigrationError(
                    "В таблице requests отсутствуют поля: "
                    + ", ".join(sorted(missing_request_columns))
                )

            required_history_columns = {
                "id",
                "request_id",
                "action",
                "description",
                "created_at",
            }
            if validation_schema >= 2:
                required_history_columns.add("snapshot")
            missing_history_columns = required_history_columns - _column_names(conn, "request_history")
            if missing_history_columns:
                raise DatabaseMigrationError(
                    "В таблице request_history отсутствуют поля: "
                    + ", ".join(sorted(missing_history_columns))
                )

            if expected_schema is not None and schema_version != int(expected_schema):
                raise DatabaseMigrationError(
                    f"Версия схемы БД {schema_version}, ожидалась {int(expected_schema)}."
                )

            integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
            integrity_check = str(integrity_row[0] if integrity_row else "")
            if integrity_check.lower() != "ok":
                raise DatabaseMigrationError(f"PRAGMA integrity_check: {integrity_check}")

            foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_rows:
                raise DatabaseMigrationError(
                    f"PRAGMA foreign_key_check обнаружил ошибок: {len(foreign_key_rows)}"
                )

            requests_count = int(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
            history_count = int(conn.execute("SELECT COUNT(*) FROM request_history").fetchone()[0])
            request_ids_sha256 = _ids_digest(conn, "requests")
            history_ids_sha256 = _ids_digest(conn, "request_history")

            report = DatabaseValidationReport(
                path=str(database),
                schema_version=schema_version,
                integrity_check=integrity_check,
                foreign_key_errors=len(foreign_key_rows),
                requests_count=requests_count,
                history_count=history_count,
                request_ids_sha256=request_ids_sha256,
                history_ids_sha256=history_ids_sha256,
            )
    except DatabaseMigrationError:
        raise
    except sqlite3.Error as error:
        raise DatabaseMigrationError(f"Не удалось проверить БД: {error}") from error

    if expected_invariants:
        checks = {
            "requests_count": report.requests_count,
            "history_count": report.history_count,
            "request_ids_sha256": report.request_ids_sha256,
            "history_ids_sha256": report.history_ids_sha256,
        }
        for key, expected in expected_invariants.items():
            if key in checks and checks[key] != expected:
                raise DatabaseMigrationError(
                    f"После миграции изменился контрольный показатель {key}: "
                    f"{checks[key]!r} != {expected!r}."
                )

        try:
            with closing(sqlite3.connect(database, timeout=10.0)) as conn:
                for prefix, table in (("requests", "requests"), ("history", "request_history")):
                    columns = [str(value) for value in expected_invariants.get(f"{prefix}_columns", [])]
                    expected_digest = expected_invariants.get(f"{prefix}_data_sha256")
                    if expected_digest is None:
                        continue
                    missing = set(columns) - _column_names(conn, table)
                    if missing:
                        raise DatabaseMigrationError(
                            f"После миграции исчезли старые поля {table}: "
                            + ", ".join(sorted(missing))
                        )
                    actual_digest = _rows_digest(conn, table, columns)
                    if actual_digest != expected_digest:
                        raise DatabaseMigrationError(
                            f"После миграции изменились существующие данные таблицы {table}."
                        )
        except DatabaseMigrationError:
            raise
        except sqlite3.Error as error:
            raise DatabaseMigrationError(
                f"Не удалось сравнить данные до и после миграции: {error}"
            ) from error

    return report


def _rebuild_tables_for_target_schema(conn: sqlite3.Connection, target_schema: int) -> None:
    req_defs: list[tuple[str, str, str]] = [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT", "id"),
        ("received_date", "TEXT NOT NULL", "COALESCE(received_date, '')"),
    ]
    if target_schema >= 3:
        req_defs.append(("due_date", "TEXT DEFAULT ''", "COALESCE(due_date, '')"))
    if target_schema >= 4:
        req_defs.append(("completed_date", "TEXT DEFAULT ''", "COALESCE(completed_date, '')"))
    req_defs.extend(
        [
            ("from_department", "TEXT", "from_department"),
            ("from_position", "TEXT", "from_position"),
            ("from_person", "TEXT", "from_person"),
            ("to_person", "TEXT", "to_person"),
            ("request_subject", "TEXT", "request_subject"),
            ("note", "TEXT DEFAULT ''", "COALESCE(note, '')"),
            ("status", "TEXT NOT NULL DEFAULT 'Новый'", "COALESCE(status, 'Новый')"),
            ("source_document", "TEXT", "source_document"),
            ("created_at", "TEXT DEFAULT CURRENT_TIMESTAMP", "created_at"),
            ("updated_at", "TEXT DEFAULT CURRENT_TIMESTAMP", "updated_at"),
        ]
    )
    if target_schema >= 5:
        req_defs.append(("is_pinned", "INTEGER NOT NULL DEFAULT 0", "COALESCE(is_pinned, 0)"))
    if target_schema >= 7:
        req_defs.extend(
            [
                ("source_sha256", "TEXT NOT NULL DEFAULT ''", "COALESCE(source_sha256, '')"),
                ("source_size_bytes", "INTEGER NOT NULL DEFAULT 0", "COALESCE(source_size_bytes, 0)"),
            ]
        )

    existing_req = _column_names(conn, "requests")
    conn.execute("DROP TABLE IF EXISTS requests__downgrade")
    create_req_cols = ",\n            ".join(f"{name} {col_def}" for name, col_def, _ in req_defs)
    conn.execute(f"CREATE TABLE requests__downgrade (\n            {create_req_cols}\n        )")
    insert_req_names = ", ".join(name for name, _, _ in req_defs)
    select_req_exprs = ", ".join(
        expr if name in existing_req else ("0" if "INTEGER" in col_def else "''")
        for name, col_def, expr in req_defs
    )
    conn.execute(
        f"INSERT INTO requests__downgrade ({insert_req_names}) "
        f"SELECT {select_req_exprs} FROM requests ORDER BY id"
    )
    conn.execute("DROP TABLE requests")
    conn.execute("ALTER TABLE requests__downgrade RENAME TO requests")

    hist_defs: list[tuple[str, str, str]] = [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT", "id"),
        ("request_id", "INTEGER", "request_id"),
        ("action", "TEXT NOT NULL", "COALESCE(action, '')"),
        ("description", "TEXT NOT NULL", "COALESCE(description, '')"),
        ("created_at", "TEXT DEFAULT CURRENT_TIMESTAMP", "created_at"),
    ]
    if target_schema >= 2:
        hist_defs.append(("snapshot", "TEXT", "snapshot"))

    existing_hist = _column_names(conn, "request_history")
    conn.execute("DROP TABLE IF EXISTS request_history__downgrade")
    create_hist_cols = ",\n            ".join(f"{name} {col_def}" for name, col_def, _ in hist_defs)
    conn.execute(f"CREATE TABLE request_history__downgrade (\n            {create_hist_cols}\n        )")
    insert_hist_names = ", ".join(name for name, _, _ in hist_defs)
    select_hist_exprs = ", ".join(
        expr if name in existing_hist else "NULL"
        for name, _, expr in hist_defs
    )
    conn.execute(
        f"INSERT INTO request_history__downgrade ({insert_hist_names}) "
        f"SELECT {select_hist_exprs} FROM request_history ORDER BY id"
    )
    conn.execute("DROP TABLE request_history")
    conn.execute("ALTER TABLE request_history__downgrade RENAME TO request_history")


def downgrade_database(
    path: str | Path,
    *,
    target_schema: int,
    expected_invariants: dict[str, object] | None = None,
) -> DatabaseValidationReport:
    """Safely convert a newer database schema down to target_schema for version rollback."""
    database = Path(path)
    if not database.is_file():
        raise DatabaseMigrationError(f"Файл БД не найден: {database}")
    if target_schema < 1 or target_schema > CURRENT_SCHEMA_VERSION:
        raise DatabaseMigrationError(
            f"Некорректная целевая версия схемы БД для отката: {target_schema}."
        )

    invariants = expected_invariants or capture_invariants(database, max_schema=target_schema)
    try:
        with closing(sqlite3.connect(database, timeout=30.0)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            try:
                conn.execute("BEGIN IMMEDIATE")
                for version in range(0, target_schema):
                    migration = _MIGRATIONS.get(version)
                    if migration is not None:
                        migration(conn)

                _drop_fts_triggers(conn)
                conn.execute("DROP TABLE IF EXISTS request_fts")
                if target_schema < 7:
                    conn.execute("DROP TABLE IF EXISTS request_parser_metadata")
                    conn.execute("DROP INDEX IF EXISTS idx_requests_source_fingerprint")
                if target_schema < 6:
                    conn.execute("DROP TABLE IF EXISTS request_search_content")

                _rebuild_tables_for_target_schema(conn, target_schema)

                if target_schema >= 6:
                    if "request_search_content" in _user_tables(conn):
                        conn.execute(
                            "DELETE FROM request_search_content "
                            "WHERE request_id NOT IN (SELECT id FROM requests)"
                        )
                    _create_search_index(conn)

                if target_schema >= 7:
                    _migration_6_to_7(conn)

                conn.execute(f"PRAGMA user_version = {int(target_schema)}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except DatabaseMigrationError:
        raise
    except sqlite3.Error as error:
        raise DatabaseMigrationError(f"Ошибка отката схемы БД: {error}") from error

    return validate_database(
        database,
        expected_schema=target_schema,
        expected_invariants=invariants,
    )


def migrate_database(
    path: str | Path,
    *,
    target_schema: int = CURRENT_SCHEMA_VERSION,
    expected_invariants: dict[str, object] | None = None,
    allow_downgrade: bool = False,
) -> DatabaseValidationReport:
    database = Path(path)
    database.parent.mkdir(parents=True, exist_ok=True)

    if target_schema < 0:
        raise DatabaseMigrationError("Версия схемы не может быть отрицательной.")
    if target_schema > CURRENT_SCHEMA_VERSION:
        raise DatabaseMigrationError(
            f"Эта версия программы поддерживает схему БД только до {CURRENT_SCHEMA_VERSION}, "
            f"запрошена {target_schema}."
        )

    if database.is_file():
        current_version = get_schema_version(database)
        if current_version > target_schema and allow_downgrade:
            downgrade_invariants = capture_invariants(database, max_schema=target_schema)
            return downgrade_database(
                database,
                target_schema=target_schema,
                expected_invariants=downgrade_invariants,
            )

    try:
        with closing(sqlite3.connect(database, timeout=30.0)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            version_row = conn.execute("PRAGMA user_version").fetchone()
            current = int(version_row[0] if version_row else 0)
            if current > target_schema:
                raise DatabaseMigrationError(
                    f"БД имеет более новую схему {current}; текущая программа ожидает {target_schema}."
                )

            # Historical builds used a few idempotent compatibility repairs in
            # addition to PRAGMA user_version. Apply the known shape builders
            # once even when user_version is already current, so an old/partly
            # upgraded database is repaired before strict validation.
            try:
                conn.execute("BEGIN IMMEDIATE")
                for version in range(0, target_schema):
                    migration = _MIGRATIONS.get(version)
                    if migration is not None:
                        migration(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

            while current < target_schema:
                migration = _MIGRATIONS.get(current)
                if migration is None:
                    raise DatabaseMigrationError(
                        f"Нет миграции схемы {current} -> {current + 1}."
                    )
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    migration(conn)
                    conn.execute(f"PRAGMA user_version = {current + 1}")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                current += 1
    except DatabaseMigrationError:
        raise
    except sqlite3.Error as error:
        raise DatabaseMigrationError(f"Ошибка миграции БД: {error}") from error

    return validate_database(
        database,
        expected_schema=target_schema,
        expected_invariants=expected_invariants,
    )


def write_report(report: DatabaseValidationReport, output: str | Path) -> Path:
    target = Path(output).expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target
