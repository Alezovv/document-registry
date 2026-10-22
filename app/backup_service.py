from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile
from typing import Callable

from app.archive_safety import validate_archive_member_paths
from app.database import Database
from app.db_migrations import CURRENT_SCHEMA_VERSION, migrate_database
from app.document_service import DocumentService
from app.updater_main import (
    MAX_ARCHIVE_FILES,
    MAX_ARCHIVE_TOTAL_BYTES,
    MAX_ARCHIVE_SINGLE_FILE_BYTES,
)



BACKUP_FORMAT_VERSION = 1
_BACKUP_FREE_SPACE_RESERVE = 64 * 1024 * 1024


class BackupError(RuntimeError):
    pass


class BackupCancelled(BackupError):
    """Raised when a cooperative cancellation is requested before commit."""


@dataclass(frozen=True, slots=True)
class BackupResult:
    path: Path
    document_count: int
    trash_count: int
    size_bytes: int
    verified: bool = False
    verified_at: str = ""


class BackupService:
    """Creates, verifies and restores self-contained application backups.

    SQLite is copied through the online backup API, so a backup is consistent
    even if WAL mode is enabled.  Verification deliberately streams archive
    entries and extracts only the database to a temporary directory; document
    bodies are never accumulated in RAM.
    """

    def __init__(self, database: Database, documents: DocumentService):
        self.database = database
        self.documents = documents
        self.health_path = Path(self.database.db_path).parent / "backup_health.json"

    @staticmethod
    def is_backup_due(backup_path: str | Path, interval_days: int = 7) -> bool:
        """Return True when the single rolling auto-backup should be refreshed."""
        path = Path(backup_path)
        if not path.is_file():
            return True
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return True
        return datetime.now(timezone.utc) - modified >= timedelta(days=max(1, int(interval_days)))

    @staticmethod
    def backup_age_days(backup_path: str | Path) -> int | None:
        path = Path(backup_path)
        if not path.is_file():
            return None
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None
        return max(0, (datetime.now(timezone.utc) - modified).days)

    @property
    def default_backups_dir(self) -> Path:
        data_dir = Path(self.database.db_path).parent
        if data_dir.name.lower() == "data":
            return (data_dir.parent / "backups").resolve()
        return (data_dir / "backups").resolve()

    def create_backup(
        self,
        destination: str | Path,
        *,
        update_health: bool = True,
        should_cancel: Callable[[], bool] | None = None,
    ) -> BackupResult:
        self._check_cancelled(should_cancel)
        dest_path = Path(destination).expanduser()
        if not dest_path.is_absolute():
            destination = (self.default_backups_dir / dest_path).resolve(strict=False)
        else:
            destination = dest_path.resolve(strict=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.suffix.lower() != ".zip":
            destination = destination.with_suffix(".zip")

        document_paths = [entry.path for entry in self.documents.list_document_entries()]
        trash_paths = (
            [p for p in self.documents.trash_dir.rglob("*") if p.is_file()]
            if self.documents.trash_dir.exists()
            else []
        )
        self._ensure_free_space(destination.parent, document_paths, trash_paths)

        with tempfile.TemporaryDirectory(prefix="registry_backup_") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            db_snapshot = temp_dir / "requests.db"
            self._snapshot_database(db_snapshot)

            manifest = {
                "format_version": BACKUP_FORMAT_VERSION,
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "document_count": len(document_paths),
                "trash_count": len(trash_paths),
                "documents_dir_name": "documents",
            }

            # Use a unique temporary file in the destination directory. A fixed
            # ``.tmp`` name lets concurrent manual/automatic backups overwrite
            # each other's in-progress ZIP archives.
            temp_fd, temp_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
            )
            os.close(temp_fd)
            temp_zip = Path(temp_name)
            try:
                with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                    self._write_file_to_archive(
                        archive, db_snapshot, "database/requests.db", should_cancel
                    )
                    archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                    for path in document_paths:
                        self._write_file_to_archive(
                            archive, path, f"documents/{path.name}", should_cancel
                        )
                    for path in trash_paths:
                        relative = path.relative_to(self.documents.trash_dir)
                        self._write_file_to_archive(
                            archive, path, (Path("trash") / relative).as_posix(), should_cancel
                        )
                # Validate the temporary archive before replacing a previous
                # backup. If cancellation or verification fails, the old backup
                # remains untouched.
                verification = self.verify_backup(temp_zip, should_cancel=should_cancel)
                self._check_cancelled(should_cancel)
                temp_zip.replace(destination)
            except BackupCancelled:
                temp_zip.unlink(missing_ok=True)
                raise
            except Exception as error:
                temp_zip.unlink(missing_ok=True)
                raise BackupError(f"Не удалось создать резервную копию: {error}") from error

        # The temporary archive and embedded database were validated before
        # the final atomic replace above.
        try:
            size_bytes = destination.stat().st_size
        except OSError:
            size_bytes = 0
        verified_at = str(verification.get("verified_at", ""))
        result = BackupResult(
            destination,
            len(document_paths),
            len(trash_paths),
            size_bytes,
            verified=True,
            verified_at=verified_at,
        )
        if update_health:
            self._write_health(result)
        return result

    def inspect_backup(self, backup_path: str | Path) -> dict:
        path = Path(backup_path).expanduser().resolve(strict=False)
        try:
            with zipfile.ZipFile(path, "r") as archive:
                members = archive.infolist()
                if len(members) > MAX_ARCHIVE_FILES:
                    raise BackupError(
                        f"В архиве резервной копии слишком много файлов ({len(members)} > {MAX_ARCHIVE_FILES})."
                    )
                names = {info.filename for info in members}
                if "manifest.json" not in names or "database/requests.db" not in names:
                    raise BackupError("Архив не является резервной копией этого приложения.")
                manifest_info = archive.getinfo("manifest.json")
                max_manifest_bytes = 5 * 1024 * 1024
                if manifest_info.file_size > max_manifest_bytes:
                    raise BackupError("Манифест резервной копии превышает допустимый размер.")
                with archive.open("manifest.json", "r") as mf:
                    manifest_bytes = mf.read(max_manifest_bytes + 1)
                if len(manifest_bytes) > max_manifest_bytes:
                    raise BackupError("Манифест резервной копии превышает допустимый размер.")
                manifest = json.loads(manifest_bytes.decode("utf-8"))
                if int(manifest.get("format_version", 0)) != BACKUP_FORMAT_VERSION:
                    raise BackupError("Версия резервной копии не поддерживается.")
                return manifest
        except BackupError:
            raise
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise BackupError(f"Не удалось прочитать резервную копию: {error}") from error

    def verify_backup(
        self,
        backup_path: str | Path,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        """Deep-verify archive readability, file counts and the embedded SQLite DB."""
        self._check_cancelled(should_cancel)
        path = Path(backup_path).expanduser().resolve(strict=False)
        manifest = self.inspect_backup(path)
        try:
            with zipfile.ZipFile(path, "r") as archive:
                members = archive.infolist()
                if len(members) > MAX_ARCHIVE_FILES:
                    raise BackupError(
                        f"В архиве резервной копии слишком много файлов ({len(members)} > {MAX_ARCHIVE_FILES})."
                    )
                declared_total = 0
                for member in members:
                    self._check_cancelled(should_cancel)
                    if member.file_size > MAX_ARCHIVE_SINGLE_FILE_BYTES:
                        raise BackupError(
                            f"Файл '{member.filename}' в архиве превышает допустимый размер "
                            f"({member.file_size} > {MAX_ARCHIVE_SINGLE_FILE_BYTES} байт)."
                        )
                    declared_total += member.file_size
                    if declared_total > MAX_ARCHIVE_TOTAL_BYTES:
                        raise BackupError(
                            f"Общий распакованный размер архива ({declared_total} байт) "
                            f"превышает лимит ({MAX_ARCHIVE_TOTAL_BYTES} байт)."
                        )
                # Bounded stream verification (replaces unbounded testzip() to prevent Zip-bomb DoS)
                total_verified = 0
                for member in members:
                    self._check_cancelled(should_cancel)
                    if member.is_dir():
                        continue
                    member_read = 0
                    try:
                        with archive.open(member, "r") as src:
                            while chunk := src.read(65536):
                                self._check_cancelled(should_cancel)
                                member_read += len(chunk)
                                total_verified += len(chunk)
                                if member_read > MAX_ARCHIVE_SINGLE_FILE_BYTES:
                                    raise BackupError(
                                        f"Файл '{member.filename}' в архиве превышает допустимый размер "
                                        f"({member_read} > {MAX_ARCHIVE_SINGLE_FILE_BYTES} байт)."
                                    )
                                if total_verified > MAX_ARCHIVE_TOTAL_BYTES:
                                    raise BackupError(
                                        f"Общий распакованный размер архива ({total_verified} байт) "
                                        f"превышает лимит ({MAX_ARCHIVE_TOTAL_BYTES} байт)."
                                    )
                    except (zipfile.BadZipFile, OSError) as crc_err:
                        raise BackupError(f"Архив повреждён: {member.filename} ({crc_err})") from crc_err

                files = [info.filename for info in members if not info.is_dir()]
                document_count = sum(1 for name in files if name.startswith("documents/"))
                trash_count = sum(1 for name in files if name.startswith("trash/"))
                expected_documents = int(manifest.get("document_count", 0) or 0)
                expected_trash = int(manifest.get("trash_count", 0) or 0)
                if document_count != expected_documents:
                    raise BackupError(
                        f"Число документов в backup не совпадает с manifest: {document_count} != {expected_documents}."
                    )
                if trash_count != expected_trash:
                    raise BackupError(
                        f"Число файлов корзины в backup не совпадает с manifest: {trash_count} != {expected_trash}."
                    )
                with tempfile.TemporaryDirectory(prefix="registry_backup_verify_") as temp_dir_name:
                    db_path = Path(temp_dir_name) / "requests.db"
                    # Stream only the embedded DB to disk with size enforcement.
                    db_written = 0
                    with archive.open("database/requests.db", "r") as source, db_path.open("wb") as target:
                        while chunk := source.read(65536):
                            db_written += len(chunk)
                            if db_written > MAX_ARCHIVE_SINGLE_FILE_BYTES:
                                raise BackupError("База данных в архиве превышает допустимый размер.")
                            target.write(chunk)
                    self._validate_database(db_path)
        except BackupError:
            raise
        except (OSError, zipfile.BadZipFile) as error:
            raise BackupError(f"Проверка резервной копии не выполнена: {error}") from error

        return {
            "ok": True,
            "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "document_count": int(manifest.get("document_count", 0) or 0),
            "trash_count": int(manifest.get("trash_count", 0) or 0),
        }

    def verification_info(self) -> dict[str, object]:
        try:
            payload = json.loads(self.health_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def restore_backup(
        self,
        backup_path: str | Path,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        path = Path(backup_path).expanduser().resolve(strict=False)
        # Cancellation is allowed only during preflight/extraction. Once the live
        # database and document directories start swapping, rollback must finish.
        self._check_cancelled(should_cancel)
        self.verify_backup(path, should_cancel=should_cancel)

        with tempfile.TemporaryDirectory(prefix="registry_restore_") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            extracted = temp_dir / "incoming"
            extracted.mkdir()
            try:
                with zipfile.ZipFile(path, "r") as archive:
                    self._safe_extract(archive, extracted, should_cancel=should_cancel)
            except Exception as error:
                if isinstance(error, BackupError):
                    raise
                raise BackupError(f"Не удалось распаковать резервную копию: {error}") from error

            self._check_cancelled(should_cancel)
            incoming_db = extracted / "database" / "requests.db"
            incoming_schema = self._validate_database(incoming_db)
            if incoming_schema > CURRENT_SCHEMA_VERSION:
                raise BackupError(
                    f"Резервная копия создана в более новой версии программы (схема {incoming_schema} > {CURRENT_SCHEMA_VERSION}). "
                    "Обновите программу перед восстановлением."
                )
            if incoming_schema < CURRENT_SCHEMA_VERSION:
                try:
                    migrate_database(incoming_db, target_schema=CURRENT_SCHEMA_VERSION)
                except Exception as error:
                    raise BackupError(
                        f"Не удалось обновить схему базы данных из резервной копии ({incoming_schema} -> {CURRENT_SCHEMA_VERSION}): {error}"
                    ) from error
                self._validate_database(incoming_db)

            rollback = temp_dir / "rollback"
            rollback.mkdir()
            rollback_db = rollback / "requests.db"
            self._snapshot_database(rollback_db)
            rollback_docs = rollback / "documents"
            rollback_trash = rollback / "trash"
            if self.documents.documents_dir.exists():
                shutil.copytree(self.documents.documents_dir, rollback_docs)
            if self.documents.trash_dir.exists():
                shutil.copytree(self.documents.trash_dir, rollback_trash)

            # This is the final cancellation point. From here onward, the live
            # swap/rollback sequence must run to completion to preserve a
            # consistent database + document set.
            self._check_cancelled(should_cancel)
            try:
                self._replace_database(incoming_db)
                self._replace_directory(extracted / "documents", self.documents.documents_dir)
                self._replace_directory(extracted / "trash", self.documents.trash_dir)
                self.documents.repair_stored_document_paths(preserve_runtime_spelling=False)
            except Exception as error:
                try:
                    self._replace_database(rollback_db)
                    self._replace_directory(rollback_docs, self.documents.documents_dir)
                    self._replace_directory(rollback_trash, self.documents.trash_dir)
                except Exception as rollback_error:
                    raise BackupError(
                        f"Восстановление не удалось, а автоматический откат завершился ошибкой: {rollback_error}"
                    ) from error
                raise BackupError(f"Не удалось восстановить резервную копию: {error}") from error

    @staticmethod
    def _check_cancelled(should_cancel: Callable[[], bool] | None) -> None:
        if should_cancel is not None and should_cancel():
            raise BackupCancelled("Операция резервного копирования отменена.")

    @classmethod
    def _write_file_to_archive(
        cls,
        archive: zipfile.ZipFile,
        source_path: Path,
        archive_name: str,
        should_cancel: Callable[[], bool] | None,
    ) -> None:
        """Write one file in bounded chunks so large PDFs can be cancelled."""
        cls._check_cancelled(should_cancel)
        info = zipfile.ZipInfo(archive_name)
        info.compress_type = zipfile.ZIP_DEFLATED
        with source_path.open("rb") as source, archive.open(info, "w") as target:
            while chunk := source.read(1024 * 1024):
                cls._check_cancelled(should_cancel)
                target.write(chunk)

    def _ensure_free_space(self, destination_dir: Path, documents: list[Path], trash: list[Path]) -> None:
        def safe_size(path: Path) -> int:
            try:
                return int(path.stat().st_size)
            except OSError:
                return 0

        # Compression makes the final archive normally smaller, but reserve the
        # uncompressed payload plus headroom. This prevents predictable ENOSPC
        # failures without reading file contents into memory.
        estimated = safe_size(Path(self.database.db_path))
        estimated += sum(safe_size(path) for path in documents)
        estimated += sum(safe_size(path) for path in trash)
        required = estimated + max(_BACKUP_FREE_SPACE_RESERVE, estimated // 10)
        try:
            free = int(shutil.disk_usage(destination_dir).free)
        except OSError:
            return
        if free < required:
            need_mb = required / (1024 * 1024)
            free_mb = free / (1024 * 1024)
            raise BackupError(
                f"Недостаточно свободного места для backup: требуется примерно {need_mb:.0f} МБ, "
                f"доступно {free_mb:.0f} МБ."
            )

    def _write_health(self, result: BackupResult) -> None:
        payload = {
            "ok": bool(result.verified),
            "path": str(result.path),
            "name": result.path.name,
            "verified_at": result.verified_at,
            "size_bytes": int(result.size_bytes),
            "document_count": int(result.document_count),
            "trash_count": int(result.trash_count),
        }
        temp: Path | None = None
        temp_fd: int | None = None
        try:
            self.health_path.parent.mkdir(parents=True, exist_ok=True)
            temp_fd, temp_name = tempfile.mkstemp(
                prefix=f".{self.health_path.name}.", suffix=".tmp", dir=self.health_path.parent
            )
            temp = Path(temp_name)
            with os.fdopen(temp_fd, "w", encoding="utf-8", newline="\n") as stream:
                temp_fd = None  # fdopen now owns and closes the descriptor
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            temp.replace(self.health_path)
        except OSError:
            # Backup is already valid; inability to persist informational health
            # metadata must not turn it into a failed backup.
            pass
        finally:
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except OSError:
                    pass
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    # Health metadata is best-effort; leave an orphaned temp file
                    # rather than turning a verified backup into a failure.
                    pass

    def _snapshot_database(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            source = sqlite3.connect(self.database.db_path, timeout=10.0)
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
        except sqlite3.Error as error:
            raise BackupError(f"Не удалось создать снимок базы данных: {error}") from error

    @staticmethod
    def _validate_database(path: Path) -> int:
        if not path.is_file():
            raise BackupError("В резервной копии отсутствует база данных.")
        try:
            conn = sqlite3.connect(path)
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()
                if not result or str(result[0]).lower() != "ok":
                    raise BackupError("Проверка целостности резервной базы данных не пройдена.")
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "requests" not in tables or "request_history" not in tables:
                    raise BackupError("В резервной базе нет обязательных таблиц.")
                version_row = conn.execute("PRAGMA user_version").fetchone()
                return int(version_row[0]) if version_row else 0
            finally:
                conn.close()
        except sqlite3.Error as error:
            raise BackupError(f"Резервная база данных повреждена: {error}") from error

    def _replace_database(self, source: Path) -> None:
        """Restore a validated SQLite snapshot without deleting live WAL files.

        When the database already exists, SQLite's online backup API updates it
        transactionally. If that operation fails (for example, because another
        connection holds a lock), do not fall back to deleting ``-wal``/``-shm``
        and replacing the file underneath those connections. A missing database
        is installed through a validated same-directory temporary file.
        """
        live_db = Path(self.database.db_path)
        live_db.parent.mkdir(parents=True, exist_ok=True)
        if not source.is_file():
            raise BackupError("Не найден файл базы данных для восстановления.")

        if live_db.exists() and not live_db.is_file():
            raise BackupError("Путь рабочей базы данных занят не файлом.")

        if live_db.is_file():
            source_conn = None
            target_conn = None
            try:
                source_conn = sqlite3.connect(source, timeout=10.0)
                target_conn = sqlite3.connect(live_db, timeout=15.0)
                target_conn.execute("PRAGMA busy_timeout = 10000")
                source_conn.backup(target_conn)
            except sqlite3.Error as error:
                raise BackupError(
                    "Не удалось безопасно восстановить рабочую базу данных: "
                    f"{error}. Служебные файлы WAL/SHM не удалялись."
                ) from error
            finally:
                if target_conn is not None:
                    target_conn.close()
                if source_conn is not None:
                    source_conn.close()

            # Verify the effective database after closing the destination handle;
            # in WAL mode SQLite will read committed pages from its WAL as needed.
            self._validate_database(live_db)
            return

        # If the main DB is missing while WAL/SHM sidecars remain, do not guess
        # whether another process still owns them. Stop without destroying them.
        sidecars = [Path(str(live_db) + suffix) for suffix in ("-wal", "-shm")]
        if any(path.exists() for path in sidecars):
            raise BackupError(
                "Основной файл базы отсутствует, но остались WAL/SHM-файлы. "
                "Восстановление остановлено, чтобы не повредить данные."
            )

        temp_fd, temp_name = tempfile.mkstemp(
            prefix=f".{live_db.name}.", suffix=".restore", dir=live_db.parent
        )
        os.close(temp_fd)
        temp_target = Path(temp_name)
        try:
            shutil.copy2(source, temp_target)
            self._validate_database(temp_target)
            temp_target.replace(live_db)
        except Exception as error:
            raise BackupError(f"Не удалось установить резервную базу данных: {error}") from error
        finally:
            try:
                temp_target.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _replace_file(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f".{target.name}.restore")
        shutil.copy2(source, staged)
        staged.replace(target)

    @staticmethod
    def _replace_directory(source: Path, target: Path) -> None:
        """Stage a directory and swap it in without deleting the live copy first.

        If the second rename fails, restore the old directory. This also avoids
        partially deleting a directory when a file is locked by another process.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f".{target.name}.restore")
        previous = target.with_name(f".{target.name}.previous")
        # Recover the only remaining copy if a previous process crashed between
        # renaming the live directory aside and installing the staged directory.
        # Never delete ``.previous`` while the live target is missing.
        if previous.exists():
            if not target.exists():
                previous.replace(target)
            else:
                shutil.rmtree(previous)
        if staged.exists():
            shutil.rmtree(staged)
        if source.exists():
            shutil.copytree(source, staged)
        else:
            staged.mkdir(parents=True)

        moved_old = False
        try:
            if target.exists():
                target.replace(previous)
                moved_old = True
            staged.replace(target)
        except Exception:
            if moved_old and previous.exists() and not target.exists():
                previous.replace(target)
            raise
        else:
            if previous.exists():
                # The new directory is already installed; a locked file in the
                # old directory must not turn a successful swap into a failure.
                shutil.rmtree(previous, ignore_errors=True)
        finally:
            if staged.exists():
                shutil.rmtree(staged, ignore_errors=True)

    @staticmethod
    def _safe_extract(
        archive: zipfile.ZipFile,
        destination: Path,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_FILES:
            raise BackupError(
                f"В архиве резервной копии слишком много файлов ({len(members)} > {MAX_ARCHIVE_FILES})."
            )
        member_paths = validate_archive_member_paths(members, destination, BackupError)
        total_size = 0
        for member in members:
            if member.file_size > MAX_ARCHIVE_SINGLE_FILE_BYTES:
                raise BackupError(
                    f"Файл '{member.filename}' в архиве превышает допустимый размер "
                    f"({member.file_size} > {MAX_ARCHIVE_SINGLE_FILE_BYTES} байт)."
                )
            total_size += member.file_size
            if total_size > MAX_ARCHIVE_TOTAL_BYTES:
                raise BackupError(
                    f"Общий распакованный размер архива ({total_size} байт) "
                    f"превышает лимит ({MAX_ARCHIVE_TOTAL_BYTES} байт)."
                )

        total_extracted = 0
        for member, member_path in zip(members, member_paths):
            BackupService._check_cancelled(should_cancel)
            if member.is_dir():
                member_path.mkdir(parents=True, exist_ok=True)
                continue
            member_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, open(member_path, "wb") as dst:
                member_extracted = 0
                while chunk := src.read(65536):
                    BackupService._check_cancelled(should_cancel)
                    member_extracted += len(chunk)
                    total_extracted += len(chunk)
                    if member_extracted > MAX_ARCHIVE_SINGLE_FILE_BYTES:
                        raise BackupError(
                            f"Файл '{member.filename}' в архиве превышает допустимый размер "
                            f"({member_extracted} > {MAX_ARCHIVE_SINGLE_FILE_BYTES} байт)."
                        )
                    if total_extracted > MAX_ARCHIVE_TOTAL_BYTES:
                        raise BackupError(
                            f"Общий распакованный размер архива ({total_extracted} байт) "
                            f"превышает лимит ({MAX_ARCHIVE_TOTAL_BYTES} байт)."
                        )
                    dst.write(chunk)

