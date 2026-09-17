from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import shutil
import sqlite3
import tempfile

from app.backup_service import BackupService
from app.database import Database
from app.document_service import DocumentService


class LegacyImportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LegacyInspection:
    source_db: Path
    record_count: int
    columns: tuple[str, ...]
    document_references: int
    documents_found: int
    documents_missing: int
    suggested_documents_dir: Path | None


@dataclass(frozen=True, slots=True)
class LegacyImportResult:
    source_db: Path
    imported_records: int
    copied_documents: int
    missing_documents: int
    backup_path: Path


class LegacyImportService:
    """One-shot migration of a legacy SQLite registry into the current schema.

    Construction is intentionally inert: no database is opened, no directory is
    scanned and no thread/timer is created. All I/O starts only from inspect() or
    migrate(), which are called explicitly by the UI.
    """

    LEGACY_FIELDS = (
        "id",
        "received_date",
        "due_date",
        "completed_date",
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
        "is_pinned",
    )

    def __init__(
        self,
        database: Database,
        documents: DocumentService,
        backup: BackupService,
    ) -> None:
        self.database = database
        self.documents = documents
        self.backup = backup

    @staticmethod
    def _open_legacy(path: Path) -> sqlite3.Connection:
        if not path.is_file():
            raise LegacyImportError(f"Файл старой базы не найден: {path}")
        try:
            conn = sqlite3.connect(path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            return conn
        except sqlite3.Error as error:
            raise LegacyImportError(f"Не удалось открыть старую базу: {error}") from error

    @staticmethod
    def _table_names(conn: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
        return tuple(str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})"))

    @staticmethod
    def _suggest_documents_dir(source_db: Path) -> Path | None:
        candidates = (
            source_db.parent / "documents",
            source_db.parent / "data" / "documents",
            source_db.parent.parent / "documents",
            source_db.parent.parent / "data" / "documents",
        )
        for candidate in candidates:
            if candidate.is_dir():
                return candidate.resolve(strict=False)
        return None

    @staticmethod
    def _resolve_legacy_document(
        reference: str,
        source_db: Path,
        documents_dir: Path | None,
    ) -> Path | None:
        value = (reference or "").strip()
        if not value:
            return None
        raw = Path(value).expanduser()
        candidates: list[Path] = []
        if raw.is_absolute():
            candidates.append(raw)
        else:
            candidates.append(source_db.parent / raw)
        basename = raw.name
        if documents_dir is not None and basename:
            candidates.append(documents_dir / basename)
        if basename:
            candidates.extend(
                [
                    source_db.parent / "documents" / basename,
                    source_db.parent / "data" / "documents" / basename,
                    source_db.parent.parent / "documents" / basename,
                    source_db.parent.parent / "data" / "documents" / basename,
                ]
            )
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            try:
                if candidate.is_file():
                    return candidate.resolve(strict=False)
            except OSError:
                continue
        return None

    def inspect(
        self,
        source_db: str | Path,
        documents_dir: str | Path | None = None,
    ) -> LegacyInspection:
        source = Path(source_db).expanduser().resolve(strict=False)
        if source == self.database.db_path.resolve(strict=False):
            raise LegacyImportError("Нельзя импортировать текущую рабочую базу в саму себя.")

        explicit_docs = (
            Path(documents_dir).expanduser().resolve(strict=False)
            if documents_dir
            else None
        )
        suggested = explicit_docs or self._suggest_documents_dir(source)
        conn = self._open_legacy(source)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise LegacyImportError("Старая база не прошла проверку целостности.")
            if "requests" not in self._table_names(conn):
                raise LegacyImportError("В старой базе отсутствует таблица requests.")
            columns = self._columns(conn, "requests")
            recognized = set(columns).intersection(self.LEGACY_FIELDS)
            if len(recognized) < 4:
                raise LegacyImportError(
                    "Структура старой таблицы requests не похожа на поддерживаемую версию."
                )
            record_count = int(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0])

            refs: list[str] = []
            if "source_document" in columns:
                refs = [
                    str(row[0] or "")
                    for row in conn.execute(
                        "SELECT source_document FROM requests WHERE TRIM(COALESCE(source_document, '')) <> ''"
                    ).fetchall()
                ]
            found = sum(
                1
                for ref in refs
                if self._resolve_legacy_document(ref, source, suggested) is not None
            )
            return LegacyInspection(
                source_db=source,
                record_count=record_count,
                columns=columns,
                document_references=len(refs),
                documents_found=found,
                documents_missing=max(0, len(refs) - found),
                suggested_documents_dir=suggested,
            )
        except sqlite3.Error as error:
            raise LegacyImportError(f"Не удалось прочитать старую базу: {error}") from error
        finally:
            conn.close()

    @staticmethod
    def _unique_name(directory: Path, name: str, reserved: set[str]) -> str:
        safe_name = Path(name).name or "document.pdf"
        key = safe_name.casefold()
        if key not in reserved and not (directory / safe_name).exists():
            reserved.add(key)
            return safe_name
        source = Path(safe_name)
        index = 2
        while True:
            candidate = f"{source.stem} ({index}){source.suffix}"
            key = candidate.casefold()
            if key not in reserved and not (directory / candidate).exists():
                reserved.add(key)
                return candidate
            index += 1

    @staticmethod
    def _value(row: sqlite3.Row, columns: set[str], name: str, default=""):
        if name not in columns:
            return default
        value = row[name]
        return default if value is None else value

    @staticmethod
    def _as_bool_int(value) -> int:
        if isinstance(value, str):
            return int(value.strip().casefold() in {"1", "true", "yes", "да", "on"})
        return int(bool(value))

    def _build_current_database(
        self,
        source_db: Path,
        legacy_documents_dir: Path | None,
        staged_data: Path,
    ) -> tuple[Path, Path, int, int, int]:
        staged_docs = staged_data / "documents"
        staged_db = staged_data / "requests.db"
        staged_data.mkdir(parents=True, exist_ok=True)
        staged_docs.mkdir(parents=True, exist_ok=True)

        # Let the application's current Database class create the schema. This
        # guarantees the migrated file matches the same schema as a fresh install.
        Database(data_dir=staged_data, db_path=staged_db, documents_dir=staged_docs)

        source_conn = self._open_legacy(source_db)
        target_conn = sqlite3.connect(staged_db, timeout=10.0)
        target_conn.row_factory = sqlite3.Row
        copied = 0
        missing = 0
        imported = 0
        reserved: set[str] = set()
        try:
            old_columns_tuple = self._columns(source_conn, "requests")
            old_columns = set(old_columns_tuple)
            rows = source_conn.execute("SELECT * FROM requests ORDER BY id" if "id" in old_columns else "SELECT * FROM requests").fetchall()

            insert_sql = """
                INSERT INTO requests (
                    id, received_date, due_date, completed_date,
                    from_department, from_position, from_person, to_person,
                    request_subject, note, status, source_document,
                    created_at, updated_at, is_pinned
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          COALESCE(?, CURRENT_TIMESTAMP),
                          COALESCE(?, CURRENT_TIMESTAMP), ?)
            """
            for row in rows:
                source_document = str(self._value(row, old_columns, "source_document", "") or "").strip()
                migrated_document = source_document
                if source_document:
                    source_path = self._resolve_legacy_document(
                        source_document,
                        source_db,
                        legacy_documents_dir,
                    )
                    if source_path is not None:
                        target_name = self._unique_name(staged_docs, source_path.name, reserved)
                        shutil.copy2(source_path, staged_docs / target_name)
                        # Store the final path, not the temporary staging path.
                        migrated_document = str((self.documents.documents_dir / target_name).resolve(strict=False))
                        copied += 1
                    else:
                        # Preserve the old reference rather than silently losing it.
                        missing += 1

                request_id = self._value(row, old_columns, "id", None)
                received_date = str(self._value(row, old_columns, "received_date", "") or "")
                status = str(self._value(row, old_columns, "status", "Новый") or "Новый")
                created_at = self._value(row, old_columns, "created_at", None)
                updated_at = self._value(row, old_columns, "updated_at", None)
                target_conn.execute(
                    insert_sql,
                    (
                        request_id,
                        received_date,
                        str(self._value(row, old_columns, "due_date", "") or ""),
                        str(self._value(row, old_columns, "completed_date", "") or ""),
                        str(self._value(row, old_columns, "from_department", "") or ""),
                        str(self._value(row, old_columns, "from_position", "") or ""),
                        str(self._value(row, old_columns, "from_person", "") or ""),
                        str(self._value(row, old_columns, "to_person", "") or ""),
                        str(self._value(row, old_columns, "request_subject", "") or ""),
                        str(self._value(row, old_columns, "note", "") or ""),
                        status,
                        migrated_document,
                        created_at,
                        updated_at,
                        self._as_bool_int(self._value(row, old_columns, "is_pinned", 0)),
                    ),
                )
                imported += 1

            target_conn.commit()
            check = target_conn.execute("PRAGMA integrity_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise LegacyImportError("Новая база не прошла проверку целостности после переноса.")
            current_version = int(target_conn.execute("PRAGMA user_version").fetchone()[0])
            if current_version != Database.SCHEMA_VERSION:
                raise LegacyImportError("Не удалось привести базу к текущей версии схемы.")
            target_count = int(target_conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
            if target_count != imported:
                raise LegacyImportError("Количество записей после переноса не совпало с исходной базой.")
        except (sqlite3.Error, OSError) as error:
            target_conn.rollback()
            if isinstance(error, LegacyImportError):
                raise
            raise LegacyImportError(f"Ошибка подготовки новой базы: {error}") from error
        finally:
            target_conn.close()
            source_conn.close()

        return staged_db, staged_docs, imported, copied, missing

    def _replace_current(self, staged_db: Path, staged_docs: Path) -> None:
        db_path = self.database.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)

        staged_target_db = db_path.with_name(f".{db_path.name}.legacy_import")
        shutil.copy2(staged_db, staged_target_db)
        staged_target_db.replace(db_path)

        target_docs = self.documents.documents_dir
        temp_docs = target_docs.with_name(f".{target_docs.name}.legacy_import")
        if temp_docs.exists():
            shutil.rmtree(temp_docs)
        shutil.copytree(staged_docs, temp_docs)
        if target_docs.exists():
            shutil.rmtree(target_docs)
        temp_docs.replace(target_docs)

        # Legacy history/trash does not describe the migrated dataset. Start with
        # an empty recovery trash; the pre-import backup keeps the previous one.
        if self.documents.trash_dir.exists():
            shutil.rmtree(self.documents.trash_dir)
        self.documents.trash_dir.mkdir(parents=True, exist_ok=True)

        self.database.create_tables()
        self.database._upgrade_schema()
        self.database._configure_database()
        self.documents.clear_caches()

    def migrate(
        self,
        source_db: str | Path,
        documents_dir: str | Path | None = None,
        backup_dir: str | Path | None = None,
    ) -> LegacyImportResult:
        source = Path(source_db).expanduser().resolve(strict=False)
        explicit_docs = (
            Path(documents_dir).expanduser().resolve(strict=False)
            if documents_dir
            else None
        )
        inspection = self.inspect(source, explicit_docs)
        legacy_docs = explicit_docs or inspection.suggested_documents_dir

        backup_root = (
            Path(backup_dir).expanduser().resolve(strict=False)
            if backup_dir
            else self.database.base_dir / "backups"
        )
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_path = backup_root / f"before_legacy_import_{stamp}.zip"
        self.backup.create_backup(backup_path)

        with tempfile.TemporaryDirectory(prefix="registry_legacy_import_") as temp_name:
            staged_data = Path(temp_name) / "data"
            staged_db, staged_docs, imported, copied, missing = self._build_current_database(
                source,
                legacy_docs,
                staged_data,
            )
            try:
                self._replace_current(staged_db, staged_docs)
            except Exception as error:
                try:
                    self.backup.restore_backup(backup_path)
                    self.documents.clear_caches()
                except Exception as rollback_error:
                    raise LegacyImportError(
                        "Перенос не завершён, и автоматический откат также завершился ошибкой: "
                        f"{rollback_error}"
                    ) from error
                raise LegacyImportError(
                    f"Перенос не завершён. Текущие данные восстановлены из backup: {error}"
                ) from error

        return LegacyImportResult(
            source_db=source,
            imported_records=imported,
            copied_documents=copied,
            missing_documents=missing,
            backup_path=backup_path,
        )
