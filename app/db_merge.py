from __future__ import annotations

import logging
import os
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

        current_index = {self._business_key(r): r for r in current_records}
        rollback_index = {self._business_key(r): r for r in rollback_records}

        current_keys = set(current_index.keys())
        rollback_keys = set(rollback_index.keys())

        # Записи только в текущей БД (сохранены после отката)
        for key in current_keys - rollback_keys:
            analysis.only_in_current.append(current_index[key])

        # Записи только в rollback БД (подлежат импорту)
        for key in rollback_keys - current_keys:
            analysis.only_in_rollback.append(rollback_index[key])

        # Записи, присутствующие в обеих базах
        for key in current_keys & rollback_keys:
            cur_rec = current_index[key]
            rb_rec = rollback_index[key]
            diffs = self._compare_records(cur_rec, rb_rec)

            if diffs:
                analysis.conflicts.append(
                    MergeConflict(
                        business_key=key,
                        display_label=self._format_record_label(cur_rec),
                        current_id=cur_rec["id"],
                        rollback_id=rb_rec["id"],
                        diffs=diffs,
                    )
                )
            else:
                analysis.identical_count += 1

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

        conn = sqlite3.connect(str(self.current_db_path))
        conn.row_factory = sqlite3.Row

        rollback_conn = sqlite3.connect(str(analysis.rollback_db_path))
        rollback_conn.row_factory = sqlite3.Row
        rollback_conn.execute("PRAGMA query_only = ON")

        reserved_names: set[str] = set()

        try:
            conn.execute("BEGIN IMMEDIATE")

            # 2. Добавление записей, существующих только в сохранённой копии
            for record in analysis.only_in_rollback:
                try:
                    self._import_record(
                        conn,
                        rollback_conn,
                        record,
                        analysis.rollback_db_path,
                        reserved_names,
                    )
                    result.imported_count += 1
                except Exception as e:
                    label = self._format_record_label(record)
                    result.errors.append(f"Ошибка импорта {label}: {e}")
                    logger.exception("Import error for %s", label)

            # 3. Разрешение конфликтующих записей
            for conflict in analysis.conflicts:
                res = (conflict_resolutions or {}).get(conflict.business_key, strategy)
                try:
                    self._resolve_conflict(
                        conn,
                        rollback_conn,
                        conflict,
                        res,
                        analysis.rollback_db_path,
                        reserved_names,
                    )
                    result.conflict_resolved += 1
                except Exception as e:
                    result.errors.append(
                        f"Ошибка разрешения конфликта {conflict.display_label}: {e}"
                    )
                    logger.exception("Conflict resolution error for %s", conflict.display_label)

            conn.commit()

        except Exception as e:
            conn.rollback()
            result.success = False
            result.errors.append(f"Критическая ошибка слияния (транзакция откатана): {e}")
            logger.exception("Merge failed, rolled back")
        finally:
            rollback_conn.close()
            conn.close()

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
                    if c.is_file():
                        return c.resolve()
                except OSError:
                    continue

        # Ищем относительно rollback_db и documents
        raw = Path(text)
        candidates = [
            raw,
            rollback_db_path.parent / raw,
            rollback_db_path.parent / raw.name,
            rollback_db_path.parent / "documents" / raw.name,
            rollback_db_path.parent / "data" / "documents" / raw.name,
            rollback_db_path.parent.parent / raw.name,
            self.documents_dir / raw.name,
        ]
        for candidate in candidates:
            try:
                if candidate.is_file():
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
        dest_name = self._unique_name(self.documents_dir, source_file.name, reserved_names)
        dest_path = self.documents_dir / dest_name

        if source_file.resolve() != dest_path.resolve():
            shutil.copy2(source_file, dest_path)

        return encode_runtime_path(dest_path, self.runtime_root)

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
        values = []
        for c in cols:
            if c == "source_document":
                values.append(new_source_doc)
            else:
                values.append(record.get(c, ""))

        placeholders = ", ".join(["?"] * len(cols))
        cur = conn.execute(
            f"INSERT INTO requests ({', '.join(cols)}) VALUES ({placeholders})",
            values,
        )
        new_id = cur.lastrowid

        # Перенос истории изменений
        hist_rows = rollback_conn.execute(
            "SELECT action, description, created_at, snapshot FROM request_history WHERE request_id = ? ORDER BY id",
            (old_id,),
        ).fetchall()
        for h in hist_rows:
            conn.execute(
                "INSERT INTO request_history (request_id, action, description, created_at, snapshot) "
                "VALUES (?, ?, ?, ?, ?)",
                (new_id, h["action"], h["description"], h["created_at"], h["snapshot"]),
            )

        conn.execute(
            "INSERT INTO request_history (request_id, action, description, created_at) "
            "VALUES (?, 'merge_import', 'Запись восстановлена при слиянии из сохранённой копии БД', CURRENT_TIMESTAMP)",
            (new_id,),
        )

        # Перенос OCR-текста
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
            cur_updated = str(cur_rec["updated_at"] or cur_rec["created_at"] or "")
            rb_updated = str(rb_rec["updated_at"] or rb_rec["created_at"] or "")
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
            vals = []
            for c in cols:
                if c == "source_document":
                    vals.append(new_source_doc)
                else:
                    vals.append(rb_rec[c])
            vals.append(conflict.current_id)

            conn.execute(f"UPDATE requests SET {set_clause} WHERE id = ?", vals)

            conn.execute(
                "INSERT INTO request_history (request_id, action, description, created_at) "
                "VALUES (?, 'conflict_resolved', ?, CURRENT_TIMESTAMP)",
                (conflict.current_id, f"Конфликт разрешён в пользу версии отката ({strategy.value})"),
            )

    def _create_pre_merge_backup(self, backup_service):
        """Создать защитную копию рабочей БД перед операцией слияния."""
        if backup_service and hasattr(backup_service, "create_backup"):
            try:
                backup_service.create_backup("pre_merge")
                return
            except Exception as e:
                logger.warning("BackupService.create_backup failed: %s, falling back to direct copy", e)

        backup_file = self.current_db_path.with_name(
            f"{self.current_db_path.stem}.pre_merge_backup{self.current_db_path.suffix}"
        )
        shutil.copy2(self.current_db_path, backup_file)
