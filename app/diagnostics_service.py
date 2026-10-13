from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import zipfile

from app import __version__
from app.app_paths import AppPaths, ensure_runtime_directories, get_app_paths
from app.performance_diagnostics import PerformanceDiagnostics, format_memory
from app.self_check import perform_self_check
from app.document_parser import PARSER_VERSION
from app.document_fingerprint import DocumentFingerprintService
from app.update_history import load_update_history
from app.background_task_coordinator import COORDINATOR
from app.resource_profile import resource_profile_from_environment


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    ok: bool
    path: Path | None
    summary: str


class DiagnosticsService:
    """Create a support bundle without user documents or registry contents."""

    def __init__(
        self,
        database,
        document_service=None,
        performance_diagnostics: PerformanceDiagnostics | None = None,
        paths: AppPaths | None = None,
    ) -> None:
        self.database = database
        self.document_service = document_service
        self.performance_diagnostics = performance_diagnostics
        self.paths = ensure_runtime_directories(paths or get_app_paths())

    def copyable_summary(self) -> str:
        snapshot = self._snapshot()
        db_info = self._database_info()
        search_info = self._search_index_info()
        update_info = self._update_status_info()
        backup_info = self._latest_backup_info()
        return "\n".join(
            [
                f"Document Registry {__version__}",
                f"Parser: {PARSER_VERSION}",
                f"OS: {platform.system()} {platform.release()} ({platform.version()})",
                f"Python: {platform.python_version()}",
                f"CPU: {snapshot.get('logical_cpus', 'n/a')} logical",
                f"RAM: {snapshot.get('total_ram', 'n/a')}",
                f"App RAM: {snapshot.get('process_ram', 'n/a')}",
                f"Peak RAM: {snapshot.get('process_peak_ram', 'n/a')}",
                f"RAM budget: {snapshot.get('memory_budget', 'n/a')}",
                f"Memory pressure: {snapshot.get('memory_pressure', 'n/a')}",
                f"Performance mode: {snapshot.get('performance_mode', 'n/a')}",
                f"OCR threads: {snapshot.get('ocr_threads', 'n/a')}",
                f"OCR DPI: {snapshot.get('ocr_dpi', 'n/a')}",
                f"OCR model: {snapshot.get('ocr_model', 'n/a')}",
                f"DB schema: {db_info['schema_version']}",
                f"DB quick_check: {db_info['quick_check']}",
                f"Records: {db_info['request_count']}",
                f"Documents: {self._document_count()}",
                f"Search index: {search_info.get('indexed', 0)}/{search_info.get('requests', 0)}; OCR={search_info.get('with_ocr', 0)}",
                f"Last update: {update_info.get('state', 'n/a')} {update_info.get('version', '')}".rstrip(),
                f"Latest backup: {backup_info.get('name', 'n/a')}",
                f"Last verified backup: {backup_info.get('verified_name', 'n/a')} {backup_info.get('verified_at', '')}".rstrip(),
            ]
        )

    def overview(self) -> dict[str, object]:
        db = self._database_info()
        models = self._model_info()
        snapshot = self._snapshot()
        return {
            "application_version": __version__,
            "parser_version": PARSER_VERSION,
            "database_path": str(self.database.db_path),
            "database_schema": db.get("schema_version", "n/a"),
            "database_size_bytes": int(db.get("size_bytes", 0) or 0),
            "database_check": str(db.get("quick_check", "unknown")),
            "request_count": int(db.get("request_count", 0) or 0),
            "history_count": int(db.get("history_count", 0) or 0),
            "document_count": self._document_count(),
            "ocr_models_present": bool(models.get("all_present")),
            "ocr_model_loaded": snapshot.get("ocr_model", "unknown"),
            "performance_mode": snapshot.get("performance_mode", "n/a"),
            "process_ram": snapshot.get("process_ram", "n/a"),
            "process_peak_ram": snapshot.get("process_peak_ram", "n/a"),
            "memory_budget": snapshot.get("memory_budget", "n/a"),
            "memory_pressure": snapshot.get("memory_pressure", "n/a"),
            "available_ram": snapshot.get("available_ram", "n/a"),
            "search_index": self._search_index_info(),
            "last_update": self._update_status_info(),
            "latest_backup": self._latest_backup_info(),
            "paths": {
                "runtime": str(self.paths.runtime),
                "data": str(self.paths.data),
                "logs": str(self.paths.logs),
                "backups": str(self.paths.backups),
                "documents": str(self.paths.documents),
            },
        }

    def update_history(self, *, limit: int = 250) -> list[dict[str, object]]:
        return load_update_history(self.paths.data, limit=limit)

    def rebuild_search_index(self) -> DiagnosticResult:
        profile = resource_profile_from_environment()
        task_started = False
        if profile.economy_memory_policy:
            task_started = COORDINATOR.try_start("search_rebuild")
            if not task_started:
                return DiagnosticResult(False, None, "Экономный режим: сначала дождитесь завершения OCR/backup.")
        try:
            count = int(self.database.rebuild_search_index())
            return DiagnosticResult(
                ok=True,
                path=None,
                summary=f"Полнотекстовый индекс перестроен. Проиндексировано записей: {count}.",
            )
        finally:
            if task_started:
                COORDINATOR.finish("search_rebuild")

    def scan_duplicates(self) -> DiagnosticResult:
        if self.document_service is None:
            return DiagnosticResult(False, None, "Сервис документов недоступен.")
        fingerprints = DocumentFingerprintService(self.document_service)
        # Upgrade only a small legacy batch per invocation. Repeated diagnostics
        # gradually complete old archives without a long disk/CPU spike.
        upgraded = fingerprints.backfill_missing(limit=25)
        groups = fingerprints.duplicate_groups(backfill=False)
        duplicate_records = sum(len(group.requests) for group in groups)
        if not groups:
            summary = f"Дубликаты среди уже проиндексированных документов не найдены. Обновлено legacy fingerprints: {upgraded}."
        else:
            summary = (
                f"Найдено групп дубликатов: {len(groups)}; "
                f"записей в этих группах: {duplicate_records}. Legacy fingerprints обновлено: {upgraded}."
            )
        return DiagnosticResult(True, None, summary)

    def check_state(self) -> DiagnosticResult:
        check = perform_self_check(self.paths, check_imports=False)
        db_info = self._database_info()
        model_ok = self._model_info()["all_present"]
        ok = check.ok and db_info["quick_check"].lower() == "ok" and model_ok
        failed = [item.details for item in check.items if not item.ok]
        if db_info["quick_check"].lower() != "ok":
            failed.append(f"SQLite quick_check: {db_info['quick_check']}")
        if not model_ok:
            failed.append("Не найдены один или несколько OCR model files")
        summary = "Состояние в норме." if ok else "Обнаружены проблемы: " + "; ".join(failed)
        return DiagnosticResult(ok=ok, path=None, summary=summary)

    def create_package(self, output_dir: str | Path | None = None) -> DiagnosticResult:
        target_dir = Path(output_dir) if output_dir else self.paths.runtime
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        target = target_dir / f"DocumentRegistry_Diagnostics_{stamp}.zip"
        counter = 2
        while target.exists():
            target = target_dir / f"DocumentRegistry_Diagnostics_{stamp}_{counter}.zip"
            counter += 1

        reports = {
            "application.txt": self._application_report(),
            "system.txt": self._system_report(),
            "performance.txt": self._performance_report(),
            "database.txt": self._database_report(),
            "ocr.txt": self._ocr_report(),
            "paths.txt": self._paths_report(),
            "self_check.txt": self._self_check_report(),
            "update_history.txt": self._update_history_report(),
        }

        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in reports.items():
                archive.writestr(name, content)
            archive.writestr("logs/", "")
            for log_path, content in self._sanitized_logs():
                archive.writestr(f"logs/{log_path.name}", content)

        return DiagnosticResult(ok=True, path=target, summary=f"Диагностический пакет создан: {target}")

    def _snapshot(self) -> dict[str, object]:
        if self.performance_diagnostics is None:
            return {}
        snap = self.performance_diagnostics.snapshot()
        return {
            "profile_name": snap.profile_name,
            "performance_mode": snap.performance_mode,
            "logical_cpus": snap.logical_cpus,
            "total_ram": format_memory(snap.total_ram_bytes),
            "available_ram": format_memory(snap.available_ram_bytes),
            "process_ram": format_memory(snap.process_ram_bytes),
            "process_peak_ram": format_memory(snap.process_peak_ram_bytes),
            "memory_budget": format_memory(snap.memory_budget_bytes) if snap.memory_budget_bytes else "без жёсткого лимита",
            "memory_pressure": snap.memory_pressure,
            "process_cpu": f"{snap.process_cpu_percent:.1f}%",
            "ocr_threads": snap.ocr_threads,
            "ocr_dpi": f"{snap.ocr_dpi}/{snap.ocr_retry_dpi}",
            "ocr_model": "loaded" if snap.ocr_model_loaded else "not loaded",
            "memory_mode": snap.memory_mode,
        }

    def _application_report(self) -> str:
        return "\n".join(
            [
                f"version={__version__}",
                f"python={platform.python_version()}",
                f"frozen={bool(getattr(__import__('sys'), 'frozen', False))}",
                f"database_schema_expected={getattr(self.database, 'SCHEMA_VERSION', 'n/a')}",
            ]
        ) + "\n"

    def _system_report(self) -> str:
        return "\n".join(
            [
                f"system={platform.system()}",
                f"release={platform.release()}",
                f"version={platform.version()}",
                f"machine={platform.machine()}",
                f"processor={platform.processor() or 'unknown'}",
                f"logical_cpus={os.cpu_count() or 1}",
            ]
        ) + "\n"

    def _performance_report(self) -> str:
        snapshot = self._snapshot()
        return "\n".join(f"{key}={value}" for key, value in snapshot.items()) + "\n"

    def _database_info(self) -> dict[str, object]:
        db_path = Path(self.database.db_path)
        info: dict[str, object] = {
            "schema_version": "n/a",
            "size_bytes": db_path.stat().st_size if db_path.is_file() else 0,
            "quick_check": "database file not found",
            "request_count": 0,
            "history_count": 0,
        }
        if not db_path.is_file():
            return info
        connection: sqlite3.Connection | None = None
        try:
            # sqlite3.Connection's context manager commits/rolls back but does NOT
            # close the connection. An explicitly closed handle matters on Windows,
            # where TemporaryDirectory cannot remove requests.db while it is open.
            connection = sqlite3.connect(db_path, timeout=5.0)
            version = connection.execute("PRAGMA user_version").fetchone()
            info["schema_version"] = int(version[0] if version else 0)
            check = connection.execute("PRAGMA quick_check").fetchone()
            info["quick_check"] = str(check[0] if check else "unknown")
            row = connection.execute("SELECT COUNT(*) FROM requests").fetchone()
            info["request_count"] = int(row[0] if row else 0)
            row = connection.execute("SELECT COUNT(*) FROM request_history").fetchone()
            info["history_count"] = int(row[0] if row else 0)
        except sqlite3.Error as error:
            info["quick_check"] = f"error: {type(error).__name__}: {error}"
        finally:
            if connection is not None:
                connection.close()
        return info

    def _database_report(self) -> str:
        info = self._database_info()
        return "\n".join(
            [
                f"schema_version={info['schema_version']}",
                f"size_bytes={info['size_bytes']}",
                f"quick_check={info['quick_check']}",
                f"request_count={info['request_count']}",
                f"history_count={info['history_count']}",
                f"document_count={self._document_count()}",
            ]
        ) + "\n"

    def _search_index_info(self) -> dict[str, int]:
        try:
            getter = getattr(self.database, "get_search_index_stats")
            result = getter()
            return {
                "requests": int(result.get("requests", 0)),
                "indexed": int(result.get("indexed", 0)),
                "with_ocr": int(result.get("with_ocr", 0)),
            }
        except Exception:
            return {"requests": 0, "indexed": 0, "with_ocr": 0}

    def _update_status_info(self) -> dict[str, object]:
        path = self.paths.data / "update_status.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            key: payload.get(key)
            for key in (
                "state", "stage", "message", "progress", "version", "updated_at",
                "window_started", "window_mode", "window_error",
            )
            if key in payload
        }

    def _latest_backup_info(self) -> dict[str, object]:
        health_path = self.paths.data / "backup_health.json"
        try:
            health = json.loads(health_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            health = {}
        if not isinstance(health, dict):
            health = {}

        candidates: list[Path] = []
        try:
            candidates.extend(path for path in self.paths.backups.glob("*.zip") if path.is_file())
            updates_dir = self.paths.backups / "updates"
            if updates_dir.is_dir():
                candidates.extend(
                    path for path in updates_dir.iterdir()
                    if path.is_dir() and not path.name.endswith(".creating")
                )
        except OSError:
            candidates = []

        result: dict[str, object] = {}
        if candidates:
            def stamp(path: Path) -> float:
                try:
                    return path.stat().st_mtime
                except OSError:
                    return 0.0
            latest = max(candidates, key=stamp)
            try:
                stat = latest.stat()
                modified = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")
                size = stat.st_size if latest.is_file() else 0
            except OSError:
                modified, size = "", 0
            result.update({
                "name": latest.name,
                "path": str(latest),
                "modified_at": modified,
                "size_bytes": size,
            })

        if health.get("ok"):
            verified_path = Path(str(health.get("path", "")))
            result.update({
                "verified": True,
                "verified_name": str(health.get("name") or verified_path.name),
                "verified_path": str(verified_path),
                "verified_at": str(health.get("verified_at", "")),
                "verified_size_bytes": int(health.get("size_bytes", 0) or 0),
            })
            if not result.get("name"):
                result.update({
                    "name": str(health.get("name") or verified_path.name),
                    "path": str(verified_path),
                    "modified_at": str(health.get("verified_at", "")),
                    "size_bytes": int(health.get("size_bytes", 0) or 0),
                })
        else:
            result["verified"] = False
        return result

    def _model_info(self) -> dict[str, object]:
        root = self.paths.models / "paddle" / "official_models"
        files = {
            "detection": root / "PP-OCRv5_mobile_det_onnx" / "inference.onnx",
            "recognition": root / "eslav_PP-OCRv5_mobile_rec_onnx" / "inference.onnx",
        }
        states = {name: path.is_file() for name, path in files.items()}
        return {"files": files, "states": states, "all_present": all(states.values())}

    def _ocr_report(self) -> str:
        info = self._model_info()
        snapshot = self._snapshot()
        lines = [
            f"model_loaded={snapshot.get('ocr_model', 'unknown')}",
            f"threads={snapshot.get('ocr_threads', 'unknown')}",
            f"dpi={snapshot.get('ocr_dpi', 'unknown')}",
            f"memory_mode={snapshot.get('memory_mode', 'unknown')}",
        ]
        for name, present in info["states"].items():
            lines.append(f"{name}_model_present={present}")
        return "\n".join(lines) + "\n"

    def _paths_report(self) -> str:
        entries = {
            "runtime": self.paths.runtime,
            "data": self.paths.data,
            "documents": self.paths.documents,
            "backups": self.paths.backups,
            "logs": self.paths.logs,
            "models": self.paths.models,
        }
        lines = []
        home = Path.home()
        for name, path in entries.items():
            display = self._safe_path(path, home)
            try:
                usage = shutil.disk_usage(path if path.exists() else path.parent)
                free = usage.free
            except OSError:
                free = 0
            lines.append(f"{name}={display}")
            lines.append(f"{name}_free_bytes={free}")
        return "\n".join(lines) + "\n"

    def _self_check_report(self) -> str:
        result = perform_self_check(self.paths, check_imports=False)
        lines = [f"ok={result.ok}"]
        for item in result.items:
            details = self._sanitize_text(item.details)
            lines.append(f"{item.name}: {'OK' if item.ok else 'FAIL'} | {details}")
        db = self._database_info()
        lines.append(f"database: {'OK' if str(db['quick_check']).lower() == 'ok' else 'FAIL'} | quick_check={db['quick_check']}")
        return "\n".join(lines) + "\n"

    def _update_history_report(self) -> str:
        rows = list(reversed(self.update_history(limit=300)))
        if not rows:
            return "no update history\n"
        lines = []
        for item in rows:
            lines.append(
                " | ".join(
                    str(item.get(key, ""))
                    for key in ("timestamp", "version", "state", "stage", "progress", "message")
                )
            )
        return "\n".join(lines) + "\n"

    def _document_count(self) -> int:
        if self.document_service is not None:
            try:
                return len(self.document_service.list_document_entries())
            except Exception:
                return 0
        try:
            return sum(1 for path in self.paths.documents.iterdir() if path.is_file())
        except OSError:
            return 0

    def _sanitized_logs(self) -> list[tuple[Path, str]]:
        results: list[tuple[Path, str]] = []
        try:
            logs = sorted(
                (path for path in self.paths.logs.glob("*.log*") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:4]
        except OSError:
            return results

        for path in logs:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            kept: list[str] = []
            include_trace = False
            for line in lines[-1500:]:
                is_level = any(token in line for token in (" | WARNING | ", " | ERROR | ", " | CRITICAL | "))
                is_trace = include_trace and (line.startswith(" ") or line.startswith("Traceback") or line.startswith("File "))
                if is_level:
                    include_trace = True
                    kept.append(self._sanitize_text(line))
                elif is_trace:
                    kept.append(self._sanitize_text(line))
                elif include_trace and not line.strip():
                    kept.append("")
                else:
                    include_trace = False
            if kept:
                results.append((path, "\n".join(kept[-400:]) + "\n"))
        return results

    @staticmethod
    def _safe_path(path: Path, home: Path) -> str:
        try:
            relative = path.resolve(strict=False).relative_to(home.resolve(strict=False))
            return str(Path("%USERPROFILE%") / relative)
        except (OSError, ValueError):
            return str(path)

    @classmethod
    def _sanitize_text(cls, value: str) -> str:
        text = str(value or "")
        text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}", "<email>", text)
        text = re.sub(r"(?<!\d)(?:\+?7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)", "<phone>", text)
        text = re.sub(r"(?i)(?:[A-ZА-Я]:\\|/)[^\r\n]*?\.pdf\b", "<PDF>", text)
        text = re.sub(r"\b[А-ЯЁ][а-яё-]{2,}\s+[А-ЯЁ][а-яё-]{2,}\s+[А-ЯЁ][а-яё-]{2,}\b", "<ФИО>", text)
        text = re.sub(
            r"\b[А-ЯЁ][а-яё-]{2,}\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.(?=$|\s|[,;:])",
            "<ФИО>",
            text,
        )
        return text
