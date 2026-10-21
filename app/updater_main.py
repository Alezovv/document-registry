from __future__ import annotations

import argparse
from contextlib import closing
import ctypes
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import zipfile

from app import __version__ as UPDATER_VERSION
from app.archive_safety import validate_archive_member_paths
from app.db_migrations import capture_invariants, downgrade_database
from app.text_encoding import loads_json_with_fallback
from app.updater_status import UpdaterDisplay, UpdaterStatusReporter

FORMAT_VERSION = 1
MAIN_EXE = "DocumentRegistry.exe"
INTERNAL_DIR = "_internal"
VERSION_FILE = "version.json"
UPDATER_DIR = "updater"
UPDATER_FILE = "DocumentRegistryUpdater.exe"
DATABASE_RELATIVE = Path("data") / "requests.db"
DEFAULT_DOCUMENTS_RELATIVE = Path("data") / "documents"
_VERSION_RE = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?\s*$")

logger = logging.getLogger("DocumentRegistryUpdater")


class InstallError(RuntimeError):
    pass


def _to_extended_path(path: Path | str) -> str:
    """Return a Windows extended-length path (\\\\?\\...) to bypass MAX_PATH (260 chars)."""
    if not path:
        return ""
    path_str = str(path).replace("/", "\\")
    if sys.platform == "win32" and not path_str.startswith("\\\\?\\"):
        path_str = os.path.abspath(path_str)
        if path_str.startswith("\\\\"):
            return "\\\\?\\UNC\\" + path_str[2:]
        return "\\\\?\\" + path_str
    return path_str


def _parse_version_tuple(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match(str(value or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _is_rollback_transition(current_version: str, target_version: str) -> bool:
    cur = _parse_version_tuple(current_version)
    tgt = _parse_version_tuple(target_version)
    if cur is None or tgt is None:
        return False
    return tgt < cur


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(_to_extended_path(path), "rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


MAX_ARCHIVE_FILES = 15_000
MAX_ARCHIVE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
MAX_ARCHIVE_SINGLE_FILE_BYTES = 500 * 1024 * 1024  # 500 MB


def safe_extract(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(_to_extended_path(archive_path), "r") as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_FILES:
            raise InstallError(
                f"В архиве слишком много файлов ({len(members)} > {MAX_ARCHIVE_FILES})."
            )
        member_paths = validate_archive_member_paths(members, destination, InstallError)
        total_size = 0
        for member in members:
            if member.file_size > MAX_ARCHIVE_SINGLE_FILE_BYTES:
                raise InstallError(
                    f"Файл '{member.filename}' в архиве превышает допустимый размер "
                    f"({member.file_size} > {MAX_ARCHIVE_SINGLE_FILE_BYTES} байт)."
                )
            total_size += member.file_size
            if total_size > MAX_ARCHIVE_TOTAL_BYTES:
                raise InstallError(
                    f"Общий распакованный размер архива ({total_size} байт) "
                    f"превышает лимит ({MAX_ARCHIVE_TOTAL_BYTES} байт)."
                )

        total_extracted = 0
        for member, member_path in zip(members, member_paths):
            if member.is_dir():
                member_path.mkdir(parents=True, exist_ok=True)
                continue
            member_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, open(_to_extended_path(member_path), "wb") as dst:
                member_extracted = 0
                while chunk := src.read(65536):
                    member_extracted += len(chunk)
                    total_extracted += len(chunk)
                    if member_extracted > MAX_ARCHIVE_SINGLE_FILE_BYTES:
                        raise InstallError(
                            f"Файл '{member.filename}' в архиве превышает допустимый размер "
                            f"({member_extracted} > {MAX_ARCHIVE_SINGLE_FILE_BYTES} байт)."
                        )
                    if total_extracted > MAX_ARCHIVE_TOTAL_BYTES:
                        raise InstallError(
                            f"Общий распакованный размер архива ({total_extracted} байт) "
                            f"превышает лимит ({MAX_ARCHIVE_TOTAL_BYTES} байт)."
                        )
                    dst.write(chunk)



def read_staged_manifest(staging: Path, expected_version: str) -> dict:
    manifest = staging / "update_manifest.json"
    if not manifest.is_file():
        raise InstallError("В пакете отсутствует update_manifest.json.")
    try:
        raw = loads_json_with_fallback(manifest)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InstallError(f"Не удалось прочитать внутренний manifest: {error}") from error
    if not isinstance(raw, dict):
        raise InstallError("Внутренний manifest имеет неверный формат.")
    if int(raw.get("format_version", 0) or 0) != FORMAT_VERSION:
        raise InstallError("Формат пакета обновления не поддерживается.")
    if str(raw.get("version") or "").strip() != expected_version:
        raise InstallError("Версия внутри пакета не совпадает с выбранным обновлением.")
    try:
        database_schema = int(raw.get("database_schema", 0) or 0)
    except (TypeError, ValueError) as error:
        raise InstallError("Некорректная версия схемы БД в пакете обновления.") from error
    if database_schema < 0:
        raise InstallError("Некорректная версия схемы БД в пакете обновления.")
    raw["database_schema"] = database_schema
    return raw


def validate_staged_package(staging: Path, expected_version: str) -> Path:
    payload = staging / "payload"
    if not payload.is_dir():
        raise InstallError("Пакет обновления имеет неверную структуру.")
    read_staged_manifest(staging, expected_version)

    if not (payload / MAIN_EXE).is_file() or not (payload / INTERNAL_DIR).is_dir():
        raise InstallError("В пакете отсутствуют обязательные файлы программы.")
    if not (payload / UPDATER_DIR / UPDATER_FILE).is_file():
        raise InstallError("В пакете отсутствует DocumentRegistryUpdater.exe.")

    protected = {"data", "backups", "logs", "models"}
    present = {item.name.lower() for item in payload.iterdir()}
    forbidden = protected.intersection(present)
    if forbidden:
        raise InstallError(
            "Пакет обновления пытается изменить пользовательские данные: "
            + ", ".join(sorted(forbidden))
        )
    return payload


def _configure_logging(target: Path) -> logging.Handler:
    """Attach a dedicated updater log handler and return it for explicit cleanup.

    On Windows an open FileHandler keeps updater.log locked.  The updater can be
    imported and exercised repeatedly by tests, so relying on interpreter
    shutdown to close the file is not sufficient.
    """
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    try:
        logs = target / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(
            logs / "updater.log",
            encoding="utf-8",
        )
    except Exception:
        handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(formatter)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    return handler


def _close_logging(handler: logging.Handler | None) -> None:
    if handler is None:
        return
    try:
        handler.flush()
    except Exception:
        pass
    try:
        logger.removeHandler(handler)
    except Exception:
        pass
    try:
        handler.close()
    except Exception:
        pass


def wait_for_process_exit(pid: int, timeout_seconds: int = 180) -> None:
    if pid <= 0:
        return
    deadline = time.monotonic() + max(1, timeout_seconds)
    if os.name == "nt":
        synchronize = 0x00100000
        wait_object_0 = 0x00000000
        wait_timeout = 0x00000102
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(synchronize, False, int(pid))
        if not handle:
            return
        try:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            result = kernel32.WaitForSingleObject(handle, remaining_ms)
            if result == wait_timeout:
                raise InstallError("Программа не завершилась вовремя. Обновление отменено.")
            if result != wait_object_0:
                raise InstallError("Не удалось дождаться завершения программы.")
            return
        finally:
            kernel32.CloseHandle(handle)

    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)
    raise InstallError("Программа не завершилась вовремя. Обновление отменено.")


def _make_writable(path: str | Path) -> None:
    try:
        path_str = str(path)
        if sys.platform == "win32":
            ctypes.windll.kernel32.SetFileAttributesW(_to_extended_path(path_str), 0x80)  # FILE_ATTRIBUTE_NORMAL
        os.chmod(_to_extended_path(path_str), stat.S_IWRITE)
    except Exception:
        pass


def _terminate_lingering_instances(target: Path) -> None:
    """Terminate any lingering DocumentRegistry.exe instances that might lock DLLs or databases."""
    if os.name != "nt":
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", MAIN_EXE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
        time.sleep(0.5)
    except Exception:
        pass


def _check_disk_space(target: Path, required_mb: int = 500) -> None:
    try:
        usage = shutil.disk_usage(str(target))
        free_mb = usage.free / (1024 * 1024)
        if free_mb < required_mb:
            raise InstallError(
                f"Недостаточно свободного места на диске ({free_mb:.0f} МБ свободно, требуется минимум {required_mb} МБ)."
            )
    except OSError:
        pass


def _remove_path(path: Path, retries: int = 4, delay: float = 0.3) -> None:
    p_ext = _to_extended_path(path)
    if not os.path.exists(p_ext):
        return

    def _on_rm_error(func, p, excinfo):
        _make_writable(p)
        try:
            func(p)
        except Exception:
            pass

    for attempt in range(max(1, retries)):
        if os.path.isdir(p_ext):
            try:
                shutil.rmtree(p_ext, onerror=_on_rm_error)
            except Exception:
                shutil.rmtree(p_ext, ignore_errors=True)
        else:
            _make_writable(p_ext)
            try:
                os.unlink(p_ext)
            except OSError:
                pass
        if not os.path.exists(p_ext):
            return
        time.sleep(delay)


def _move_if_exists(source: Path, destination: Path, retries: int = 6, delay: float = 0.5) -> None:
    src_ext = _to_extended_path(source)
    dst_ext = _to_extended_path(destination)
    if not os.path.exists(src_ext):
        return
    os.makedirs(os.path.dirname(dst_ext), exist_ok=True)

    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            if os.path.exists(dst_ext):
                _remove_path(destination)
            shutil.move(src_ext, dst_ext)
            return
        except (PermissionError, OSError, shutil.Error) as err:
            last_error = err
            logger.warning(
                "Попытка %s/%s перемещения %s -> %s не удалась (%s). Повтор...",
                attempt + 1,
                retries,
                source.name,
                destination.name,
                err,
            )
            if os.path.isdir(src_ext):
                for root, _, files in os.walk(src_ext):
                    for f in files:
                        _make_writable(os.path.join(root, f))
            else:
                _make_writable(src_ext)
            time.sleep(delay)

    if last_error is not None:
        raise last_error


def _rollback_program(target: Path, rollback: Path) -> None:
    logger.warning("Выполняется откат файлов программы")
    saved_prog = rollback / "program"
    saved_prog_ext = _to_extended_path(saved_prog)
    if not os.path.exists(saved_prog_ext):
        return

    _terminate_lingering_instances(target)

    for name in (MAIN_EXE, INTERNAL_DIR, VERSION_FILE):
        saved = saved_prog / name
        if os.path.exists(_to_extended_path(saved)):
            current = target / name
            _remove_path(current)
            _move_if_exists(saved, current)

    saved_updater = saved_prog / UPDATER_DIR / UPDATER_FILE
    if os.path.exists(_to_extended_path(saved_updater)):
        current_updater = target / UPDATER_DIR / UPDATER_FILE
        _remove_path(current_updater)
        _move_if_exists(saved_updater, current_updater)


def _recover_interrupted_rollback(target: Path) -> bool:
    """If a previous install was interrupted mid-swap (e.g. power cut), restore program files."""
    rollback = target / ".update_rollback"
    saved_prog = rollback / "program"
    saved_prog_ext = _to_extended_path(saved_prog)
    if not os.path.exists(saved_prog_ext):
        return False

    saved_exe = saved_prog / MAIN_EXE
    target_exe = target / MAIN_EXE
    recovered = False
    if os.path.isfile(_to_extended_path(saved_exe)) and not os.path.isfile(_to_extended_path(target_exe)):
        logger.warning("Обнаружена прерванная установка: восстанавливаем %s из %s", MAIN_EXE, saved_exe)
        try:
            _move_if_exists(saved_exe, target_exe)
            recovered = True
        except Exception as e:
            logger.error("Не удалось восстановить %s: %s", MAIN_EXE, e)

    saved_internal = saved_prog / INTERNAL_DIR
    target_internal = target / INTERNAL_DIR
    if os.path.isdir(_to_extended_path(saved_internal)) and not os.path.isdir(_to_extended_path(target_internal)):
        try:
            _move_if_exists(saved_internal, target_internal)
            recovered = True
        except Exception:
            pass

    return recovered


def _checkpoint_database(database: Path) -> None:
    db_ext = _to_extended_path(database)
    if not os.path.isfile(db_ext):
        sidecars = [Path(str(database) + suffix) for suffix in ("-wal", "-shm")]
        if any(os.path.exists(_to_extended_path(path)) for path in sidecars):
            raise InstallError(
                "Основной файл БД отсутствует, но остались WAL/SHM-файлы. "
                "Обновление остановлено, чтобы не потерять данные."
            )
        return
    try:
        with closing(sqlite3.connect(db_ext, timeout=30.0)) as conn:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and int(checkpoint[0]) != 0:
                raise InstallError(
                    "Не удалось полностью выполнить checkpoint WAL рабочей базы. "
                    "Обновление остановлено; файлы базы не заменялись."
                )
            # A successful checkpoint alone is not enough if another process can
            # still use the WAL. Switching journal mode must succeed before the
            # activation path removes sidecar files or replaces the main DB.
            mode = conn.execute("PRAGMA journal_mode = DELETE").fetchone()
            if mode and str(mode[0]).lower() != "delete":
                raise InstallError(
                    "Не удалось безопасно переключить рабочую БД из WAL-режима. "
                    "Обновление остановлено; файлы базы не заменялись."
                )
    except InstallError:
        raise
    except sqlite3.Error as error:
        raise InstallError(f"Не удалось подготовить БД к резервному копированию: {error}") from error


def _sqlite_backup(source: Path, destination: Path) -> None:
    src_ext = _to_extended_path(source)
    dst_ext = _to_extended_path(destination)
    os.makedirs(os.path.dirname(dst_ext), exist_ok=True)
    try:
        os.unlink(dst_ext)
    except OSError:
        pass
    source_conn = None
    target_conn = None
    try:
        source_conn = sqlite3.connect(src_ext, timeout=30.0)
        target_conn = sqlite3.connect(dst_ext)
        source_conn.backup(target_conn)
    except sqlite3.Error as error:
        try:
            os.unlink(dst_ext)
        except OSError:
            pass
        raise InstallError(f"Не удалось создать резервную копию БД: {error}") from error
    finally:
        if target_conn is not None:
            target_conn.close()
        if source_conn is not None:
            source_conn.close()


def _validate_sqlite_backup(database: Path) -> None:
    db_ext = _to_extended_path(database)
    if not os.path.isfile(db_ext):
        raise InstallError("Резервная база данных не создана.")
    try:
        with closing(sqlite3.connect(db_ext, timeout=10.0)) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if not row or str(row[0]).strip().lower() != "ok":
                raise InstallError("Проверка целостности pre-update backup БД не пройдена.")
    except sqlite3.Error as error:
        raise InstallError(f"Не удалось проверить pre-update backup БД: {error}") from error


def _database_schema(database: Path) -> int:
    db_ext = _to_extended_path(database)
    if not os.path.isfile(db_ext):
        return 0
    try:
        with closing(sqlite3.connect(db_ext, timeout=10.0)) as conn:
            row = conn.execute("PRAGMA user_version").fetchone()
            return int(row[0] if row else 0)
    except sqlite3.Error as error:
        raise InstallError(f"Не удалось прочитать версию схемы БД: {error}") from error


def _target_version(target: Path) -> str:
    version_file = Path(_to_extended_path(target / VERSION_FILE))
    if not version_file.is_file():
        return "unknown"
    try:
        raw = loads_json_with_fallback(version_file)
        return str(raw.get("version") or "unknown").strip() or "unknown"
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return "unknown"


def _decode_documents_dir(target: Path) -> Path:
    default = target / DEFAULT_DOCUMENTS_RELATIVE
    settings = Path(_to_extended_path(target / "data" / "settings.json"))
    if not settings.is_file():
        return default
    try:
        raw = loads_json_with_fallback(settings)
        value = str(raw.get("documents_dir") or "").strip()
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return default
    if not value:
        return default
    token = "@runtime/"
    if value.startswith(token):
        parts = [part for part in value[len(token):].split("/") if part not in {"", "."}]
        if ".." in parts:
            return default
        return target.joinpath(*parts)
    path = Path(value).expanduser()
    return path if os.path.exists(_to_extended_path(path)) else default


def _copy_tree(source: Path, destination: Path) -> None:
    src_ext = _to_extended_path(source)
    dst_ext = _to_extended_path(destination)
    if not os.path.isdir(src_ext):
        os.makedirs(dst_ext, exist_ok=True)
        return
    os.makedirs(dst_ext, exist_ok=True)

    def _safe_copy2(src: str, dst: str, **kwargs) -> str:
        s = _to_extended_path(src)
        d = _to_extended_path(dst)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        try:
            return shutil.copy2(s, d, **kwargs)
        except OSError as err:
            logger.warning("Не удалось скопировать файл в резервную копию: %s -> %s (%s)", src, dst, err)
            return dst

    try:
        shutil.copytree(src_ext, dst_ext, dirs_exist_ok=True, copy_function=_safe_copy2)
    except shutil.Error as err:
        logger.warning("Предупреждения при резервном копировании документов: %s", err)


def _count_files_and_bytes(root: Path) -> tuple[int, int]:
    root_ext = _to_extended_path(root)
    if not os.path.isdir(root_ext):
        return 0, 0
    count = 0
    total = 0
    for dirpath, _, filenames in os.walk(root_ext):
        for fname in filenames:
            count += 1
            fpath = os.path.join(dirpath, fname)
            try:
                total += os.path.getsize(fpath)
            except OSError:
                pass
    return count, total


def _save_dedicated_rollback_db_copy(
    target: Path,
    completed_db: Path | None,
    *,
    current_version: str,
    target_version: str,
    old_schema: int,
    target_schema: int | None = None,
) -> Path | None:
    """Save the source database into backups/rollback/ when rolling back to an older version."""
    if completed_db is None or not completed_db.is_file():
        return None
    rollback_dir = target / "backups" / "rollback"
    try:
        rollback_dir.mkdir(parents=True, exist_ok=True)
        safe_cur = re.sub(r"[^A-Za-z0-9._-]", "_", str(current_version or "current"))
        safe_tgt = re.sub(r"[^A-Za-z0-9._-]", "_", str(target_version or "older"))
        prefix = f"requests_before_rollback_v{safe_cur}_to_v{safe_tgt}_"

        # Avoid creating a duplicate snapshot if the GUI already saved one right before launching updater
        now_ts = time.time()
        for existing_db in sorted(rollback_dir.glob(f"{prefix}*.db"), reverse=True):
            try:
                if existing_db.is_file() and (now_ts - existing_db.stat().st_mtime) < 180.0:
                    return existing_db
            except OSError:
                continue

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"{prefix}{stamp}"
        dest_db = rollback_dir / f"{base}.db"
        suffix = 1
        while os.path.exists(_to_extended_path(dest_db)):
            dest_db = rollback_dir / f"{base}_{suffix}.db"
            suffix += 1
        shutil.copy2(_to_extended_path(completed_db), _to_extended_path(dest_db))

        requests_count = 0
        history_count = 0
        try:
            with closing(sqlite3.connect(_to_extended_path(dest_db), timeout=10.0)) as conn:
                tables = {
                    str(r[0])
                    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                if "requests" in tables:
                    requests_count = int(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
                if "request_history" in tables:
                    history_count = int(conn.execute("SELECT COUNT(*) FROM request_history").fetchone()[0])
        except sqlite3.Error:
            pass

        meta = {
            "kind": "pre_rollback_database_backup",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_version": str(current_version),
            "target_version": str(target_version),
            "source_schema": int(old_schema),
            "target_schema": int(target_schema if target_schema is not None else old_schema),
            "database_file": dest_db.name,
            "database_path": str(dest_db),
            "requests_count": requests_count,
            "history_count": history_count,
            "verified": True,
        }
        meta_file = _to_extended_path(dest_db.with_suffix(".json"))
        with open(meta_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(meta, ensure_ascii=False, indent=2))
        return dest_db
    except OSError:
        logger.warning("Не удалось записать дополнительную копию БД в backups/rollback", exc_info=True)
        return None


def create_pre_update_backup(
    target: Path,
    *,
    current_version: str,
    target_version: str,
    target_schema: int | None = None,
    is_rollback: bool = False,
) -> tuple[Path, Path | None, int]:
    """Create a permanent pre-update/pre-rollback snapshot of the old DB and PDFs.

    The snapshot is completed before any program or database file is replaced.
    A .creating directory is never accepted as a valid backup.
    """
    database = target / DATABASE_RELATIVE
    documents = _decode_documents_dir(target)
    old_schema = _database_schema(database)

    backup_root = target / "backups" / "updates"
    backup_root.mkdir(parents=True, exist_ok=True)
    source_docs_count, source_docs_bytes = _count_files_and_bytes(documents)
    try:
        db_ext = _to_extended_path(database)
        db_bytes = os.path.getsize(db_ext) if os.path.isfile(db_ext) else 0
        free_bytes = shutil.disk_usage(backup_root).free
        required_bytes = db_bytes + source_docs_bytes + max(64 * 1024 * 1024, (db_bytes + source_docs_bytes) // 10)
        if free_bytes < required_bytes:
            raise InstallError(
                f"Недостаточно свободного места для pre-update backup: "
                f"нужно примерно {required_bytes / (1024 ** 2):.0f} МБ, "
                f"доступно {free_bytes / (1024 ** 2):.0f} МБ."
            )
    except OSError:
        pass
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"{stamp}_v{current_version}_to_v{target_version}"
    final = backup_root / base_name
    suffix = 1
    while os.path.exists(_to_extended_path(final)) or os.path.exists(_to_extended_path(final.with_name(final.name + ".creating"))):
        final = backup_root / f"{base_name}_{suffix}"
        suffix += 1
    temporary = final.with_name(final.name + ".creating")
    os.makedirs(_to_extended_path(temporary), exist_ok=False)

    try:
        backup_db: Path | None = None
        if database.is_file():
            _checkpoint_database(database)
            backup_db = temporary / "database" / "requests.db"
            _sqlite_backup(database, backup_db)
            _validate_sqlite_backup(backup_db)

        docs_target = temporary / "documents"
        _copy_tree(documents, docs_target)
        docs_count, docs_bytes = _count_files_and_bytes(docs_target)
        if source_docs_count > 0 and docs_count == 0:
            raise InstallError(
                "Проверка pre-update backup документов не пройдена: "
                "не удалось скопировать документы в резервную копию."
            )
        if docs_count != source_docs_count or docs_bytes != source_docs_bytes:
            logger.warning(
                "Часть документов не была скопирована в бэкап (возможно, файлы заняты внешними программами): "
                f"скопировано {docs_count}/{source_docs_count} файлов ({docs_bytes}/{source_docs_bytes} байт). "
                "Оригинальные документы в рабочей папке сохранены без изменений."
            )

        extras = temporary / "data"
        for name in ("settings.json", "parser_corrections.json"):
            source = target / "data" / name
            if source.is_file():
                os.makedirs(_to_extended_path(extras), exist_ok=True)
                shutil.copy2(_to_extended_path(source), _to_extended_path(extras / name))

        metadata = {
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_version": current_version,
            "target_version": target_version,
            "database_schema": old_schema,
            "target_database_schema": target_schema if target_schema is not None else old_schema,
            "is_rollback": bool(is_rollback),
            "database": "database/requests.db" if backup_db is not None else None,
            "documents_source": str(documents),
            "documents_count": docs_count,
            "documents_bytes": docs_bytes,
            "verified": True,
            "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        manifest_file = _to_extended_path(temporary / "backup_manifest.json")
        with open(manifest_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(metadata, ensure_ascii=False, indent=2))
        os.replace(_to_extended_path(temporary), _to_extended_path(final))
        completed_db = final / "database" / "requests.db"
        if is_rollback or (target_schema is not None and old_schema > target_schema):
            _save_dedicated_rollback_db_copy(
                target,
                completed_db if completed_db.is_file() else None,
                current_version=current_version,
                target_version=target_version,
                old_schema=old_schema,
                target_schema=target_schema,
            )
        try:
            health = {
                "ok": True,
                "path": str(final),
                "name": final.name,
                "verified_at": metadata["verified_at"],
                "size_bytes": 0,
                "document_count": docs_count,
                "trash_count": 0,
                "kind": "pre_rollback" if is_rollback else "pre_update",
            }
            health_path = target / "data" / "backup_health.json"
            os.makedirs(os.path.dirname(_to_extended_path(health_path)), exist_ok=True)
            temp_health = _to_extended_path(health_path.with_suffix(".tmp"))
            with open(temp_health, "w", encoding="utf-8") as f:
                f.write(json.dumps(health, ensure_ascii=False, indent=2))
            os.replace(temp_health, _to_extended_path(health_path))
        except OSError:
            logger.warning("Не удалось записать backup_health.json", exc_info=True)
        return final, completed_db if completed_db.is_file() else None, old_schema
    except Exception:
        _remove_path(temporary)
        raise


def _subprocess_env(target: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["DOCUMENT_REGISTRY_HOME"] = str(target)
    return env


def _run_executable(command: list[str], *, cwd: Path, target: Path, label: str) -> None:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=_subprocess_env(target),
            check=False,
            timeout=240,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError(f"{label}: {error}") from error
    if completed.returncode != 0:
        raise InstallError(f"{label}: код завершения {completed.returncode}.")


def _run_staged_self_check(payload: Path, target: Path) -> None:
    _run_executable(
        [str(payload / MAIN_EXE), "--self-check"],
        cwd=payload,
        target=target,
        label="Предварительный self-check новой версии не пройден",
    )


def _prepare_migrated_database(
    payload: Path,
    target: Path,
    backup_db: Path | None,
    target_schema: int,
    staging: Path,
) -> Path | None:
    if backup_db is None:
        return None
    migration_dir = staging / "database_migration"
    migration_dir.mkdir(parents=True, exist_ok=True)
    candidate = migration_dir / "requests.db"
    shutil.copy2(_to_extended_path(backup_db), _to_extended_path(candidate))

    # When rolling back to an older version whose database schema is lower than
    # the current database schema, downgrade the candidate DB first while the
    # original database remains safely preserved in the backup.
    candidate_schema = _database_schema(candidate)
    if candidate_schema > target_schema:
        logger.info(
            "Понижение версии схемы копии БД с %s до %s для совместимости со старой версией",
            candidate_schema,
            target_schema,
        )
        try:
            invariants = capture_invariants(candidate, max_schema=target_schema)
            downgrade_database(
                candidate,
                target_schema=target_schema,
                expected_invariants=invariants,
            )
        except Exception as error:
            raise InstallError(
                f"Не удалось адаптировать копию БД к схеме старой версии ({target_schema}): {error}"
            ) from error

    report = migration_dir / "migration_report.json"
    _run_executable(
        [
            str(payload / MAIN_EXE),
            "--migrate-database",
            str(candidate),
            "--target-schema",
            str(target_schema),
            "--migration-report",
            str(report),
        ],
        cwd=payload,
        target=target,
        label="Миграция копии БД не выполнена",
    )
    if not report.is_file():
        raise InstallError("Новая версия не создала отчёт миграции БД.")
    try:
        raw = loads_json_with_fallback(report)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InstallError(f"Не удалось прочитать отчёт миграции БД: {error}") from error
    if not bool(raw.get("ok")) or int(raw.get("schema_version", -1)) != target_schema:
        raise InstallError("Проверка мигрированной БД не пройдена.")
    return candidate


def _activate_program(payload: Path, target: Path, rollback: Path) -> None:
    _terminate_lingering_instances(target)
    try:
        for name in (INTERNAL_DIR, VERSION_FILE):
            _move_if_exists(target / name, rollback / "program" / name)
        current_updater = target / UPDATER_DIR / UPDATER_FILE
        incoming_updater = payload / UPDATER_DIR / UPDATER_FILE
        _move_if_exists(current_updater, rollback / "program" / UPDATER_DIR / UPDATER_FILE)

        _move_if_exists(payload / INTERNAL_DIR, target / INTERNAL_DIR)
        _move_if_exists(payload / VERSION_FILE, target / VERSION_FILE)
        _move_if_exists(incoming_updater, current_updater)

        _move_if_exists(target / MAIN_EXE, rollback / "program" / MAIN_EXE)
        _move_if_exists(payload / MAIN_EXE, target / MAIN_EXE)
    except Exception as error:
        logger.error("Ошибка при замене файлов программы: %s. Немедленный откат...", error)
        _rollback_program(target, rollback)
        raise


def _activate_database(candidate: Path | None, target: Path, rollback: Path) -> bool:
    if candidate is None:
        return False
    database = target / DATABASE_RELATIVE
    database.parent.mkdir(parents=True, exist_ok=True)
    _checkpoint_database(database)
    for suffix in ("-wal", "-shm"):
        _remove_path(Path(str(database) + suffix))

    saved_database = rollback / "database" / "requests.db"
    moved_old_database = False
    try:
        if os.path.exists(_to_extended_path(database)):
            _move_if_exists(database, saved_database)
            moved_old_database = saved_database.is_file()
        shutil.move(_to_extended_path(candidate), _to_extended_path(database))
    except Exception:
        # This helper can fail after it has moved the live DB aside but before
        # the caller receives ``True``. Restore it here so the caller's
        # database_swapped flag is not the sole protection against data loss.
        if moved_old_database and saved_database.is_file():
            try:
                # If a failed cross-volume move left a partial destination, remove
                # that incomplete file before returning the saved live DB.
                if database.exists():
                    _remove_path(database)
                if database.exists():
                    raise OSError("Не удалось удалить частичный файл новой БД")
                shutil.move(_to_extended_path(saved_database), _to_extended_path(database))
            except Exception as rollback_error:
                raise InstallError(
                    "Не удалось установить новую БД и вернуть прежнюю рабочую БД. "
                    f"Прежняя база сохранена в {saved_database}: {rollback_error}"
                ) from rollback_error
        raise
    return True


def _rollback_database(target: Path, rollback: Path, database_was_swapped: bool) -> None:
    if not database_was_swapped:
        return
    logger.warning("Выполняется откат рабочей БД")
    database = target / DATABASE_RELATIVE
    for suffix in ("", "-wal", "-shm"):
        _remove_path(Path(str(database) + suffix))
    saved = rollback / "database" / "requests.db"
    if os.path.isfile(_to_extended_path(saved)):
        database.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(_to_extended_path(saved), _to_extended_path(database))


def _run_installed_checks(target: Path, target_schema: int) -> None:
    executable = target / MAIN_EXE
    _run_executable(
        [str(executable), "--self-check"],
        cwd=target,
        target=target,
        label="Self-check установленной версии не пройден",
    )
    database = target / DATABASE_RELATIVE
    if database.is_file():
        report = target / ".update_database_check.json"
        try:
            _run_executable(
                [
                    str(executable),
                    "--validate-database",
                    str(database),
                    "--expected-schema",
                    str(target_schema),
                    "--database-report",
                    str(report),
                ],
                cwd=target,
                target=target,
                label="Проверка рабочей БД новой версией не пройдена",
            )
        finally:
            report.unlink(missing_ok=True)


def _restart_application(target: Path) -> None:
    executable = target / MAIN_EXE
    if not executable.is_file():
        return
    try:
        subprocess.Popen([str(executable)], cwd=target)
    except OSError:
        logger.exception("Не удалось автоматически запустить DocumentRegistry")


def _install_update_impl(
    package: Path,
    target: Path,
    expected_sha256: str,
    version: str,
    parent_pid: int,
    expected_database_schema: int | None = None,
    status: UpdaterStatusReporter | None = None,
) -> None:
    target = target.resolve(strict=False)
    package = package.resolve(strict=False)
    current_version = _target_version(target)
    is_rollback = _is_rollback_transition(current_version, version)
    if status is not None and is_rollback:
        status.rollback = True
    logger.info(
        "Начало атомарной установки версии %s (текущая: %s, откат: %s)",
        version,
        current_version,
        is_rollback,
    )
    if status:
        status.stage("wait_app", "Ожидаем закрытия DocumentRegistry...", 5)

    wait_for_process_exit(parent_pid)
    _terminate_lingering_instances(target)
    _check_disk_space(target)
    _recover_interrupted_rollback(target)
    if status:
        status.stage("verify_package", "Проверяем пакет обновления...", 12)

    staging = target / ".update_staging"
    rollback = target / ".update_rollback"
    _remove_path(staging)
    _remove_path(rollback)
    os.makedirs(_to_extended_path(staging), exist_ok=True)
    os.makedirs(_to_extended_path(rollback), exist_ok=True)

    program_swapped = False
    database_swapped = False
    backup_dir: Path | None = None

    try:
        if not package.is_file():
            raise InstallError("Локальный пакет обновления не найден.")
        if sha256_file(package).lower() != expected_sha256.lower():
            raise InstallError("SHA-256 локального пакета не совпадает с manifest.json.")

        if status:
            status.stage("extract", "Распаковываем новую версию...", 22)
        safe_extract(package, staging)
        payload = validate_staged_package(staging, version)
        manifest = read_staged_manifest(staging, version)
        target_schema = int(manifest.get("database_schema", 0) or 0)
        if expected_database_schema is not None and int(expected_database_schema) != target_schema:
            raise InstallError("Версия схемы БД во внешнем и внутреннем manifest не совпадает.")

        # The new executable is verified while the old installation is still intact.
        if status:
            status.stage("precheck", "Проверяем новую версию перед установкой...", 34)
        _run_staged_self_check(payload, target)

        if status:
            status.stage("backup", "Создаём резервную копию БД и документов...", 45)
        backup_dir, backup_db, old_schema = create_pre_update_backup(
            target,
            current_version=current_version,
            target_version=version,
            target_schema=target_schema,
            is_rollback=is_rollback or (old_schema_check := _database_schema(target / DATABASE_RELATIVE)) > target_schema,
        )
        logger.info(
            "Резервная копия данных создана: %s (schema %s)",
            backup_dir,
            old_schema,
        )

        # Migration happens only on the backup-derived candidate DB and is run by
        # the target executable (after downgrading if rolling back to an older schema).
        if status:
            status.stage("migrate", "Подготавливаем и проверяем схему БД...", 58)
        migrated_database = _prepare_migrated_database(
            payload,
            target,
            backup_db,
            target_schema,
            staging,
        )

        # Commit phase. From here every changed working file has a temporary
        # rollback copy until all final checks pass.
        if status:
            status.stage("activate", "Устанавливаем выбранную версию программы...", 72)
        _activate_program(payload, target, rollback)
        program_swapped = True

        database_swapped = _activate_database(migrated_database, target, rollback)
        if status:
            status.stage("final_check", "Проверяем установленную программу и БД...", 88)
        _run_installed_checks(target, target_schema)

        _remove_path(rollback)
        logger.info(
            "Версия %s успешно установлена. Backup сохранён: %s",
            version,
            backup_dir,
        )
    except Exception:
        if status:
            status.stage("rollback", "Ошибка установки. Возвращаем предыдущую версию...", 96)
        try:
            _rollback_database(target, rollback, database_swapped)
        except Exception:
            logger.exception("Ошибка отката БД")
        try:
            _rollback_program(target, rollback)
        except Exception:
            logger.exception("Ошибка отката файлов программы")
        saved_database = rollback / "database" / "requests.db"
        if saved_database.is_file():
            # Do not delete the rollback directory if a database recovery failed.
            # Keep the last verified database copy available for manual recovery.
            logger.critical(
                "Предыдущая база сохранена для ручного восстановления: %s",
                saved_database,
            )
        else:
            _remove_path(rollback)
        _restart_application(target)
        raise
    finally:
        _remove_path(staging)

    try:
        _remove_path(package)
    except OSError:
        logger.debug("Не удалось удалить локальный пакет обновления", exc_info=True)

    if status:
        status.stage("restart", "Установка завершена. Запускаем DocumentRegistry...", 98)
    try:
        subprocess.Popen([str(target / MAIN_EXE)], cwd=target)
    except OSError as error:
        # Installation has already passed the staged and installed self-checks.
        # Do not destroy the successful migrated state only because automatic
        # relaunch was blocked by Windows; the user can start the EXE manually.
        raise InstallError(
            f"Обновление установлено, но программу не удалось запустить автоматически: {error}"
        ) from error


def install_update(
    package: Path,
    target: Path,
    expected_sha256: str,
    version: str,
    parent_pid: int,
    expected_database_schema: int | None = None,
) -> None:
    resolved_target = target.resolve(strict=False)
    log_handler = _configure_logging(resolved_target)
    current_version = _target_version(resolved_target)
    is_rollback = _is_rollback_transition(current_version, version)
    status = UpdaterStatusReporter(resolved_target, version, rollback=is_rollback)
    try:
        _install_update_impl(
            package,
            resolved_target,
            expected_sha256,
            version,
            parent_pid,
            expected_database_schema,
            status,
        )
        status.success(
            "Откат версии выполнен. Запускаем программу..."
            if status.rollback
            else "Обновление установлено. Запускаем программу..."
        )
        status.close(success=True)
    except Exception as error:
        logger.exception("Обновление завершилось ошибкой")
        status.failed(
            "Обновление не установлено. Предыдущая версия восстановлена. "
            + str(error)
        )
        status.close(success=False)
        raise
    finally:
        # Critical on Windows: release updater.log before callers/tests try to
        # remove or replace the application directory.
        _close_logging(log_handler)


def show_error(message: str) -> None:
    logger.error(message)
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                str(message),
                "DocumentRegistry — ошибка обновления",
                0x10,
            )
            return
        except Exception:
            pass
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass


def show_info(message: str) -> None:
    logger.info(message)
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                str(message),
                "DocumentRegistry",
                0x40,
            )
            return
        except Exception:
            pass
    try:
        print(message)
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--ui-test", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--install")
    parser.add_argument("--target")
    parser.add_argument("--expected-sha256")
    parser.add_argument("--version")
    parser.add_argument("--database-schema", type=int)
    parser.add_argument("--pid", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_check:
        return 0

    if args.ui_test:
        version = str(args.version or "test")
        display = UpdaterDisplay(version)
        if not display.start():
            show_error("Не удалось показать окно установки: " + (display.error or "неизвестная ошибка"))
            return 3
        for message, progress, delay in (
            ("Проверка окна установки...", 12, 0.45),
            ("Проверяем пакет обновления...", 28, 0.45),
            ("Создаём резервную копию...", 48, 0.45),
            ("Устанавливаем новую версию программы...", 72, 0.55),
            ("Проверяем установленную версию...", 90, 0.45),
            ("Тест окна установки завершён.", 100, 0.8),
        ):
            display.update(message, progress)
            time.sleep(delay)
        display.close(delay_seconds=0.4)
        return 0

    required = (args.install, args.target, args.expected_sha256, args.version)
    if not all(required):
        # Self-healing: if someone launched updater directly or by double-clicking it
        target_dir = Path(sys.executable).resolve().parent
        if target_dir.name.lower() == "updater":
            target_dir = target_dir.parent
        if _recover_interrupted_rollback(target_dir):
            show_info("Восстановлена предыдущая работоспособная версия программы. Запускаем...")
            _restart_application(target_dir)
            return 0
        show_error("Не заданы обязательные параметры установки обновления.")
        return 2

    requested_version = str(args.version or "").strip()
    if requested_version != str(UPDATER_VERSION):
        req_tuple = _parse_version_tuple(requested_version)
        upd_tuple = _parse_version_tuple(str(UPDATER_VERSION))
        is_valid_rollback = bool(
            args.rollback
            and req_tuple is not None
            and (upd_tuple is None or req_tuple <= upd_tuple)
        )
        if not is_valid_rollback:
            show_error(
                "Версия updater не совпадает с версией пакета: "
                f"updater={UPDATER_VERSION}, package={requested_version}. Установка отменена."
            )
            return 4

    try:
        install_update(
            Path(args.install),
            Path(args.target),
            str(args.expected_sha256),
            str(args.version),
            int(args.pid or 0),
            args.database_schema,
        )
        return 0
    except Exception as error:
        show_error(str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
