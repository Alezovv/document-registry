from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile

from app.database import Database
from app.document_service import DocumentService


BACKUP_FORMAT_VERSION = 1
_BACKUP_FREE_SPACE_RESERVE = 64 * 1024 * 1024


class BackupError(RuntimeError):
    pass


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

    def create_backup(self, destination: str | Path) -> BackupResult:
        destination = Path(destination).expanduser().resolve(strict=False)
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

            temp_zip = destination.with_name(f".{destination.name}.tmp")
            try:
                with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                    archive.write(db_snapshot, "database/requests.db")
                    archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                    for path in document_paths:
                        archive.write(path, f"documents/{path.name}")
                    for path in trash_paths:
                        relative = path.relative_to(self.documents.trash_dir)
                        archive.write(path, str(Path("trash") / relative))
                temp_zip.replace(destination)
            except Exception as error:
                temp_zip.unlink(missing_ok=True)
                raise BackupError(f"Не удалось создать резервную копию: {error}") from error

        # A backup is only reported as successful after the produced archive and
        # the database inside it have both been validated.
        verification = self.verify_backup(destination)
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
        self._write_health(result)
        return result

    def inspect_backup(self, backup_path: str | Path) -> dict:
        path = Path(backup_path).expanduser().resolve(strict=False)
        try:
            with zipfile.ZipFile(path, "r") as archive:
                names = set(archive.namelist())
                if "manifest.json" not in names or "database/requests.db" not in names:
                    raise BackupError("Архив не является резервной копией этого приложения.")
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                if int(manifest.get("format_version", 0)) != BACKUP_FORMAT_VERSION:
                    raise BackupError("Версия резервной копии не поддерживается.")
                return manifest
        except BackupError:
            raise
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise BackupError(f"Не удалось прочитать резервную копию: {error}") from error

    def verify_backup(self, backup_path: str | Path) -> dict[str, object]:
        """Deep-verify archive readability, file counts and the embedded SQLite DB."""
        path = Path(backup_path).expanduser().resolve(strict=False)
        manifest = self.inspect_backup(path)
        try:
            with zipfile.ZipFile(path, "r") as archive:
                bad_member = archive.testzip()
                if bad_member:
                    raise BackupError(f"Архив повреждён: {bad_member}")
                files = [info.filename for info in archive.infolist() if not info.is_dir()]
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
                    # Stream only the embedded DB to disk; do not load it into RAM.
                    with archive.open("database/requests.db", "r") as source, db_path.open("wb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
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

    def restore_backup(self, backup_path: str | Path) -> None:
        path = Path(backup_path).expanduser().resolve(strict=False)
        # Restoration always uses deep verification; a manifest-only check is not
        # sufficient before replacing user data.
        self.verify_backup(path)

        with tempfile.TemporaryDirectory(prefix="registry_restore_") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            extracted = temp_dir / "incoming"
            extracted.mkdir()
            try:
                with zipfile.ZipFile(path, "r") as archive:
                    self._safe_extract(archive, extracted)
            except Exception as error:
                if isinstance(error, BackupError):
                    raise
                raise BackupError(f"Не удалось распаковать резервную копию: {error}") from error

            incoming_db = extracted / "database" / "requests.db"
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
        try:
            self.health_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.health_path.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.health_path)
        except OSError:
            # Backup is already valid; inability to persist informational health
            # metadata must not turn it into a failed backup.
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
    def _validate_database(path: Path) -> None:
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
            finally:
                conn.close()
        except sqlite3.Error as error:
            raise BackupError(f"Резервная база данных повреждена: {error}") from error

    def _replace_database(self, source: Path) -> None:
        self.database.db_path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(self.database.db_path) + suffix).unlink(missing_ok=True)
        temp_target = self.database.db_path.with_name(f".{self.database.db_path.name}.restore")
        shutil.copy2(source, temp_target)
        temp_target.replace(self.database.db_path)

    @staticmethod
    def _replace_file(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f".{target.name}.restore")
        shutil.copy2(source, staged)
        staged.replace(target)

    @staticmethod
    def _replace_directory(source: Path, target: Path) -> None:
        staged = target.with_name(f".{target.name}.restore")
        if staged.exists():
            shutil.rmtree(staged)
        if source.exists():
            shutil.copytree(source, staged)
        else:
            staged.mkdir(parents=True)
        if target.exists():
            shutil.rmtree(target)
        staged.replace(target)

    @staticmethod
    def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
        root = destination.resolve()
        for member in archive.infolist():
            member_path = (destination / member.filename).resolve()
            if root not in member_path.parents and member_path != root:
                raise BackupError("В архиве обнаружен небезопасный путь.")
        archive.extractall(destination)
