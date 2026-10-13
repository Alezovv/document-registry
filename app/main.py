from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import warnings
from time import perf_counter
from typing import Any

from app.app_paths import ensure_runtime_directories, get_app_paths
from app.resource_profile import (
    configure_process_environment,
    detect_resource_profile,
    load_performance_mode,
)

PROGRAM_START = perf_counter()

PATHS = get_app_paths()
BASE_DIR = PATHS.runtime
DATA_DIR = PATHS.data
PERFORMANCE_MODE = load_performance_mode(PATHS.settings)
RESOURCE_PROFILE = detect_resource_profile(PERFORMANCE_MODE)
configure_process_environment(RESOURCE_PROFILE)

DOCUMENTS_DIR = PATHS.documents
LOG_DIR = PATHS.logs
MODELS_DIR = PATHS.models
PADDLE_CACHE_DIR = PATHS.paddle_cache
APP_ICON_PATH = PATHS.app_icon_png


def ensure_directories() -> None:
    ensure_runtime_directories(PATHS)


def configure_logging() -> None:
    ensure_directories()
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        LOG_DIR / "document-registry.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if os.environ.get("DOCUMENT_REGISTRY_CONSOLE_LOG") == "1":
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)


def configure_local_environment() -> None:
    # Resource limits are selected before heavy numeric/OCR imports.
    configure_process_environment(RESOURCE_PROFILE)

    # Keep every Paddle/PaddleX cache in the writable runtime folder. Explicit
    # local model paths are passed to PaddleOCR, so a deployed portable build
    # never needs to download models on the target PC.
    os.environ["PADDLE_HOME"] = str(PADDLE_CACHE_DIR)
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(PADDLE_CACHE_DIR)
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    warnings.filterwarnings("ignore", category=UserWarning, module="paddle")


configure_local_environment()
logger = logging.getLogger(__name__)


def _self_check_output_from_argv(argv: list[str]) -> Path | None:
    try:
        index = argv.index("--self-check-output")
    except ValueError:
        return None
    if index + 1 >= len(argv):
        return None
    return Path(argv[index + 1])


def _argv_value(argv: list[str], flag: str) -> str | None:
    try:
        index = argv.index(flag)
    except ValueError:
        return None
    if index + 1 >= len(argv):
        return None
    return argv[index + 1]


def _run_database_migration() -> int:
    from app.db_migrations import (
        CURRENT_SCHEMA_VERSION,
        capture_invariants,
        migrate_database,
        write_report,
    )

    database_value = _argv_value(sys.argv, "--migrate-database")
    if not database_value:
        return 2
    schema_value = _argv_value(sys.argv, "--target-schema")
    target_schema = int(schema_value) if schema_value is not None else CURRENT_SCHEMA_VERSION
    allow_downgrade = "--allow-downgrade" in sys.argv
    invariants = capture_invariants(
        database_value,
        max_schema=target_schema if allow_downgrade else None,
    )
    report = migrate_database(
        database_value,
        target_schema=target_schema,
        expected_invariants=invariants,
        allow_downgrade=allow_downgrade,
    )
    report_path = _argv_value(sys.argv, "--migration-report")
    if report_path:
        write_report(report, report_path)
    return 0


def _run_database_validation() -> int:
    from app.db_migrations import CURRENT_SCHEMA_VERSION, validate_database, write_report

    database_value = _argv_value(sys.argv, "--validate-database")
    if not database_value:
        return 2
    schema_value = _argv_value(sys.argv, "--expected-schema")
    expected_schema = int(schema_value) if schema_value is not None else CURRENT_SCHEMA_VERSION
    report = validate_database(database_value, expected_schema=expected_schema)
    report_path = _argv_value(sys.argv, "--database-report")
    if report_path:
        write_report(report, report_path)
    return 0


def _run_self_check() -> int:
    from app.self_check import perform_self_check, write_self_check_report

    result = perform_self_check(PATHS)
    output = _self_check_output_from_argv(sys.argv)
    if output is not None:
        write_self_check_report(result, output)
    elif sys.stdout is not None:
        import json
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 2


def _run_ocr_self_check() -> int:
    from app.self_check import perform_ocr_runtime_self_check, write_self_check_report

    result = perform_ocr_runtime_self_check(PATHS)
    output = _self_check_output_from_argv(sys.argv)
    if output is not None:
        write_self_check_report(result, output)
    elif sys.stdout is not None:
        import json
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 2


def _run_ner_self_check() -> int:
    from app.self_check import perform_ner_runtime_self_check, write_self_check_report

    result = perform_ner_runtime_self_check(PATHS)
    output = _self_check_output_from_argv(sys.argv)
    if output is not None:
        write_self_check_report(result, output)
    elif sys.stdout is not None:
        import json
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 2


class PerformanceMonitor:
    """Optional development monitor enabled by DOCUMENT_REGISTRY_PERF=1."""

    def __init__(self, window: Any, interval_ms: int = 1000):
        try:
            import psutil
        except ImportError as error:
            raise RuntimeError("Для performance monitor нужен psutil") from error

        from PySide6.QtCore import QTimer

        self.psutil = psutil
        self.window = window
        self.process = psutil.Process(os.getpid())
        self.peak_ram_mb = 0.0
        self.process.cpu_percent(interval=None)
        self.timer = QTimer(window)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.update_metrics)
        self.timer.start()

    def update_metrics(self) -> None:
        try:
            ram_mb = self.process.memory_info().rss / (1024 * 1024)
            self.peak_ram_mb = max(self.peak_ram_mb, ram_mb)
            cpu_pct = self.process.cpu_percent(interval=None)
            logger.info(
                "PERF ram=%.1fMB peak=%.1fMB cpu=%.1f%% threads=%s",
                ram_mb,
                self.peak_ram_mb,
                cpu_pct,
                self.process.num_threads(),
            )
        except (self.psutil.NoSuchProcess, self.psutil.AccessDenied):
            self.timer.stop()


def main() -> None:
    if "--migrate-database" in sys.argv:
        raise SystemExit(_run_database_migration())
    if "--validate-database" in sys.argv:
        raise SystemExit(_run_database_validation())
    if "--ocr-self-check" in sys.argv:
        raise SystemExit(_run_ocr_self_check())
    if "--ner-self-check" in sys.argv:
        raise SystemExit(_run_ner_self_check())
    if "--self-check" in sys.argv:
        raise SystemExit(_run_self_check())

    ensure_directories()
    configure_logging()
    logger.info("Запуск приложения")
    logger.info("Профиль ресурсов: %s", RESOURCE_PROFILE.summary)

    # Import the GUI only for normal application startup. This keeps the build
    # self-check small and lets it report missing frozen dependencies cleanly.
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication
    from app.window import MainWindow

    app = QApplication(sys.argv)
    if APP_ICON_PATH.is_file():
        app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    window = MainWindow()
    if not app.windowIcon().isNull():
        window.setWindowIcon(app.windowIcon())
    window.showMaximized()

    if os.environ.get("DOCUMENT_REGISTRY_PERF") == "1":
        try:
            window._perf_monitor = PerformanceMonitor(window)
        except Exception:
            logger.exception("Не удалось запустить performance monitor")

    logger.info("Приложение готово за %.3f с", perf_counter() - PROGRAM_START)
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
