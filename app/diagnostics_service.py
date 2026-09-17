from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
        return "\n".join(
            [
                f"Document Registry {__version__}",
                f"OS: {platform.system()} {platform.release()} ({platform.version()})",
                f"Python: {platform.python_version()}",
                f"CPU: {snapshot.get('logical_cpus', 'n/a')} logical",
                f"RAM: {snapshot.get('total_ram', 'n/a')}",
                f"App RAM: {snapshot.get('process_ram', 'n/a')}",
                f"Performance mode: {snapshot.get('performance_mode', 'n/a')}",
                f"OCR threads: {snapshot.get('ocr_threads', 'n/a')}",
                f"OCR DPI: {snapshot.get('ocr_dpi', 'n/a')}",
                f"OCR model: {snapshot.get('ocr_model', 'n/a')}",
                f"DB schema: {db_info['schema_version']}",
                f"DB quick_check: {db_info['quick_check']}",
                f"Records: {db_info['request_count']}",
                f"Documents: {self._document_count()}",
            ]
        )

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
