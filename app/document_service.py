from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path, PureWindowsPath
import re
import shutil
import threading
import uuid

from app.database import Database
from app.models import Request


logger = logging.getLogger(__name__)


class DocumentError(RuntimeError):
    """Base error for document storage operations."""


class InvalidDocumentError(DocumentError):
    pass


class DocumentStorageError(DocumentError):
    pass


@dataclass(frozen=True)
class StoredDocument:
    path: Path
    created: bool


@dataclass(frozen=True)
class DocumentEntry:
    path: Path
    size_bytes: int
    modified_ns: int


@dataclass(frozen=True)
class DocumentDeleteResult:
    path: Path
    related_request_id: int | None
    removed: bool


@dataclass(frozen=True)
class TrashStats:
    file_count: int
    size_bytes: int


@dataclass(frozen=True)
class StagedTrash:
    path: Path
    stats: TrashStats


class DocumentService:
    """Filesystem boundary for PDF documents.

    The service owns copying, hashing, trash/restore and duplicate detection.
    Database only stores paths and business records.
    """

    def __init__(
        self,
        database: Database,
        documents_dir: str | Path | None = None,
    ) -> None:
        self.database = database
        self.documents_dir = Path(documents_dir or database.documents_dir)
        self.trash_dir = database.data_dir / "trash"
        self._hash_cache: OrderedDict[tuple[str, int, int], str] = OrderedDict()
        self._hash_cache_lock = threading.RLock()
        self._hash_cache_limit = 512
        # Compatibility/reporting guard for the first automatic portable rebind.
        # It is intentionally process-local: the database itself stays free of
        # machine-specific runtime-root metadata.
        self._runtime_rebind_reported = False
        self.set_documents_dir(self.documents_dir)

    @staticmethod
    def _absolute_without_resolving(path: str | Path) -> Path:
        """Return an absolute path without resolving aliases/symlinks.

        On Windows ``Path.resolve()`` can expand an 8.3 profile path such as
        ``C:\\Users\\CD86~1`` into its long Unicode form.  Both names refer
        to the same location, but persisting the expanded spelling breaks
        portable-path equality after the application folder is moved.
        """
        candidate = Path(path).expanduser()
        if candidate.is_absolute():
            return candidate
        return Path.cwd() / candidate

    def set_documents_dir(self, documents_dir: str | Path) -> None:
        self.documents_dir = self._absolute_without_resolving(documents_dir)
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        self.database.documents_dir = self.documents_dir
        self.clear_caches()

    def clear_caches(self) -> None:
        with self._hash_cache_lock:
            self._hash_cache.clear()

    @staticmethod
    def _canonical_existing_path(path: Path) -> Path:
        """Return the canonical spelling for an existing filesystem path.

        This is deliberately used only for values returned to callers.  The
        portable runtime directory itself keeps its original spelling so a moved
        installation can still persist paths under the current runtime root.
        """
        try:
            return path.resolve()
        except OSError:
            return path.absolute()

    @staticmethod
    def _same_directory(left: Path, right: Path) -> bool:
        """Compare directory identity without changing stored path spelling."""
        try:
            return left.samefile(right)
        except OSError:
            try:
                return left.resolve() == right.resolve()
            except OSError:
                return left.absolute() == right.absolute()

    def resolve_document_reference(self, path: str | Path | None) -> Path | None:
        """Resolve a stored PDF path after the app folder was moved.

        Existing files are returned in canonical filesystem spelling.  This keeps
        compatibility with callers that compare the returned path with
        ``Path.resolve()``.  Repair policy is handled separately: normal repairs
        store the canonical path, while a detected whole-runtime move keeps the
        current portable runtime spelling.
        """
        if not path:
            return None
        decoded = self.database.decode_source_document(path)
        candidate = Path(decoded or str(path)).expanduser()
        try:
            if candidate.is_file():
                return self._canonical_existing_path(candidate)
        except OSError:
            pass

        name = self.reference_name(path)
        if not name:
            return None
        relocated = self.documents_dir / name
        try:
            if relocated.is_file():
                return self._canonical_existing_path(relocated)
        except OSError:
            pass
        return None

    def repair_stored_document_paths(self, preserve_runtime_spelling: bool | None = None) -> int:
        """Make PDF references portable and rebind legacy stale paths.

        Persistent storage uses ``@runtime/...`` for files inside the portable
        application folder. Legacy absolute paths are rebound by filename and
        converted to tokens.

        The return value counts persistent rewrites. For backwards compatibility
        with the pre-token portability contract, the *first automatic* repair on
        a database that already contains runtime tokens also reports the number
        of live token references rebound to this process' current runtime root.
        That compatibility count is in-memory only; no machine-specific root is
        written to the database. Explicit startup/backup modes do not use it.
        """
        automatic_mode = preserve_runtime_spelling is None
        had_runtime_tokens = self.database.has_runtime_source_document_tokens()
        if automatic_mode:
            # A tokenized database should expose the spelling of the current
            # portable root. Legacy absolute-path repair remains canonical.
            preserve_runtime_spelling = had_runtime_tokens
        self.database.set_runtime_path_presentation(bool(preserve_runtime_spelling))

        normalized = self.database.normalize_runtime_source_documents()
        normalized += self.database.normalize_runtime_history_paths()

        updates: list[tuple[int, str]] = []
        live_runtime_bindings = 0
        for request in self.database.get_requests_with_documents():
            if request.id is None or not request.source_document:
                continue
            current = Path(request.source_document).expanduser()
            try:
                if current.is_file():
                    if automatic_mode and had_runtime_tokens:
                        live_runtime_bindings += 1
                    continue
            except OSError:
                pass
            resolved = self.resolve_document_reference(request.source_document)
            if resolved is not None:
                updates.append((request.id, str(resolved)))

        repaired = self.database.repair_source_document_paths(updates)
        total = normalized + repaired

        # Older callers/tests use a non-zero result as a signal that references
        # were rebound after moving the whole runtime folder. With @runtime
        # storage no DB rewrite is needed, so report that successful binding once
        # per service instance without polluting persistent storage.
        if (
            total == 0
            and automatic_mode
            and had_runtime_tokens
            and live_runtime_bindings
            and not self._runtime_rebind_reported
        ):
            total = live_runtime_bindings
            self._runtime_rebind_reported = True

        if total:
            logger.info("Перепривязано/нормализовано путей PDF: %s", total)
            self.clear_caches()
        return total

    def list_document_entries(self) -> list[DocumentEntry]:
        """Return one metadata snapshot per file using a single stat call."""
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        entries: list[DocumentEntry] = []
        for path in self.documents_dir.iterdir():
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                logger.warning("Unable to read document metadata: %s", path)
                continue
            entries.append(
                DocumentEntry(
                    path=path,
                    size_bytes=stat.st_size,
                    modified_ns=stat.st_mtime_ns,
                )
            )
        entries.sort(key=lambda entry: entry.path.name.casefold())
        return entries

    def list_documents(self) -> list[Path]:
        return [entry.path for entry in self.list_document_entries()]

    @staticmethod
    def normalize_path(path: str | Path) -> Path:
        return DocumentService._absolute_without_resolving(path)

    @staticmethod
    def reference_name(path: str | Path) -> str:
        """Return a basename even for a Windows path read on another platform."""
        text = str(path or "")
        if "\\" in text:
            return PureWindowsPath(text).name
        return Path(text).name

    @staticmethod
    def normalize_file_name(path: str | Path) -> str:
        try:
            return re.sub(
                r"[^\w\u0400-\u04FF]+",
                "",
                Path(path).name.lower(),
            )
        except (TypeError, ValueError, re.error):
            return ""

    @staticmethod
    def _validate_pdf(path: Path) -> None:
        if not path.exists() or not path.is_file():
            raise InvalidDocumentError(f"PDF-файл не найден: {path}")
        if path.suffix.lower() != ".pdf":
            raise InvalidDocumentError("Можно сохранять только PDF-файлы.")

    def calculate_file_hash(self, file_path: str | Path) -> str | None:
        path = Path(file_path)
        try:
            stat = path.stat()
        except OSError as error:
            logger.warning("Не удалось прочитать метаданные %s: %s", path, error)
            return None

        cache_key = (
            str(self.normalize_path(path)),
            stat.st_size,
            stat.st_mtime_ns,
        )
        with self._hash_cache_lock:
            cached = self._hash_cache.get(cache_key)
            if cached is not None:
                self._hash_cache.move_to_end(cache_key)
                return cached

        sha256 = hashlib.sha256()
        try:
            with path.open("rb") as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    sha256.update(chunk)
        except OSError as error:
            logger.warning("Не удалось хэшировать %s: %s", path, error)
            return None

        digest = sha256.hexdigest()
        with self._hash_cache_lock:
            # Another thread may have calculated the same digest in parallel;
            # replacing the same key is harmless and all OrderedDict operations
            # remain protected.
            self._hash_cache[cache_key] = digest
            self._hash_cache.move_to_end(cache_key)
            while len(self._hash_cache) > self._hash_cache_limit:
                self._hash_cache.popitem(last=False)
        return digest

    def _unique_destination(self, source: Path) -> Path:
        # Each request owns its stored PDF path. Never reuse an existing path,
        # even when the bytes are identical, otherwise deleting one request
        # could break another request that points to the same file.
        destination = self.documents_dir / source.name
        if not destination.exists():
            return destination

        index = 2
        while True:
            candidate = self.documents_dir / f"{source.stem} ({index}){source.suffix}"
            if not candidate.exists():
                return candidate
            index += 1

    def store_document(self, source_path: str | Path) -> StoredDocument:
        source = self.normalize_path(source_path)
        self._validate_pdf(source)
        self.documents_dir.mkdir(parents=True, exist_ok=True)

        try:
            if self._same_directory(source.parent, self.documents_dir):
                return StoredDocument(
                    self._canonical_existing_path(source),
                    created=False,
                )

            destination = self._unique_destination(source)
            if destination.exists():
                return StoredDocument(
                    self._canonical_existing_path(destination),
                    created=False,
                )

            shutil.copy2(source, destination)
            return StoredDocument(
                self._canonical_existing_path(destination),
                created=True,
            )
        except OSError as error:
            raise DocumentStorageError(
                f"Не удалось сохранить PDF {source.name}: {error}"
            ) from error

    def discard_created_document(self, stored: StoredDocument | None) -> None:
        if stored is None or not stored.created:
            return
        try:
            if stored.path.exists():
                stored.path.unlink()
        except OSError:
            logger.exception("Не удалось удалить файл после отката: %s", stored.path)

    def get_trash_stats(self) -> TrashStats:
        if not self.trash_dir.exists():
            return TrashStats(0, 0)

        file_count = 0
        size_bytes = 0
        for path in self.trash_dir.rglob("*"):
            if not path.is_file():
                continue
            file_count += 1
            try:
                size_bytes += path.stat().st_size
            except OSError:
                logger.warning("Не удалось получить размер файла корзины: %s", path)
        return TrashStats(file_count, size_bytes)

    def stage_trash_cleanup(self) -> StagedTrash | None:
        stats = self.get_trash_stats()
        if not self.trash_dir.exists():
            return None

        staged = self.trash_dir.parent / f".trash_cleanup_{uuid.uuid4().hex}"
        try:
            self.trash_dir.replace(staged)
            self.trash_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            if staged.exists() and not self.trash_dir.exists():
                try:
                    staged.replace(self.trash_dir)
                except OSError:
                    logger.exception("Не удалось откатить подготовку очистки корзины")
            raise DocumentStorageError(
                f"Не удалось подготовить корзину к очистке: {error}"
            ) from error
        return StagedTrash(staged, stats)

    @staticmethod
    def _merge_directory_without_overwrite(source: Path, destination: Path) -> None:
        """Merge directory contents without ever overwriting a file.

        This is used only when rolling a staged trash cleanup back. Both trees
        must be preserved even if new trash entries appeared while cleanup was
        staged or filenames collide.
        """
        destination.mkdir(parents=True, exist_ok=True)
        for item in list(source.iterdir()):
            target = destination / item.name
            if target.exists() and item.is_dir() and target.is_dir():
                DocumentService._merge_directory_without_overwrite(item, target)
                continue
            if target.exists():
                base = target
                index = 2
                while target.exists():
                    target = base.with_name(f"{base.stem} ({index}){base.suffix}")
                    index += 1
            item.replace(target)
        source.rmdir()

    def rollback_staged_trash(self, staged: StagedTrash | None) -> None:
        if staged is None or not staged.path.exists():
            return

        # Keep any entries created after staging. Never rmtree the live trash
        # before the old tree has been restored: a failed rename must not erase
        # either generation of files.
        holding = self.trash_dir.with_name(
            f".{self.trash_dir.name}.rollback_{uuid.uuid4().hex}"
        )
        moved_current = False
        try:
            if self.trash_dir.exists():
                self.trash_dir.replace(holding)
                moved_current = True
            try:
                staged.path.replace(self.trash_dir)
            except OSError:
                # Best-effort restoration of the newer tree if the old staged
                # tree could not be moved back into place.
                if moved_current and holding.exists() and not self.trash_dir.exists():
                    holding.replace(self.trash_dir)
                raise

            if moved_current and holding.exists():
                self._merge_directory_without_overwrite(holding, self.trash_dir)
        except OSError as error:
            retained_at = f" Данные сохранены во временном каталоге: {holding}" if holding.exists() else ""
            raise DocumentStorageError(
                f"Не удалось вернуть корзину после отката: {error}.{retained_at}"
            ) from error

    def finalize_staged_trash(self, staged: StagedTrash | None) -> None:
        if staged is None or not staged.path.exists():
            return
        try:
            shutil.rmtree(staged.path)
        except OSError as error:
            raise DocumentStorageError(
                f"История очищена, но не удалось окончательно удалить файлы корзины: {error}"
            ) from error

    def _owner_trash_dir(self, owner_id: int | None) -> Path:
        name = f"request_{owner_id}" if owner_id is not None else "untracked"
        path = self.trash_dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def move_to_trash(
        self,
        path: str | Path,
        owner_id: int | None = None,
    ) -> Path | None:
        source = self.normalize_path(path)
        if not source.exists():
            return None

        target_dir = self._owner_trash_dir(owner_id)
        target = target_dir / source.name
        if target.exists():
            index = 2
            while True:
                candidate = target_dir / f"{source.stem} ({index}){source.suffix}"
                if not candidate.exists():
                    target = candidate
                    break
                index += 1

        try:
            shutil.move(str(source), str(target))
            return target
        except OSError as error:
            raise DocumentStorageError(
                f"Не удалось переместить документ в корзину: {error}"
            ) from error

    def restore_from_trash(
        self,
        trash_path: str | Path,
        destination: str | Path,
    ) -> Path:
        source = self.normalize_path(trash_path)
        target = self.normalize_path(destination)
        if not source.is_file():
            raise DocumentStorageError(f"Файл в корзине не найден: {source}")

        # Never attach an existing, unrelated document to the restored request.
        # Preserve both files by choosing a collision-free destination.
        if target.exists():
            base_target = target
            index = 2
            while target.exists():
                target = base_target.with_name(
                    f"{base_target.stem} ({index}){base_target.suffix}"
                )
                index += 1

        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(source), str(target))
            return target
        except OSError as error:
            raise DocumentStorageError(
                f"Не удалось восстановить документ из корзины: {error}"
            ) from error

    def find_latest_trashed(
        self,
        original_path: str | Path,
        owner_id: int | None,
    ) -> Path | None:
        original = Path(self.reference_name(original_path))
        owner_dir = self.trash_dir / (
            f"request_{owner_id}" if owner_id is not None else "untracked"
        )

        candidates: list[Path] = []
        if owner_dir.exists():
            candidates.extend(
                path
                for path in owner_dir.iterdir()
                if path.is_file()
                and (
                    path.name == original.name
                    or path.stem.startswith(f"{original.stem} (")
                )
                and path.suffix.lower() == original.suffix.lower()
            )

        # Backward compatibility with Step 1/2, where trash was flat.
        legacy_path = self.trash_dir / original.name
        if legacy_path.is_file():
            candidates.append(legacy_path)

        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime_ns)

    def find_duplicate_document(
        self, file_path: str | Path, exclude_request_id: int | None = None
    ) -> Request | None:
        new_hash = self.calculate_file_hash(file_path)
        normalized_new_name = self.normalize_file_name(file_path)

        for request in self.database.get_requests_with_documents():
            if exclude_request_id is not None and request.id == exclude_request_id:
                continue
            source_document = request.source_document
            if not source_document:
                continue

            existing_path = self.resolve_document_reference(source_document)
            if existing_path is None:
                continue

            existing_hash = self.calculate_file_hash(existing_path)
            if existing_hash and new_hash:
                if existing_hash == new_hash:
                    return request
                # Same filename with different content is not a duplicate.
                continue

            # Name comparison is only a fallback when hashing was impossible.
            normalized_existing_name = self.normalize_file_name(existing_path)
            if (
                normalized_new_name
                and normalized_existing_name
                and normalized_new_name == normalized_existing_name
            ):
                return request

        return None

    def find_related_request(self, path: str | Path) -> Request | None:
        target_path = self.resolve_document_reference(path) or self.normalize_path(path)
        direct = self.database.get_request_by_source_document(str(target_path))
        if direct is not None:
            return direct

        # Legacy/portable fallback for paths stored with a different spelling
        # (for example Windows 8.3 versus the long Unicode profile path).
        for request in self.database.get_requests_with_documents():
            resolved = self.resolve_document_reference(request.source_document)
            if resolved is not None and resolved == target_path:
                return request
            if self.normalize_path(request.source_document) == self.normalize_path(path):
                return request
        return None

    def delete_untracked_document(self, path: str | Path) -> DocumentDeleteResult:
        target = self.normalize_path(path)
        try:
            resolved_root = self.documents_dir.resolve(strict=False)
            resolved_target = target.resolve(strict=False)
            # Do not allow traversal, symlinks, or paths outside the application's
            # document store to turn this method into an arbitrary-file delete.
            resolved_target.relative_to(resolved_root)
        except ValueError as error:
            raise DocumentStorageError(
                "Удаление разрешено только внутри каталога документов."
            ) from error
        except (OSError, RuntimeError) as error:
            raise DocumentStorageError(
                f"Не удалось проверить путь документа: {error}"
            ) from error

        if resolved_target == resolved_root:
            raise DocumentStorageError("Нельзя удалить сам каталог документов.")
        if not target.exists():
            raise FileNotFoundError(target)
        target.unlink()
        return DocumentDeleteResult(target, None, True)
