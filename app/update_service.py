from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
import hashlib
import http.cookiejar
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request

from app.app_paths import AppPaths, get_app_paths
from app.db_migrations import (
    CURRENT_SCHEMA_VERSION,
    DatabaseRollbackComparison,
    capture_invariants,
    compare_database_for_rollback,
    downgrade_database,
    get_schema_version,
)
from app.text_encoding import loads_json_with_fallback, repair_cyrillic_mojibake
from app.update_history import append_update_history, load_update_history
from app.update_config import (
    MAX_MANIFEST_BYTES,
    UPDATE_CHUNK_SIZE,
    UPDATE_DOWNLOAD_TIMEOUT_SECONDS,
    UPDATE_DRIVE_FOLDER_ID,
    UPDATE_DRIVE_FOLDER_URL,
    UPDATE_MANIFEST_FILE_ID,
    UPDATE_HTTP_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

MANIFEST_FORMAT_VERSION = 1
MANIFEST_FILE_NAME = "manifest.json"
UPDATE_PACKAGE_SUFFIX = ".zip"
UPDATER_RELATIVE_PATH = Path("updater") / "DocumentRegistryUpdater.exe"
LEGACY_PENDING_UPDATER_RELATIVE_PATH = Path("updater") / "DocumentRegistryUpdater.next.exe"
UPDATER_PACKAGE_MEMBER = "payload/updater/DocumentRegistryUpdater.exe"
LEGACY_UPDATER_PACKAGE_MEMBER = "payload/updater/DocumentRegistryUpdater.next.exe"
MAX_BOOTSTRAP_UPDATER_BYTES = 128 * 1024 * 1024
_VERSION_RE = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?\s*$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_DRIVE_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)


class UpdateError(RuntimeError):
    """Base exception for update discovery, validation and preparation."""


class UpdateNotConfigured(UpdateError):
    pass


class UpdateNotReady(UpdateError):
    pass


class UpdateNetworkError(UpdateNotReady):
    """Temporary connectivity problem. Current app remains fully usable."""


class UpdateDownloadCancelled(UpdateError):
    """Raised when the user cancels a background package download."""


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    version: str
    package_path: Path | None
    sha256: str
    size_bytes: int
    database_schema: int = CURRENT_SCHEMA_VERSION
    notes: str = ""
    package_file_id: str = ""
    source: str = "local"


@dataclass(frozen=True, slots=True)
class SavedDatabaseBackup:
    db_backup_path: Path
    manifest_path: Path
    source_version: str
    target_version: str
    source_schema: int
    target_schema: int
    requests_count: int
    history_count: int
    created_at: str


class _DownloadFormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_download_form = False
        self.action = ""
        self.fields: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs):
        attributes = dict(attrs)
        if tag.lower() == "form" and attributes.get("id") == "download-form":
            self.in_download_form = True
            self.action = str(attributes.get("action") or "")
            return
        if tag.lower() == "input" and self.in_download_form:
            name = attributes.get("name")
            value = attributes.get("value")
            if isinstance(name, str) and isinstance(value, str):
                self.fields.append((name, value))

    def handle_endtag(self, tag: str):
        if tag.lower() == "form" and self.in_download_form:
            self.in_download_form = False


def parse_version(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.match(str(value or ""))
    if not match:
        raise ValueError(f"Некорректная версия: {value!r}")
    return tuple(int(part) for part in match.groups())


def is_newer_version(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def is_older_version(candidate: str, current: str) -> bool:
    return parse_version(candidate) < parse_version(current)


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def validate_download_url(url: str) -> str:
    """Validate that the download URL uses HTTPS and points to an authorized Google Drive host."""
    if not url or not isinstance(url, str):
        raise UpdateError("URL обновления не может быть пустым.")
    url = url.strip()
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception as e:
        raise UpdateError(f"Некорректный URL: {e}") from e

    if parsed.scheme.lower() != "https":
        raise UpdateError(f"Небезопасная схема URL: {parsed.scheme!r}. Разрешён только HTTPS.")
    if parsed.username or parsed.password:
        raise UpdateError("URL не должен содержать учетные данные (userinfo).")
    if parsed.port and parsed.port != 443:
        raise UpdateError(f"Недопустимый порт в URL: {parsed.port}")
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise UpdateError("В URL отсутствует имя хоста.")
    allowed_exact = {"drive.google.com", "docs.google.com", "drive.usercontent.google.com"}
    if hostname in allowed_exact or hostname == "googleusercontent.com" or hostname.endswith(".googleusercontent.com"):
        return url
    raise UpdateError(f"Недопустимый хост для скачивания обновления: {hostname}")


class UpdateService:
    """Update discovery, version rollback discovery, and package preparation.

    Production checks a hard-coded public Google Drive ``manifest.json``
    directly by its stable file ID over HTTPS.  Google Drive for Desktop and
    folder-page scraping are not required.  The legacy local-folder path
    remains supported internally for tests and developer diagnostics.
    """

    drive_folder_url = UPDATE_DRIVE_FOLDER_URL
    drive_folder_id = UPDATE_DRIVE_FOLDER_ID
    manifest_file_id = UPDATE_MANIFEST_FILE_ID

    def __init__(self, paths: AppPaths | None = None):
        self.paths = paths or get_app_paths()
        self.download_dir = Path(self.paths.data) / "updates"
        backups_root = getattr(self.paths, "backups", None)
        if backups_root is None:
            backups_root = Path(self.paths.data).parent / "backups"
        self._backups_root = Path(backups_root)
        self.rollback_backups_dir = self._backups_root / "rollback"

    @property
    def _database_path(self) -> Path:
        db = getattr(self.paths, "database", None)
        if db is not None:
            return Path(db)
        return Path(self.paths.data) / "requests.db"

    def consume_last_install_status(self) -> dict | None:
        """Return one terminal updater result once, then mark it as notified.

        The standalone updater writes this file even while the main application
        is closed. On the first launch after an update/rollback we surface a
        concise toast so the operator knows exactly what happened.
        """
        path = self.paths.data / "update_status.json"
        try:
            raw = loads_json_with_fallback(path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        if str(raw.get("state") or "") not in {"success", "failed"}:
            return None
        if bool(raw.get("notified")):
            return None

        # Older standalone updaters only wrote update_status.json.  Promote that
        # terminal result into the durable history on first launch, while avoiding
        # a duplicate when a newer updater has already written the same event.
        updated_at = str(raw.get("updated_at") or "")
        version = str(raw.get("version") or "")
        state = str(raw.get("state") or "")
        recent = load_update_history(self.paths.data, limit=40)
        already_logged = any(
            str(item.get("version") or "") == version
            and str(item.get("state") or "") == state
            and str(item.get("timestamp") or item.get("updated_at") or "") == updated_at
            for item in recent
        )
        if not already_logged:
            history_item = dict(raw)
            history_item["session_id"] = str(raw.get("session_id") or f"legacy-{version}-{updated_at}")
            history_item["timestamp"] = updated_at
            history_item.pop("notified", None)
            append_update_history(self.paths.data, history_item)

        raw["notified"] = True
        raw["notified_at"] = datetime.now(timezone.utc).isoformat()
        temp = path.with_suffix(".json.tmp")
        try:
            temp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, path)
        except OSError:
            pass
        return raw

    # ------------------------------------------------------------------
    # Discovery (Main Mode: Forward Update + Additional Mode: Rollback)
    # ------------------------------------------------------------------
    def check_for_update(
        self,
        source_dir: str | Path | None,
        current_version: str,
    ) -> UpdateInfo | None:
        source_text = str(source_dir or "").strip()
        if source_text:
            return self._check_local_source(Path(source_text).expanduser(), current_version)
        return self._check_google_drive(current_version)

    def list_rollback_versions(
        self,
        source_dir: str | Path | None,
        current_version: str,
    ) -> list[UpdateInfo]:
        """Return older versions available for rollback, sorted newest first."""
        parse_version(current_version)
        source_text = str(source_dir or "").strip()
        by_version: dict[tuple[int, int, int], UpdateInfo] = {}
        manifest_error: Exception | None = None

        if source_text:
            local_source = Path(source_text).expanduser()
            try:
                for item in self._list_local_manifest_versions(local_source, current_version, direction="older"):
                    by_version[parse_version(item.version)] = item
            except UpdateError as error:
                manifest_error = error
            for item in self._discover_local_packages_in_dir(local_source, current_version):
                key = parse_version(item.version)
                if key not in by_version:
                    by_version[key] = item
        else:
            try:
                for item in self._list_drive_manifest_versions(current_version, direction="older"):
                    by_version[parse_version(item.version)] = item
            except UpdateError as error:
                manifest_error = error

        # Also check locally cached packages in data/updates and backups/updates
        for cache_dir in (self.download_dir, self._backups_root / "updates"):
            for item in self._discover_local_packages_in_dir(cache_dir, current_version):
                key = parse_version(item.version)
                if key in by_version:
                    existing = by_version[key]
                    if existing.package_path is None and item.package_path is not None and item.sha256.lower() == existing.sha256.lower():
                        by_version[key] = UpdateInfo(
                            version=existing.version,
                            package_path=item.package_path,
                            sha256=existing.sha256,
                            size_bytes=existing.size_bytes,
                            database_schema=existing.database_schema,
                            notes=existing.notes or item.notes,
                            package_file_id=existing.package_file_id,
                            source=existing.source,
                        )
                else:
                    by_version[key] = item

        if not by_version and manifest_error is not None:
            raise manifest_error

        return [
            by_version[key]
            for key in sorted(by_version.keys(), reverse=True)
        ]

    def inspect_package_file(
        self,
        package_path: str | Path,
        *,
        current_version: str | None = None,
    ) -> UpdateInfo:
        """Inspect a local .zip update package and extract its version, schema and description."""
        path = Path(package_path).expanduser().resolve(strict=False)
        if not path.is_file():
            raise UpdateError(f"Файл пакета не найден: {path}")
        try:
            size_bytes = int(path.stat().st_size)
        except OSError as error:
            raise UpdateError(f"Не удалось прочитать размер пакета: {error}") from error
        if size_bytes <= 0:
            raise UpdateError("Выбранный пакет обновления пуст.")

        try:
            with zipfile.ZipFile(path, "r") as archive:
                names = set(archive.namelist())
                if "update_manifest.json" not in names:
                    raise UpdateError("В архиве отсутствует update_manifest.json.")
                manifest_info = archive.getinfo("update_manifest.json")
                max_manifest_bytes = 5 * 1024 * 1024
                if manifest_info.file_size > max_manifest_bytes:
                    raise UpdateError("Файл update_manifest.json превышает допустимый размер.")
                with archive.open("update_manifest.json", "r") as mf:
                    manifest_bytes = mf.read(max_manifest_bytes + 1)
                if len(manifest_bytes) > max_manifest_bytes:
                    raise UpdateError("Файл update_manifest.json превышает допустимый размер.")
                raw_manifest = loads_json_with_fallback(manifest_bytes)
        except UpdateError:
            raise
        except (OSError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError(f"Не удалось прочитать пакет обновления: {error}") from error

        if not isinstance(raw_manifest, dict):
            raise UpdateError("Внутренний update_manifest.json имеет неверный формат.")
        if int(raw_manifest.get("format_version", 0) or 0) != MANIFEST_FORMAT_VERSION:
            raise UpdateError("Формат пакета обновления не поддерживается.")

        version = str(raw_manifest.get("version") or "").strip()
        try:
            parse_version(version)
        except ValueError as error:
            raise UpdateError(str(error)) from error

        if current_version is not None and not is_older_version(version, current_version):
            raise UpdateError(
                f"Выбранный пакет имеет версию {version}, которая не старше текущей версии {current_version}."
            )

        try:
            database_schema = int(raw_manifest.get("database_schema", CURRENT_SCHEMA_VERSION))
        except (TypeError, ValueError) as error:
            raise UpdateError("Некорректная версия схемы БД в пакете.") from error
        if database_schema < 0:
            raise UpdateError("Некорректная версия схемы БД в пакете.")

        notes = self._extract_notes(raw_manifest)
        digest = sha256_file(path)
        return UpdateInfo(
            version=version,
            package_path=path,
            sha256=digest,
            size_bytes=size_bytes,
            database_schema=database_schema,
            notes=notes,
            source="local",
        )

    def _discover_local_packages_in_dir(
        self,
        directory: Path,
        current_version: str,
    ) -> list[UpdateInfo]:
        if not directory.is_dir():
            return []
        found: list[UpdateInfo] = []
        candidates = list(directory.glob("*.zip"))
        for subdir in directory.iterdir():
            if subdir.is_dir() and not subdir.name.endswith(".creating"):
                candidates.extend(subdir.glob("*.zip"))
        for candidate in candidates:
            try:
                info = self.inspect_package_file(candidate, current_version=current_version)
                found.append(info)
            except Exception:
                continue
        return found

    def _check_local_source(self, source: Path, current_version: str) -> UpdateInfo | None:
        manifest_path = source / MANIFEST_FILE_NAME
        if not manifest_path.is_file():
            raise UpdateNotReady("manifest.json не найден в локальной папке обновлений.")
        try:
            raw = loads_json_with_fallback(manifest_path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise UpdateNotReady(f"Не удалось прочитать manifest.json: {error}") from error

        parsed = self._parse_manifest(raw, current_version)
        if parsed is None:
            return None
        (
            version,
            package_name,
            package_file_id,
            expected_hash,
            manifest_size,
            database_schema,
            notes,
        ) = parsed

        package_path = source / package_name
        if not package_path.is_file():
            raise UpdateNotReady("Пакет обновления отсутствует в локальной папке.")
        try:
            actual_size = int(package_path.stat().st_size)
        except OSError as error:
            raise UpdateNotReady(f"Не удалось получить размер пакета обновления: {error}") from error
        if manifest_size > 0 and actual_size != manifest_size:
            raise UpdateNotReady("Размер пакета не совпадает с manifest.json.")

        return UpdateInfo(
            version=version,
            package_path=package_path,
            sha256=expected_hash,
            size_bytes=actual_size,
            database_schema=database_schema,
            notes=notes,
            source="local",
        )

    def _check_google_drive(self, current_version: str) -> UpdateInfo | None:
        if not self.manifest_file_id:
            return None
        # Do not enumerate/scrape the Drive folder.  Google changes folder HTML
        # independently of the public file download endpoint.  The manifest has
        # a stable file ID and names the exact package file ID for each release.
        manifest_bytes = self._read_drive_file(self.manifest_file_id, MAX_MANIFEST_BYTES)
        try:
            raw = loads_json_with_fallback(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError(f"Некорректный manifest.json на Google Drive: {error}") from error

        parsed = self._parse_manifest(raw, current_version)
        if parsed is None:
            return None
        (
            version,
            package_name,
            package_file_id,
            expected_hash,
            manifest_size,
            database_schema,
            notes,
        ) = parsed

        if not _DRIVE_FILE_ID_RE.fullmatch(package_file_id):
            raise UpdateNotReady(
                "В manifest.json не указан корректный package_file_id Google Drive. "
                "Обновление ещё не готово к скачиванию."
            )

        return UpdateInfo(
            version=version,
            package_path=None,
            package_file_id=package_file_id,
            sha256=expected_hash,
            size_bytes=manifest_size,
            database_schema=database_schema,
            notes=notes,
            source="google_drive",
        )

    def _list_local_manifest_versions(
        self,
        source: Path,
        current_version: str,
        *,
        direction: str = "older",
    ) -> list[UpdateInfo]:
        manifest_path = source / MANIFEST_FILE_NAME
        if not manifest_path.is_file():
            raise UpdateNotReady("manifest.json не найден в локальной папке обновлений.")
        try:
            raw = loads_json_with_fallback(manifest_path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise UpdateNotReady(f"Не удалось прочитать manifest.json: {error}") from error

        entries = self._parse_manifest_entries(raw, current_version, direction=direction)
        results: list[UpdateInfo] = []
        for (
            version,
            package_name,
            package_file_id,
            expected_hash,
            manifest_size,
            database_schema,
            notes,
        ) in entries:
            package_path = source / package_name
            if package_path.is_file():
                try:
                    actual_size = int(package_path.stat().st_size)
                except OSError:
                    actual_size = manifest_size
                if manifest_size > 0 and actual_size != manifest_size:
                    continue
                results.append(
                    UpdateInfo(
                        version=version,
                        package_path=package_path,
                        sha256=expected_hash,
                        size_bytes=actual_size,
                        database_schema=database_schema,
                        notes=notes,
                        package_file_id=package_file_id,
                        source="local",
                    )
                )
            elif _DRIVE_FILE_ID_RE.fullmatch(package_file_id):
                results.append(
                    UpdateInfo(
                        version=version,
                        package_path=None,
                        package_file_id=package_file_id,
                        sha256=expected_hash,
                        size_bytes=manifest_size,
                        database_schema=database_schema,
                        notes=notes,
                        source="google_drive",
                    )
                )
        return results

    def _list_drive_manifest_versions(
        self,
        current_version: str,
        *,
        direction: str = "older",
    ) -> list[UpdateInfo]:
        if not self.manifest_file_id:
            return []
        manifest_bytes = self._read_drive_file(self.manifest_file_id, MAX_MANIFEST_BYTES)
        try:
            raw = loads_json_with_fallback(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError(f"Некорректный manifest.json на Google Drive: {error}") from error

        entries = self._parse_manifest_entries(raw, current_version, direction=direction)
        results: list[UpdateInfo] = []
        for (
            version,
            package_name,
            package_file_id,
            expected_hash,
            manifest_size,
            database_schema,
            notes,
        ) in entries:
            local_cached = self.download_dir / package_name
            has_local = local_cached.is_file() and local_cached.stat().st_size == manifest_size
            if not has_local and not _DRIVE_FILE_ID_RE.fullmatch(package_file_id):
                continue
            results.append(
                UpdateInfo(
                    version=version,
                    package_path=local_cached if has_local else None,
                    package_file_id=package_file_id,
                    sha256=expected_hash,
                    size_bytes=manifest_size,
                    database_schema=database_schema,
                    notes=notes,
                    source="local" if has_local else "google_drive",
                )
            )
        return results

    @staticmethod
    def _extract_notes(raw: dict) -> str:
        for key in ("notes", "release_notes", "description", "changelog"):
            if key in raw and raw[key]:
                val = raw[key]
                if isinstance(val, list):
                    text = "\n".join(
                        repair_cyrillic_mojibake(str(item).strip())
                        for item in val
                        if str(item).strip()
                    )
                    return repair_cyrillic_mojibake(text)
                return repair_cyrillic_mojibake(str(val).strip())
        return ""

    def _parse_single_entry(self, entry: dict):
        version = str(entry.get("version") or "").strip()
        parse_version(version)

        package_name = str(entry.get("package") or "").strip()
        if (
            not package_name
            or Path(package_name).name != package_name
            or "/" in package_name
            or "\\" in package_name
            or not package_name.lower().endswith(UPDATE_PACKAGE_SUFFIX)
        ):
            raise UpdateError("В manifest.json указано небезопасное имя пакета обновления.")

        package_file_id = str(entry.get("package_file_id") or "").strip()

        expected_hash = str(entry.get("sha256") or "").strip().lower()
        if not _SHA256_RE.fullmatch(expected_hash):
            raise UpdateError("В manifest.json отсутствует корректный SHA-256 пакета.")

        try:
            manifest_size = int(entry.get("size", 0) or 0)
        except (TypeError, ValueError) as error:
            raise UpdateError("В manifest.json указан некорректный размер пакета.") from error
        if manifest_size <= 0:
            raise UpdateError("В manifest.json отсутствует корректный размер пакета.")

        notes = self._extract_notes(entry)

        try:
            database_schema = int(entry.get("database_schema", CURRENT_SCHEMA_VERSION))
        except (TypeError, ValueError) as error:
            raise UpdateError("В manifest.json указана некорректная версия схемы БД.") from error
        if database_schema < 0:
            raise UpdateError("В manifest.json указана некорректная версия схемы БД.")

        return (
            version,
            package_name,
            package_file_id,
            expected_hash,
            manifest_size,
            database_schema,
            notes,
        )

    def _parse_manifest_entries(
        self,
        raw: object,
        current_version: str,
        *,
        direction: str = "newer",
    ) -> list[tuple[str, str, str, str, int, int, str]]:
        if not isinstance(raw, dict):
            raise UpdateError("Некорректный формат manifest.json.")
        if int(raw.get("format_version", 0) or 0) != MANIFEST_FORMAT_VERSION:
            raise UpdateError("Версия формата manifest.json не поддерживается.")

        try:
            parse_version(current_version)
        except ValueError as error:
            raise UpdateError(str(error)) from error

        raw_entries: list[dict] = []
        if "version" in raw:
            raw_entries.append(raw)
        for list_key in ("versions", "releases", "previous_versions", "available_versions", "history"):
            items = raw.get(list_key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and "version" in item:
                        raw_entries.append(item)

        if not raw_entries:
            # Trigger standard version error when manifest has no version field
            try:
                parse_version("")
            except ValueError as error:
                raise UpdateError(str(error)) from error

        by_ver: dict[tuple[int, int, int], tuple[str, str, str, str, int, int, str]] = {}
        for idx, item in enumerate(raw_entries):
            version_str = str(item.get("version") or "").strip()
            try:
                ver_tuple = parse_version(version_str)
            except ValueError as error:
                if idx == 0:
                    raise UpdateError(str(error)) from error
                continue

            if direction == "newer" and not is_newer_version(version_str, current_version):
                continue
            if direction == "older" and not is_older_version(version_str, current_version):
                continue

            try:
                parsed_item = self._parse_single_entry(item)
            except UpdateError:
                if idx == 0 and direction == "newer":
                    raise
                continue
            if ver_tuple not in by_ver:
                by_ver[ver_tuple] = parsed_item

        return [by_ver[k] for k in sorted(by_ver.keys(), reverse=True)]

    def _parse_manifest(self, raw: object, current_version: str):
        matches = self._parse_manifest_entries(raw, current_version, direction="newer")
        if not matches:
            return None
        return matches[0]

    # ------------------------------------------------------------------
    # Database compatibility check & pre-rollback preservation
    # ------------------------------------------------------------------
    def _find_reference_db_for_version(self, target_version: str) -> Path | None:
        """Locate the most recent backup DB matching target_version if one exists."""
        updates_backup_root = self._backups_root / "updates"
        candidates: list[Path] = []
        if updates_backup_root.is_dir():
            for folder in sorted(updates_backup_root.iterdir(), reverse=True):
                if not folder.is_dir() or folder.name.endswith(".creating"):
                    continue
                manifest_file = folder / "backup_manifest.json"
                db_file = folder / "database" / "requests.db"
                if not db_file.is_file():
                    continue
                if manifest_file.is_file():
                    try:
                        meta = loads_json_with_fallback(manifest_file)
                        if str(meta.get("source_version") or "").strip() == str(target_version).strip():
                            return db_file
                    except Exception:
                        pass
                candidates.append(db_file)
        return candidates[0] if candidates else None

    def check_database_compatibility(
        self,
        info: UpdateInfo,
        current_version: str | None = None,
    ) -> DatabaseRollbackComparison:
        """Compare the current active database with the target rollback version."""
        ref_db = self._find_reference_db_for_version(info.version)
        return compare_database_for_rollback(
            self._database_path,
            info.database_schema,
            reference_db_path=ref_db,
        )

    def save_database_before_rollback(
        self,
        *,
        current_version: str,
        target_version: str,
        target_schema: int | None = None,
        comparison: DatabaseRollbackComparison | None = None,
    ) -> SavedDatabaseBackup:
        """Save and verify the current database before transitioning to an older version."""
        database = self._database_path
        self.rollback_backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_cur = re.sub(r"[^A-Za-z0-9._-]", "_", str(current_version or "current"))
        safe_tgt = re.sub(r"[^A-Za-z0-9._-]", "_", str(target_version or "older"))
        base_name = f"requests_before_rollback_v{safe_cur}_to_v{safe_tgt}_{stamp}"

        db_backup_path = self.rollback_backups_dir / f"{base_name}.db"
        suffix = 1
        while db_backup_path.exists():
            db_backup_path = self.rollback_backups_dir / f"{base_name}_{suffix}.db"
            suffix += 1
        manifest_path = db_backup_path.with_suffix(".json")

        source_schema = 0
        requests_count = 0
        history_count = 0

        if database.is_file():
            try:
                with closing(sqlite3.connect(database, timeout=30.0)) as conn:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error as error:
                raise UpdateError(
                    f"Не удалось подготовить текущую БД к сохранению перед откатом: {error}"
                ) from error

            temp_db = db_backup_path.with_suffix(".db.tmp")
            temp_db.unlink(missing_ok=True)
            source_conn = None
            target_conn = None
            try:
                source_conn = sqlite3.connect(database, timeout=30.0)
                target_conn = sqlite3.connect(temp_db)
                source_conn.backup(target_conn)
            except sqlite3.Error as error:
                temp_db.unlink(missing_ok=True)
                raise UpdateError(
                    f"Не удалось сохранить текущую БД перед откатом: {error}"
                ) from error
            finally:
                if target_conn is not None:
                    target_conn.close()
                if source_conn is not None:
                    source_conn.close()

            try:
                with closing(sqlite3.connect(temp_db, timeout=10.0)) as check_conn:
                    row = check_conn.execute("PRAGMA integrity_check").fetchone()
                    if not row or str(row[0]).strip().lower() != "ok":
                        raise UpdateError("Проверка целостности сохранённой копии БД не пройдена.")
                    ver_row = check_conn.execute("PRAGMA user_version").fetchone()
                    source_schema = int(ver_row[0] if ver_row else 0)
                    tables = {
                        str(r[0])
                        for r in check_conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                    if "requests" in tables:
                        requests_count = int(
                            check_conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
                        )
                    if "request_history" in tables:
                        history_count = int(
                            check_conn.execute("SELECT COUNT(*) FROM request_history").fetchone()[0]
                        )
                os.replace(temp_db, db_backup_path)
            except Exception:
                temp_db.unlink(missing_ok=True)
                raise

        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        resolved_target_schema = (
            int(target_schema) if target_schema is not None else CURRENT_SCHEMA_VERSION
        )
        metadata = {
            "kind": "pre_rollback_database_backup",
            "created_at": created_at,
            "source_version": str(current_version),
            "target_version": str(target_version),
            "source_schema": source_schema,
            "target_schema": resolved_target_schema,
            "database_file": db_backup_path.name,
            "database_path": str(db_backup_path),
            "requests_count": requests_count,
            "history_count": history_count,
            "has_differences": bool(comparison.has_differences) if comparison is not None else (source_schema != resolved_target_schema),
            "warnings": list(comparison.warnings) if comparison is not None else [],
            "verified": True,
        }
        manifest_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return SavedDatabaseBackup(
            db_backup_path=db_backup_path,
            manifest_path=manifest_path,
            source_version=str(current_version),
            target_version=str(target_version),
            source_schema=source_schema,
            target_schema=resolved_target_schema,
            requests_count=requests_count,
            history_count=history_count,
            created_at=created_at,
        )

    def list_saved_rollback_databases(self) -> list[dict]:
        """Return metadata for databases saved during previous rollbacks."""
        if not self.rollback_backups_dir.is_dir():
            return []
        items: list[dict] = []
        for meta_file in sorted(self.rollback_backups_dir.glob("*.json"), reverse=True):
            try:
                raw = loads_json_with_fallback(meta_file)
                if isinstance(raw, dict):
                    items.append(raw)
            except Exception:
                continue
        return items

    # ------------------------------------------------------------------
    # Google Drive public file transport
    # ------------------------------------------------------------------
    def _new_opener(self):
        class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                validate_download_url(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar),
            _SafeRedirectHandler(),
        )

    def _request(self, opener, url: str, *, timeout: int, headers: dict[str, str] | None = None):
        valid_url = validate_download_url(url)
        request_headers = {"User-Agent": _USER_AGENT, "Accept-Encoding": "identity"}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(valid_url, headers=request_headers)
        try:
            response = opener.open(request, timeout=timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise UpdateNetworkError(
                "Не удалось связаться с Google Drive. Программа продолжает работать; "
                "проверка будет повторена позже."
            ) from error
        final_url = response.geturl()
        if final_url:
            try:
                validate_download_url(final_url)
            except UpdateError as error:
                response.close()
                raise UpdateError(f"Перенаправление на небезопасный URL: {final_url}") from error
        return response

    @staticmethod
    def _confirmation_url(html_text: str) -> str:
        match = re.search(r'href="(/uc\?export=download[^"]+)', html_text)
        if match:
            return "https://docs.google.com" + unescape(match.group(1))

        form_parser = _DownloadFormParser()
        form_parser.feed(html_text)
        if form_parser.action:
            action = unescape(form_parser.action)
            if action.startswith("/"):
                action = "https://drive.google.com" + action
            parts = urllib.parse.urlsplit(action)
            params = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            params.extend(form_parser.fields)
            query = urllib.parse.urlencode(params)
            return urllib.parse.urlunsplit(parts._replace(query=query))

        match = re.search(r'"downloadUrl":"([^"]+)', html_text)
        if match:
            return (
                match.group(1)
                .replace("\\u003d", "=")
                .replace("\\u0026", "&")
                .replace("\\/", "/")
            )
        return ""

    def _open_drive_binary(self, file_id: str, *, timeout: int):
        opener = self._new_opener()
        url = "https://drive.google.com/uc?" + urllib.parse.urlencode(
            {"export": "download", "id": file_id}
        )
        for _ in range(4):
            response = self._request(opener, url, timeout=timeout)
            content_type = str(response.headers.get("Content-Type", "")).lower()
            disposition = str(response.headers.get("Content-Disposition", ""))
            if disposition or not content_type.startswith("text/html"):
                return opener, response, response.geturl()
            try:
                html_bytes = response.read(2 * 1024 * 1024)
            except (OSError, TimeoutError) as error:
                response.close()
                raise UpdateNetworkError("Соединение с Google Drive было прервано.") from error
            finally:
                response.close()
            next_url = self._confirmation_url(html_bytes.decode("utf-8", errors="replace"))
            if not next_url:
                raise UpdateNotReady(
                    "Google Drive не разрешил прямое скачивание файла. "
                    "Проверьте доступ 'Все, у кого есть ссылка'."
                )
            url = validate_download_url(next_url)
        raise UpdateNotReady("Не удалось получить прямую ссылку скачивания Google Drive.")


    def _read_drive_file(self, file_id: str, max_bytes: int) -> bytes:
        _opener, response, _url = self._open_drive_binary(
            file_id, timeout=UPDATE_HTTP_TIMEOUT_SECONDS
        )
        try:
            data = response.read(max_bytes + 1)
        except (OSError, TimeoutError) as error:
            raise UpdateNetworkError("Не удалось скачать manifest.json с Google Drive.") from error
        finally:
            response.close()
        if len(data) > max_bytes:
            raise UpdateError("manifest.json превышает допустимый размер.")
        return data

    # ------------------------------------------------------------------
    # Download/preparation
    # ------------------------------------------------------------------
    def prepare_update(
        self,
        info: UpdateInfo,
        progress: Callable[[int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Path:
        """Download/copy package in background and verify it before installation.

        A remote ``.part`` file is deliberately kept after a network failure or
        user cancellation.  Pressing "Обновить" again resumes from that file.
        The installed application is not touched during this phase.
        """
        self.download_dir.mkdir(parents=True, exist_ok=True)
        destination = self.download_dir / f"DocumentRegistry_{info.version}_update.zip"
        temporary = destination.with_suffix(destination.suffix + ".part")

        if destination.is_file():
            try:
                if destination.stat().st_size == info.size_bytes and sha256_file(destination) == info.sha256:
                    if progress is not None:
                        progress(info.size_bytes, info.size_bytes)
                    self._cleanup_old_downloads(keep=destination, keep_partial=temporary)
                    return destination
            except OSError:
                pass
            destination.unlink(missing_ok=True)

        if info.package_path is not None:
            return self._prepare_local_update(
                info, destination, temporary, progress=progress, should_cancel=should_cancel
            )
        if not info.package_file_id:
            raise UpdateError("В информации об обновлении отсутствует файл пакета Google Drive.")
        return self._prepare_remote_update(
            info, destination, temporary, progress=progress, should_cancel=should_cancel
        )

    def _prepare_local_update(
        self,
        info: UpdateInfo,
        destination: Path,
        temporary: Path,
        *,
        progress: Callable[[int, int], None] | None,
        should_cancel: Callable[[], bool] | None,
    ) -> Path:
        temporary.unlink(missing_ok=True)
        digest = hashlib.sha256()
        copied = 0
        try:
            assert info.package_path is not None
            with info.package_path.open("rb") as source, temporary.open("wb") as target:
                while True:
                    if should_cancel is not None and should_cancel():
                        raise UpdateDownloadCancelled("Скачивание обновления отменено.")
                    chunk = source.read(UPDATE_CHUNK_SIZE)
                    if not chunk:
                        break
                    target.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                    if progress is not None:
                        progress(copied, max(1, info.size_bytes))
            if copied != info.size_bytes:
                raise UpdateError("Размер подготовленного пакета не совпадает с manifest.json.")
            if digest.hexdigest().lower() != info.sha256.lower():
                raise UpdateError("SHA-256 пакета обновления не совпадает с manifest.json.")
            os.replace(temporary, destination)
        except UpdateDownloadCancelled:
            temporary.unlink(missing_ok=True)
            raise
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        self._cleanup_old_downloads(keep=destination, keep_partial=temporary)
        return destination

    def _prepare_remote_update(
        self,
        info: UpdateInfo,
        destination: Path,
        temporary: Path,
        *,
        progress: Callable[[int, int], None] | None,
        should_cancel: Callable[[], bool] | None,
    ) -> Path:
        if temporary.exists():
            try:
                partial_size = temporary.stat().st_size
            except OSError:
                temporary.unlink(missing_ok=True)
                partial_size = 0

            if partial_size > info.size_bytes:
                temporary.unlink(missing_ok=True)
                partial_size = 0
            elif partial_size == info.size_bytes:
                # A previous download can be interrupted after receiving the final
                # byte but before os.replace(). Never request Range: bytes=<size>-:
                # Google Drive answers HTTP 416 for that range. Verify locally first.
                try:
                    partial_hash = sha256_file(temporary)
                except OSError:
                    partial_hash = ""
                if partial_hash.lower() == info.sha256.lower():
                    os.replace(temporary, destination)
                    if progress is not None:
                        progress(info.size_bytes, info.size_bytes)
                    self._cleanup_old_downloads(keep=destination, keep_partial=temporary)
                    return destination
                temporary.unlink(missing_ok=True)
                partial_size = 0

        start_size = temporary.stat().st_size if temporary.exists() else 0
        if progress is not None:
            progress(start_size, info.size_bytes)

        opener, response, resolved_url = self._open_drive_binary(
            info.package_file_id, timeout=UPDATE_DOWNLOAD_TIMEOUT_SECONDS
        )
        response.close()

        headers: dict[str, str] = {}
        if start_size:
            headers["Range"] = f"bytes={start_size}-"
        response = self._request(
            opener,
            resolved_url,
            timeout=UPDATE_DOWNLOAD_TIMEOUT_SECONDS,
            headers=headers,
        )

        status = getattr(response, "status", getattr(response, "getcode", lambda: 200)())
        if start_size and status != 206:
            response.close()
            start_size = 0
            temporary.unlink(missing_ok=True)
            response = self._request(
                opener,
                resolved_url,
                timeout=UPDATE_DOWNLOAD_TIMEOUT_SECONDS,
            )

        try:
            headers_obj = getattr(response, "headers", None)
            content_type = str(
                headers_obj.get_content_type()
                if headers_obj is not None and hasattr(headers_obj, "get_content_type")
                else (headers_obj.get("Content-Type", "") if headers_obj is not None else "")
            ).lower()
        except Exception:
            content_type = ""
        if "text/html" in content_type or "application/xhtml" in content_type:
            response.close()
            temporary.unlink(missing_ok=True)
            raise UpdateNetworkError(
                "Сервер вернул HTML-страницу вместо пакета обновления. Повторите попытку позже."
            )

        mode = "ab" if start_size else "wb"
        done = start_size
        try:
            with temporary.open(mode) as target:
                while True:
                    if should_cancel is not None and should_cancel():
                        raise UpdateDownloadCancelled(
                            "Скачивание приостановлено. Уже загруженная часть сохранена."
                        )
                    try:
                        chunk = response.read(UPDATE_CHUNK_SIZE)
                    except (OSError, TimeoutError, urllib.error.URLError) as error:
                        raise UpdateNetworkError(
                            "Интернет-соединение прервалось. Уже загруженная часть сохранена; "
                            "при следующей попытке скачивание продолжится."
                        ) from error
                    if not chunk:
                        break
                    target.write(chunk)
                    done += len(chunk)
                    if done > info.size_bytes:
                        temporary.unlink(missing_ok=True)
                        raise UpdateError(
                            f"Размер скачиваемого пакета ({done} байт) превысил заявленный в манифесте лимит ({info.size_bytes} байт)."
                        )
                    if progress is not None:
                        progress(done, info.size_bytes)
        finally:
            response.close()

        try:
            actual_size = temporary.stat().st_size
        except OSError as error:
            raise UpdateError(f"Не удалось проверить скачанный пакет: {error}") from error
        if actual_size != info.size_bytes:
            raise UpdateNetworkError(
                "Скачивание завершилось не полностью. Частичный файл сохранён для продолжения."
            )

        actual_hash = sha256_file(temporary)
        if actual_hash.lower() != info.sha256.lower():
            temporary.unlink(missing_ok=True)
            raise UpdateError(
                "SHA-256 скачанного пакета не совпадает с manifest.json. "
                "Повреждённый файл удалён."
            )

        os.replace(temporary, destination)
        self._cleanup_old_downloads(keep=destination, keep_partial=temporary)
        return destination

    # ------------------------------------------------------------------
    # Installation hand-off
    # ------------------------------------------------------------------
    def _prepare_bootstrap_updater(
        self,
        package: str | Path,
        info: UpdateInfo,
        *,
        rollback: bool = False,
    ) -> Path:
        """Extract the updater into Windows TEMP.

        For rollback transitions, prefer the currently installed updater if available,
        since the current version contains the newest schema downgrade logic,
        pre-update backups, and updater UI resilience. For forward updates, use the
        updater carried inside the verified new package.
        """
        if rollback:
            installed_updater = self.paths.executable_dir / UPDATER_RELATIVE_PATH
            if installed_updater.is_file() and installed_updater.stat().st_size > 0:
                bootstrap_root = Path(tempfile.gettempdir()) / "DocumentRegistry" / "updater_bootstrap"
                try:
                    bootstrap_root.mkdir(parents=True, exist_ok=True)
                    bootstrap_dir = Path(
                        tempfile.mkdtemp(
                            prefix=f"rollback_v{str(info.version).replace('.', '_')}_",
                            dir=str(bootstrap_root),
                        )
                    )
                    updater = bootstrap_dir / "DocumentRegistryUpdater.exe"
                    shutil.copy2(installed_updater, updater)
                    return updater
                except OSError as error:
                    logger.warning(
                        "Не удалось скопировать установленный updater в TEMP (%s); используем updater из пакета",
                        error,
                    )

        package_path = Path(package).resolve(strict=False)
        if not package_path.is_file():
            raise UpdateError("Локальный пакет обновления не найден.")
        actual_hash = sha256_file(package_path)
        if actual_hash.lower() != str(info.sha256 or "").lower():
            raise UpdateError(
                "SHA-256 локального пакета не совпадает с manifest.json. "
                "Updater не запущен."
            )

        try:
            archive = zipfile.ZipFile(package_path, "r")
        except (OSError, zipfile.BadZipFile) as error:
            raise UpdateError(f"Не удалось открыть пакет обновления: {error}") from error

        with archive:
            member_name = ""
            for candidate in (UPDATER_PACKAGE_MEMBER, LEGACY_UPDATER_PACKAGE_MEMBER):
                try:
                    info_item = archive.getinfo(candidate)
                except KeyError:
                    continue
                if info_item.is_dir():
                    continue
                if info_item.file_size <= 0 or info_item.file_size > MAX_BOOTSTRAP_UPDATER_BYTES:
                    raise UpdateError("Некорректный размер updater в пакете обновления.")
                member_name = candidate
                break
            if not member_name:
                raise UpdateError(
                    "В пакете обновления отсутствует updater/DocumentRegistryUpdater.exe."
                )

            bootstrap_root = Path(tempfile.gettempdir()) / "DocumentRegistry" / "updater_bootstrap"
            try:
                bootstrap_root.mkdir(parents=True, exist_ok=True)
                bootstrap_dir = Path(
                    tempfile.mkdtemp(
                        prefix=f"v{str(info.version).replace('.', '_')}_",
                        dir=str(bootstrap_root),
                    )
                )
                updater = bootstrap_dir / "DocumentRegistryUpdater.exe"
                extracted_bytes = 0
                with archive.open(member_name, "r") as source, updater.open("wb") as target:
                    while chunk := source.read(65536):
                        extracted_bytes += len(chunk)
                        if extracted_bytes > MAX_BOOTSTRAP_UPDATER_BYTES:
                            raise UpdateError("Некорректный размер updater в пакете обновления.")
                        target.write(chunk)
            except OSError as error:
                raise UpdateError(
                    f"Не удалось подготовить временный модуль обновления: {error}"
                ) from error

        if not updater.is_file() or updater.stat().st_size <= 0:
            raise UpdateError("Временный модуль обновления не был создан.")
        return updater

    def _prepare_ui_test_updater(self) -> Path:
        """Copy the newest locally available updater to TEMP for a safe UI test."""
        install_root = self.paths.executable_dir
        candidates = [
            install_root / LEGACY_PENDING_UPDATER_RELATIVE_PATH,
            install_root / UPDATER_RELATIVE_PATH,
        ]
        source = next((candidate for candidate in candidates if candidate.is_file()), None)
        if source is None:
            raise UpdateError("Не найден модуль обновления для проверки окна установки.")
        bootstrap_root = Path(tempfile.gettempdir()) / "DocumentRegistry" / "updater_ui_test"
        try:
            bootstrap_root.mkdir(parents=True, exist_ok=True)
            bootstrap_dir = Path(tempfile.mkdtemp(prefix="test_", dir=str(bootstrap_root)))
            updater = bootstrap_dir / "DocumentRegistryUpdater.exe"
            shutil.copy2(source, updater)
            return updater
        except OSError as error:
            raise UpdateError(f"Не удалось подготовить тест updater: {error}") from error

    def launch_updater(
        self,
        package: str | Path,
        info: UpdateInfo,
        *,
        rollback: bool = False,
        current_version: str | None = None,
        pre_saved_backup: SavedDatabaseBackup | None = None,
    ) -> subprocess.Popen:
        install_root = self.paths.executable_dir

        if not getattr(sys, "frozen", False):
            raise UpdateError("Установка обновления доступна только в собранной версии программы.")

        executable = install_root / "DocumentRegistry.exe"
        if not executable.is_file():
            raise UpdateError(f"Не найден основной исполняемый файл: {executable}")

        # Always launch the updater from a TEMP directory.
        updater = self._prepare_bootstrap_updater(package, info, rollback=rollback)

        if rollback:
            # Ensure the current database is backed up to backups/rollback/ (unless
            # already saved by UpdateController). The live database remains intact
            # at current schema; candidate database downgrade is performed safely
            # in staging by the standalone updater process after the main application exits.
            if pre_saved_backup is None:
                try:
                    comparison = self.check_database_compatibility(info, current_version)
                except Exception:
                    comparison = None
                self.save_database_before_rollback(
                    current_version=current_version or "current",
                    target_version=info.version,
                    target_schema=info.database_schema,
                    comparison=comparison,
                )

        command = [
            str(updater),
            "--install",
            str(Path(package).resolve(strict=False)),
            "--target",
            str(install_root),
            "--expected-sha256",
            info.sha256,
            "--version",
            info.version,
            "--database-schema",
            str(info.database_schema),
            "--pid",
            str(os.getpid()),
        ]
        if rollback:
            command.append("--rollback")
        # A stale terminal status from the previous update must not satisfy the
        # visibility handshake for this new installer process. Durable history is
        # stored separately, so removing this one-shot status file is safe.
        try:
            (self.paths.data / "update_status.json").unlink(missing_ok=True)
        except OSError:
            logger.debug("Не удалось очистить предыдущий update_status.json", exc_info=True)
        try:
            return subprocess.Popen(command, cwd=updater.parent)
        except OSError as error:
            raise UpdateError(f"Не удалось запустить модуль обновления: {error}") from error

    def wait_for_updater_window(
        self,
        process: subprocess.Popen,
        version: str,
        *,
        timeout_seconds: float = 8.0,
        event_pump: Callable[[], None] | None = None,
    ) -> dict:
        """Wait until the standalone updater confirms a visible status window.

        The updater writes ``update_status.json`` *before* waiting for the main
        process to exit.  This gives the GUI a safe handshake point: if neither
        the native status window nor its console fallback can be shown, the main
        application stays open and installation never reaches the file-swap phase.
        """
        status_path = self.paths.data / "update_status.json"
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        expected_version = str(version or "").strip()
        last_error = ""
        while time.monotonic() < deadline:
            if event_pump is not None:
                try:
                    event_pump()
                except Exception:
                    pass
            if process.poll() is not None:
                raise UpdateError(
                    "Модуль обновления завершился до появления окна установки. "
                    "Откройте Центр диагностики и журнал updater.log."
                )
            try:
                raw = loads_json_with_fallback(status_path)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                raw = None
            if isinstance(raw, dict):
                status_version = str(raw.get("version") or "").strip()
                state = str(raw.get("state") or "").strip().lower()
                if status_version == expected_version and state == "installing":
                    if bool(raw.get("window_started")):
                        return raw
                    if "window_started" in raw:
                        last_error = str(raw.get("window_error") or "").strip()
                        try:
                            process.terminate()
                        except Exception:
                            pass
                        detail = f" Причина: {last_error}" if last_error else ""
                        raise UpdateError(
                            "Updater не смог показать окно состояния установки. "
                            "Установка не начата." + detail
                        )
            time.sleep(0.05)

        try:
            process.terminate()
        except Exception:
            pass
        detail = f" Последняя ошибка: {last_error}" if last_error else ""
        raise UpdateError(
            "Updater не подтвердил запуск окна установки за отведённое время. "
            "Установка не начата." + detail
        )

    def launch_updater_ui_test(self, version: str) -> subprocess.Popen:
        """Show the standalone installer UI without installing anything."""
        if not getattr(sys, "frozen", False):
            raise UpdateError("Тест окна установки доступен только в собранной версии программы.")
        updater = self._prepare_ui_test_updater()
        command = [
            str(updater),
            "--ui-test",
            "--version",
            str(version or "test"),
        ]
        try:
            return subprocess.Popen(command, cwd=updater.parent)
        except OSError as error:
            raise UpdateError(f"Не удалось запустить тест окна установки: {error}") from error

    def finalize_pending_updater(self) -> bool:
        """Promote legacy .next updater left by releases before v1.23.3.

        New packages replace updater.exe atomically from a TEMP bootstrapper and
        never create .next.exe.  Keeping this compatibility cleanup lets an old
        installation recover once and then retire the legacy mechanism.
        """
        updater_dir = self.paths.executable_dir / "updater"
        current = updater_dir / "DocumentRegistryUpdater.exe"
        pending = updater_dir / "DocumentRegistryUpdater.next.exe"
        if not pending.is_file():
            return False
        try:
            updater_dir.mkdir(parents=True, exist_ok=True)
            os.replace(pending, current)
            logger.info("Обновлён legacy DocumentRegistryUpdater.exe из .next")
            return True
        except OSError:
            logger.info("Legacy updater пока занят; замена будет повторена позже")
            return False

    def _cleanup_old_downloads(self, *, keep: Path, keep_partial: Path | None = None) -> None:
        try:
            for candidate in self.download_dir.glob("DocumentRegistry_*_update.zip"):
                if candidate != keep:
                    candidate.unlink(missing_ok=True)
            for partial in self.download_dir.glob("*.part"):
                if keep_partial is None or partial != keep_partial:
                    partial.unlink(missing_ok=True)
        except OSError:
            logger.debug("Не удалось полностью очистить старые пакеты обновления", exc_info=True)
