from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import shutil
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.portable_paths import decode_runtime_path, encode_runtime_path

logger = logging.getLogger(__name__)


class MergeStrategy(str, Enum):
    """Стратегия разрешения конфликтов при слиянии баз данных."""
    KEEP_CURRENT = "keep_current"      # Оставить текущее значение
    KEEP_ROLLBACK = "keep_rollback"    # Взять значение из сохранённой копии
    KEEP_NEWER = "keep_newer"          # Сравнить updated_at, оставить более свежее
    INTERACTIVE = "interactive"        # Выбор оператора для каждого конфликта


@dataclass(frozen=True, slots=True)
class FieldDiff:
    """Различие в одном поле записи."""
    field_name: str
    current_value: str
    rollback_value: str


@dataclass
class MergeConflict:
    """Конфликт: запись с тем же бизнес-ключом, но разными данными."""
    business_key: tuple
    display_label: str
    current_id: int
    rollback_id: int
    diffs: list[FieldDiff]
    resolution: Optional[MergeStrategy] = None
    current_record: dict = field(default_factory=dict)
    rollback_record: dict = field(default_factory=dict)


@dataclass
class MergeAnalysis:
    """Результат предварительного анализа слияния двух баз данных."""
    current_db_path: Path
    rollback_db_path: Path

    current_total: int = 0
    rollback_total: int = 0

    # Записи только в текущей базе (созданные после отката)
    only_in_current: list[dict] = field(default_factory=list)
    # Записи только в rollback базе (подлежащие импорту)
    only_in_rollback: list[dict] = field(default_factory=list)
    # Записи с одинаковым бизнес-ключом, но различающимися полями
    conflicts: list[MergeConflict] = field(default_factory=list)
    # Записи, данные которых полностью совпадают
    identical_count: int = 0

    @property
    def has_conflicts(self) -> bool:
        return len(self.conflicts) > 0

    @property
    def needs_merge(self) -> bool:
        """Требуется ли объединение (есть ли уникальные записи или конфликты)."""
        return len(self.only_in_rollback) > 0 or self.has_conflicts

    @property
    def summary(self) -> str:
        lines = [
            f"Текущая БД: {self.current_total} записей",
            f"Сохранённая копия: {self.rollback_total} записей",
            f"Совпадают: {self.identical_count}",
        ]
        if self.only_in_current:
            lines.append(f"Новые в текущей базе: {len(self.only_in_current)} (сохраняются)")
        if self.only_in_rollback:
            lines.append(f"Только в копии: {len(self.only_in_rollback)} (будут добавлены)")
        if self.conflicts:
            lines.append(f"Конфликты: {len(self.conflicts)} (требуют решения)")
        return "\n".join(lines)


@dataclass
class MergeResult:
    """Результат выполнения слияния."""
    success: bool = True
    imported_count: int = 0
    skipped_count: int = 0
    conflict_resolved: int = 0
    errors: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.errors:
            self.success = False

    @property
    def is_partial(self) -> bool:
        return bool(self.errors and (self.imported_count > 0 or self.conflict_resolved > 0))

    @property
    def is_full_success(self) -> bool:
        return self.success and not self.errors

    @property
    def summary(self) -> str:
        lines = [f"Добавлено записей: {self.imported_count}"]
        if self.conflict_resolved:
            lines.append(f"Разрешено конфликтов: {self.conflict_resolved}")
        if self.skipped_count:
            lines.append(f"Пропущено (без изменений): {self.skipped_count}")
        if self.errors:
            lines.append(f"Ошибок: {len(self.errors)}")
        return "\n".join(lines)


class DatabaseMergeService:
    """
    Сервис анализа и безопасного слияния (Merge) баз данных после отката.
    
    Принцип работы:
    1. analyze_merge() — сопоставление записей по бизнес-ключу без модификации файлов.
    2. execute_merge() — транзакционное слияние с предварительным защитным бэкапом.
    """

    COMPARE_FIELDS = [
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
        "is_pinned",
        "source_sha256",
        "source_size_bytes",
    ]

    def __init__(
        self,
        current_db_path: str | Path,
        runtime_root: str | Path | None = None,
        documents_dir: str | Path | None = None,
    ):
        self.current_db_path = Path(current_db_path).resolve()
        self.runtime_root = (
            Path(runtime_root).resolve()
            if runtime_root
            else self.current_db_path.parent.parent
        )
        self.documents_dir = (
            Path(documents_dir).resolve()
            if documents_dir
            else self.current_db_path.parent / "documents"
        )

    # ── Бизнес-ключ записи ────────────────────────────────────

    @staticmethod
    def _normalize_timestamp(value: object) -> str:
        """Нормализовать временную метку к единому формату UTC для корректного сравнения."""
        raw = str(value or "").strip()
        if not raw:
            return ""
        iso_candidate = raw.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso_candidate)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt.strftime("%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            return raw.replace("T", " ")

    @staticmethod
    def _business_key(record: dict) -> tuple:
        """
        Вычислить бизнес-ключ записи:
        1. Если прикреплён PDF и вычислен SHA-256 — ключ по цифровому отпечатку.
        2. Иначе — семантический кортеж реквизитов документа.
        """
        sha = str(record.get("source_sha256") or "").strip().lower()
        size = int(record.get("source_size_bytes") or 0)
        if sha:
            return ("fingerprint", sha, size)

        return (
            "semantic",
            str(record.get("received_date") or "").strip().lower(),
            str(record.get("from_department") or "").strip().lower(),
            str(record.get("from_person") or "").strip().lower(),
            str(record.get("to_person") or "").strip().lower(),
            str(record.get("request_subject") or "").strip().lower(),
        )

    @staticmethod
    def _format_record_label(record: dict) -> str:
        """Человекочитаемое представление записи для интерфейса."""
        rec_date = str(record.get("received_date") or "").strip()
        sender = (
            str(record.get("from_person") or "").strip()
            or str(record.get("from_department") or "").strip()
            or "Без автора"
        )
        recipient = str(record.get("to_person") or "").strip() or "Без адресата"
        subject = str(record.get("request_subject") or "").strip()
        if len(subject) > 45:
            subject = subject[:42] + "..."
        return f"[{rec_date}] {sender} → {recipient}: «{subject}»"

    # ── Анализ ───────────────────────────────────────────────

    def analyze_merge(self, rollback_db_path: str | Path) -> MergeAnalysis:
        """
        Сравнить рабочую БД со снимком rollback_db_path.
        
        Обеспечивает безопасность: rollback БД открывается с PRAGMA query_only = ON.
        """
        rollback_path = Path(rollback_db_path).resolve()
        analysis = MergeAnalysis(
            current_db_path=self.current_db_path,
            rollback_db_path=rollback_path,
        )

        current_records = self._load_requests(self.current_db_path)
        rollback_records = self._load_requests(rollback_path, query_only=True)

        analysis.current_total = len(current_records)
        analysis.rollback_total = len(rollback_records)

        current_grouped: dict[tuple, list[dict]] = {}
        for r in current_records:
            current_grouped.setdefault(self._business_key(r), []).append(r)

        rollback_grouped: dict[tuple, list[dict]] = {}
        for r in rollback_records:
            rollback_grouped.setdefault(self._business_key(r), []).append(r)

        all_keys = list(dict.fromkeys(list(current_grouped.keys()) + list(rollback_grouped.keys())))

        for key in all_keys:
            cur_list = current_grouped.get(key, [])
            rb_list = rollback_grouped.get(key, [])

            # Попарно сопоставляем записи с одинаковым бизнес-ключом
            min_len = min(len(cur_list), len(rb_list))
            for i in range(min_len):
                cur_rec = cur_list[i]
                rb_rec = rb_list[i]
                diffs = self._compare_records(cur_rec, rb_rec)

                if diffs:
                    analysis.conflicts.append(
                        MergeConflict(
                            business_key=key,
                            display_label=self._format_record_label(cur_rec),
                            current_id=cur_rec["id"],
                            rollback_id=rb_rec["id"],
                            diffs=diffs,
                            current_record=cur_rec,
                            rollback_record=rb_rec,
                        )
                    )
                else:
                    analysis.identical_count += 1

            # Избыточные записи в rollback БД (подлежат импорту)
            for i in range(min_len, len(rb_list)):
                analysis.only_in_rollback.append(rb_list[i])

            # Избыточные записи в текущей БД (сохранены после отката)
            for i in range(min_len, len(cur_list)):
                analysis.only_in_current.append(cur_list[i])

        logger.info(
            "Merge analysis: current=%d, rollback=%d, only_current=%d, "
            "only_rollback=%d, conflicts=%d, identical=%d",
            analysis.current_total,
            analysis.rollback_total,
            len(analysis.only_in_current),
            len(analysis.only_in_rollback),
            len(analysis.conflicts),
            analysis.identical_count,
        )
        return analysis

    # ── Выполнение слияния ───────────────────────────────────

    def execute_merge(
        self,
        analysis: MergeAnalysis,
        strategy: MergeStrategy = MergeStrategy.KEEP_CURRENT,
        conflict_resolutions: dict[tuple, MergeStrategy] | None = None,
        backup_service=None,
    ) -> MergeResult:
        """
        Выполнить слияние на основе результатов анализа.
        
        Перед модификацией обязательно создаёт точку восстановления.
        Вся операция выполняется в единой транзакции BEGIN IMMEDIATE.
        """
        result = MergeResult(skipped_count=analysis.identical_count)
        if not analysis.needs_merge:
            logger.info("Merge не требуется: базы идентичны.")
            return result

        # 1. Защитный бэкап рабочей БД
        self._create_pre_merge_backup(backup_service)

        conn = sqlite3.connect(str(self.current_db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")

        rollback_conn = sqlite3.connect(str(analysis.rollback_db_path))
        rollback_conn.row_factory = sqlite3.Row
        rollback_conn.execute("PRAGMA query_only = ON")

        reserved_names: set[str] = set()
        # Keep track of PDFs created by this merge so failed items (and a failed
        # outer transaction) cannot leave orphaned files in the live documents dir.
        self._merge_created_files: list[Path] = []

        try:
            conn.execute("BEGIN IMMEDIATE")

            # 2. Добавление записей, существующих только в сохранённой копии
            for index, record in enumerate(analysis.only_in_rollback):
                savepoint = f"merge_import_{index}"
                created_before = len(self._merge_created_files)
                conn.execute(f"SAVEPOINT {savepoint}")
                try:
                    self._import_record(
                        conn,
                        rollback_conn,
                        record,
                        analysis.rollback_db_path,
                        reserved_names,
                    )
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    result.imported_count += 1
                except Exception as e:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    self._cleanup_merge_files(created_before, reserved_names)
                    label = self._format_record_label(record)
                    result.errors.append(f"Ошибка импорта {label}: {e}")
                    logger.exception("Import error for %s", label)

            # 3. Разрешение конфликтующих записей
            for index, conflict in enumerate(analysis.conflicts):
                savepoint = f"merge_conflict_{index}"
                created_before = len(self._merge_created_files)
                conn.execute(f"SAVEPOINT {savepoint}")
                res = strategy
                if conflict_resolutions:
                    if (conflict.current_id, conflict.rollback_id) in conflict_resolutions:
                        res = conflict_resolutions[(conflict.current_id, conflict.rollback_id)]
                    elif conflict.current_id in conflict_resolutions:
                        res = conflict_resolutions[conflict.current_id]
                    elif conflict.business_key in conflict_resolutions:
                        res = conflict_resolutions[conflict.business_key]
                try:
                    self._resolve_conflict(
                        conn,
                        rollback_conn,
                        conflict,
                        res,
                        analysis.rollback_db_path,
                        reserved_names,
                    )
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    result.conflict_resolved += 1
                except Exception as e:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    self._cleanup_merge_files(created_before, reserved_names)
                    result.errors.append(
                        f"Ошибка разрешения конфликта {conflict.display_label}: {e}"
                    )
                    logger.exception("Conflict resolution error for %s", conflict.display_label)

            conn.commit()
            if result.errors:
                result.success = False
            else:
                result.success = True

        except Exception as e:
            conn.rollback()
            self._cleanup_merge_files(0, reserved_names)
            result.imported_count = 0
            result.conflict_resolved = 0
            result.success = False
            result.errors.append(f"Критическая ошибка слияния (транзакция откатана): {e}")
            logger.exception("Merge failed, rolled back")
        finally:
            rollback_conn.close()
            conn.close()
            self._merge_created_files = []

        return result

    # ── Вспомогательные методы чтения и импорта ──────────────

    def _load_requests(self, db_path: Path, query_only: bool = False) -> list[dict]:
        """Загрузить все записи таблицы requests."""
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        if query_only:
            conn.execute("PRAGMA query_only = ON")
        try:
            cursor = conn.execute("PRAGMA table_info(requests)")
            columns = [row[1] for row in cursor.fetchall()]
            rows = conn.execute(
                f"SELECT {', '.join(columns)} FROM requests ORDER BY id"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def _compare_records(self, current: dict, rollback: dict) -> list[FieldDiff]:
        """Сравнить две версии записи по значимым полям."""
        diffs = []
        for field_name in self.COMPARE_FIELDS:
            cur_val = current.get(field_name, "")
            rb_val = rollback.get(field_name, "")
            cur_str = "" if cur_val is None else str(cur_val).strip()
            rb_str = "" if rb_val is None else str(rb_val).strip()
            if cur_str != rb_str:
                diffs.append(FieldDiff(field_name, cur_str, rb_str))
        return diffs

    def _resolve_source_file(
        self,
        reference: str,
        rollback_db_path: Path,
    ) -> Path | None:
        """Найти физический файл документа для записи из сохранённой копии."""
        text = (reference or "").strip()
        if not text:
            return None

        # Пробуем декодировать как портабельный токен
        if text.startswith("@runtime/"):
            decoded = decode_runtime_path(text, self.runtime_root)
            if decoded and decoded.is_file():
                return decoded
            subpath = text[len("@runtime/"):].lstrip("/\\")
            token_candidates = [
                self.runtime_root / subpath,
                rollback_db_path.parent / subpath,
                rollback_db_path.parent.parent / subpath,
                rollback_db_path.parent / Path(subpath).name,
                self.documents_dir / Path(subpath).name,
            ]
            for c in token_candidates:
                try:
                    if c.is_file() and c.suffix.lower() == ".pdf":
                        return c.resolve()
                except OSError:
                    continue

        # Ищем относительно rollback_db и documents (блокируем выход за пределы через .. и не-PDF)
        raw = Path(text)
        has_traversal = ".." in raw.parts
        candidates: list[Path] = []
        if not has_traversal:
            candidates.extend([
                raw,
                rollback_db_path.parent / raw,
            ])
        if raw.name:
            candidates.extend([
                rollback_db_path.parent / raw.name,
                rollback_db_path.parent / "documents" / raw.name,
                rollback_db_path.parent / "data" / "documents" / raw.name,
                rollback_db_path.parent.parent / raw.name,
                self.documents_dir / raw.name,
            ])
        for candidate in candidates:
            try:
                if candidate.is_file() and candidate.suffix.lower() == ".pdf":
                    return candidate.resolve()
            except OSError:
                continue
        return None

    def _copy_document_to_runtime(
        self,
        source_file: Path,
        reserved_names: set[str],
    ) -> str:
        """Скопировать PDF-файл в локальный каталог documents с уникальным именем."""
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        safe_source_name = Path(source_file.name).name or "document.pdf"
        candidate_dest = self.documents_dir / safe_source_name

        # After a version rollback the source PDF may already be stored in the
        # active documents directory. Check identity BEFORE reserving a unique
        # name; otherwise the existing file makes _unique_name return "(2).pdf"
        # and the old check below can never detect the same-file case.
        try:
            if source_file.resolve() == candidate_dest.resolve():
                return encode_runtime_path(candidate_dest, self.runtime_root)
        except OSError:
            # Continue through the normal copy path; Path.resolve failures must
            # not turn an unverified file into a successful same-file match.
            pass

        dest_name = self._unique_name(self.documents_dir, safe_source_name, reserved_names)
        dest_path = self.documents_dir / dest_name
        created_files = getattr(self, "_merge_created_files", None)
        if created_files is not None:
            # Register before copying so a partially written destination is
            # also removed if copy2 raises midway through the operation.
            created_files.append(dest_path)
        shutil.copy2(source_file, dest_path)

        return encode_runtime_path(dest_path, self.runtime_root)

    def _cleanup_merge_files(self, start_index: int, reserved_names: set[str]) -> None:
        """Remove files created by failed merge items and release their names."""
        created_files = getattr(self, "_merge_created_files", [])
        for path in reversed(created_files[start_index:]):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Не удалось удалить PDF после отката слияния: %s", path)
            reserved_names.discard(path.name.casefold())
        del created_files[start_index:]

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

    def _import_record(
        self,
        conn: sqlite3.Connection,
        rollback_conn: sqlite3.Connection,
        record: dict,
        rollback_db_path: Path,
        reserved_names: set[str],
    ) -> int:
        """Импортировать одну запись из rollback в текущую базу со всеми дочерними данными."""
        old_id = record["id"]

        # Копирование документа, если он существует
        source_doc_ref = record.get("source_document", "")
        new_source_doc = source_doc_ref
        found_file = self._resolve_source_file(source_doc_ref, rollback_db_path)
        if found_file:
            new_source_doc = self._copy_document_to_runtime(found_file, reserved_names)

        # Read the destination schema at runtime. A hard-coded column list
        # silently discards fields introduced by newer migrations when merging
        # a database from a later version. Preserve every column present in both
        # the destination schema and the source record, while retaining explicit
        # safe defaults for the long-standing request fields.
        standard_cols = {
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
            "is_pinned",
            "source_sha256",
            "source_size_bytes",
        }
        destination_columns = [
            str(row[1])
            for row in conn.execute("PRAGMA table_info(requests)").fetchall()
            if str(row[1]) != "id"
        ]
        record_keys = set(record)
        cols = [
            column for column in destination_columns
            if column in record_keys or column in standard_cols
        ]
        values = []
        for column in cols:
            value = record.get(column)
            if column == "source_document":
                values.append(new_source_doc or "")
            elif column == "is_pinned":
                values.append(int(value or 0))
            elif column == "source_size_bytes":
                values.append(int(value or 0))
            elif column == "source_sha256":
                values.append(str(value or ""))
            elif column == "received_date":
                values.append(str(value or record.get("created_at") or datetime.now().strftime("%d.%m.%Y")))
            elif column == "status":
                values.append(str(value or "Новый"))
            elif column in standard_cols:
                values.append(str(value or ""))
            else:
                # Newly migrated columns keep their original SQLite value/type;
                # omitted columns not present in the source use schema defaults.
                values.append(value)

        placeholders = ", ".join(["?"] * len(cols))
        cur = conn.execute(
            f"INSERT INTO requests ({', '.join(cols)}) VALUES ({placeholders})",
            values,
        )
        new_id = cur.lastrowid

        rollback_tables = {
            row[0]
            for row in rollback_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

        # Перенос истории изменений с проверкой наличия таблицы и колонки snapshot
        if "request_history" in rollback_tables:
            hist_columns = {
                row[1]
                for row in rollback_conn.execute("PRAGMA table_info(request_history)").fetchall()
            }
            if "snapshot" in hist_columns:
                hist_sql = "SELECT action, description, created_at, snapshot FROM request_history WHERE request_id = ? ORDER BY id"
            else:
                hist_sql = "SELECT action, description, created_at, NULL as snapshot FROM request_history WHERE request_id = ? ORDER BY id"

            hist_rows = rollback_conn.execute(hist_sql, (old_id,)).fetchall()
            for h in hist_rows:
                raw_snap = h["snapshot"]
                updated_snap = raw_snap
                if raw_snap:
                    try:
                        parsed = json.loads(raw_snap)
                        if isinstance(parsed, dict) and "id" in parsed:
                            parsed["id"] = new_id
                            if new_source_doc and parsed.get("source_document"):
                                parsed["source_document"] = new_source_doc
                            updated_snap = json.dumps(parsed, ensure_ascii=False)
                    except Exception:
                        pass

                conn.execute(
                    "INSERT INTO request_history (request_id, action, description, created_at, snapshot) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (new_id, h["action"], h["description"], h["created_at"], updated_snap),
                )

        conn.execute(
            "INSERT INTO request_history (request_id, action, description, created_at) "
            "VALUES (?, 'merge_import', 'Запись восстановлена при слиянии из сохранённой копии БД', CURRENT_TIMESTAMP)",
            (new_id,),
        )

        # Перенос OCR-текста
        if "request_search_content" in rollback_tables:
            ocr_row = rollback_conn.execute(
                "SELECT ocr_text FROM request_search_content WHERE request_id = ?",
                (old_id,),
            ).fetchone()
            if ocr_row and ocr_row["ocr_text"]:
                conn.execute(
                    "INSERT OR REPLACE INTO request_search_content (request_id, ocr_text, indexed_at) "
                    "VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (new_id, ocr_row["ocr_text"]),
                )

        # Перенос метаданных парсера
        if "request_parser_metadata" in rollback_tables:
            meta_row = rollback_conn.execute(
                "SELECT parser_version, diagnostics_json FROM request_parser_metadata WHERE request_id = ?",
                (old_id,),
            ).fetchone()
            if meta_row:
                conn.execute(
                    "INSERT OR REPLACE INTO request_parser_metadata (request_id, parser_version, diagnostics_json, updated_at) "
                    "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                    (new_id, meta_row["parser_version"], meta_row["diagnostics_json"]),
                )

        # Перенос связей с делами (cases / case_requests)
        current_tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if {"cases", "case_requests"}.issubset(rollback_tables) and {"cases", "case_requests"}.issubset(current_tables):
            case_rows = rollback_conn.execute(
                "SELECT c.title, c.note, c.created_at, c.updated_at, cr.added_at "
                "FROM case_requests cr JOIN cases c ON c.id = cr.case_id "
                "WHERE cr.request_id = ? ORDER BY c.id",
                (old_id,),
            ).fetchall()
            for c_row in case_rows:
                title = str(c_row["title"] or "").strip()
                if not title:
                    continue
                existing_case = conn.execute(
                    "SELECT id FROM cases WHERE title = ? LIMIT 1", (title,)
                ).fetchone()
                if existing_case:
                    target_case_id = int(existing_case["id"])
                else:
                    cur_case = conn.execute(
                        "INSERT INTO cases (title, note, created_at, updated_at) "
                        "VALUES (?, ?, COALESCE(?, CURRENT_TIMESTAMP), COALESCE(?, CURRENT_TIMESTAMP))",
                        (title, str(c_row["note"] or ""), c_row["created_at"], c_row["updated_at"]),
                    )
                    target_case_id = int(cur_case.lastrowid)
                conn.execute(
                    "INSERT OR IGNORE INTO case_requests (case_id, request_id, added_at) "
                    "VALUES (?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
                    (target_case_id, new_id, c_row["added_at"]),
                )

        return new_id

    def _resolve_conflict(
        self,
        conn: sqlite3.Connection,
        rollback_conn: sqlite3.Connection,
        conflict: MergeConflict,
        strategy: MergeStrategy,
        rollback_db_path: Path,
        reserved_names: set[str],
    ):
        """Разрешить конфликт по выбранной стратегии."""
        if strategy == MergeStrategy.KEEP_CURRENT:
            return

        rb_rec = rollback_conn.execute(
            "SELECT * FROM requests WHERE id = ?", (conflict.rollback_id,)
        ).fetchone()
        cur_rec = conn.execute(
            "SELECT * FROM requests WHERE id = ?", (conflict.current_id,)
        ).fetchone()

        if not rb_rec or not cur_rec:
            return

        should_apply_rollback = False
        if strategy == MergeStrategy.KEEP_ROLLBACK:
            should_apply_rollback = True
        elif strategy == MergeStrategy.KEEP_NEWER:
            cur_updated = self._normalize_timestamp(cur_rec["updated_at"] or cur_rec["created_at"])
            rb_updated = self._normalize_timestamp(rb_rec["updated_at"] or rb_rec["created_at"])
            if rb_updated > cur_updated:
                should_apply_rollback = True

        if should_apply_rollback:
            source_doc_ref = rb_rec["source_document"] or ""
            new_source_doc = source_doc_ref
            found_file = self._resolve_source_file(source_doc_ref, rollback_db_path)
            if found_file:
                new_source_doc = self._copy_document_to_runtime(found_file, reserved_names)

            cols = [
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
                "is_pinned",
                "source_sha256",
                "source_size_bytes",
            ]
            set_clause = ", ".join([f"{c} = ?" for c in cols]) + ", updated_at = CURRENT_TIMESTAMP"
            rb_keys = set(rb_rec.keys())
            vals = []
            for c in cols:
                val = rb_rec[c] if c in rb_keys else None
                if c == "source_document":
                    vals.append(new_source_doc or "")
                elif c == "is_pinned":
                    vals.append(int(val or 0))
                elif c == "source_size_bytes":
                    vals.append(int(val or 0))
                elif c == "source_sha256":
                    vals.append(str(val or ""))
                elif c == "received_date":
                    vals.append(str(val or cur_rec["received_date"] or datetime.now().strftime("%d.%m.%Y")))
                elif c == "status":
                    vals.append(str(val or "Новый"))
                else:
                    vals.append(str(val or ""))
            vals.append(conflict.current_id)

            conn.execute(f"UPDATE requests SET {set_clause} WHERE id = ?", vals)

            cur_snap_json = json.dumps(dict(cur_rec), ensure_ascii=False) if cur_rec else None
            conn.execute(
                "INSERT INTO request_history (request_id, action, description, snapshot, created_at) "
                "VALUES (?, 'conflict_resolved', ?, ?, CURRENT_TIMESTAMP)",
                (conflict.current_id, f"Конфликт разрешён в пользу версии отката ({strategy.value})", cur_snap_json),
            )

            rollback_tables = {
                row[0]
                for row in rollback_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }

            current_tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            ocr_row = (
                rollback_conn.execute(
                    "SELECT ocr_text FROM request_search_content WHERE request_id = ?",
                    (conflict.rollback_id,),
                ).fetchone()
                if "request_search_content" in rollback_tables
                else None
            )
            if ocr_row and ocr_row["ocr_text"]:
                conn.execute(
                    "INSERT OR REPLACE INTO request_search_content (request_id, ocr_text, indexed_at) "
                    "VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (conflict.current_id, ocr_row["ocr_text"]),
                )
            elif "request_search_content" in current_tables:
                conn.execute(
                    "DELETE FROM request_search_content WHERE request_id = ?",
                    (conflict.current_id,),
                )

            meta_row = (
                rollback_conn.execute(
                    "SELECT parser_version, diagnostics_json FROM request_parser_metadata WHERE request_id = ?",
                    (conflict.rollback_id,),
                ).fetchone()
                if "request_parser_metadata" in rollback_tables
                else None
            )
            if meta_row:
                conn.execute(
                    "INSERT OR REPLACE INTO request_parser_metadata (request_id, parser_version, diagnostics_json, updated_at) "
                    "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                    (conflict.current_id, meta_row["parser_version"], meta_row["diagnostics_json"]),
                )
            elif "request_parser_metadata" in current_tables:
                conn.execute(
                    "DELETE FROM request_parser_metadata WHERE request_id = ?",
                    (conflict.current_id,),
                )

    def _create_pre_merge_backup(self, backup_service):
        """Создать уникальную защитную копию рабочей БД перед слиянием."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        if backup_service and hasattr(backup_service, "create_backup"):
            try:
                backup_service.create_backup(f"pre_merge_{stamp}")
                return
            except Exception as e:
                logger.warning("BackupService.create_backup failed: %s, falling back to direct copy", e)

        backup_file = self.current_db_path.with_name(
            f"{self.current_db_path.stem}.pre_merge_backup_{stamp}{self.current_db_path.suffix}"
        )
        try:
            with closing(sqlite3.connect(str(self.current_db_path), timeout=30.0)) as src, closing(sqlite3.connect(str(backup_file))) as dst:
                src.backup(dst)
        except Exception:
            try:
                with closing(sqlite3.connect(str(self.current_db_path), timeout=10.0)) as ck_conn:
                    ck_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            shutil.copy2(self.current_db_path, backup_file)
