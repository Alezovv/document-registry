from __future__ import annotations

from contextlib import closing
from datetime import datetime
from html import escape
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any, Callable

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.db_merge import DatabaseMergeService
from app.db_migrations import CURRENT_SCHEMA_VERSION, get_schema_version, migrate_database
from app.text_encoding import loads_json_with_fallback, repair_cyrillic_mojibake
from app.ui.dialogs.merge_preview_dialog import MergePreviewDialog
from app.ui.styles import PRIMARY_COLOR, PRIMARY_HOVER
from app.update_service import UpdateService


class _DatabaseOperationWorker(QThread):
    """Run slow rollback-tab file/SQLite operations away from the GUI thread."""

    def __init__(self, operation: Callable[[], Any], parent=None):
        super().__init__(parent)
        self._operation = operation
        self.result: Any = None
        self.error: str = ""

    def run(self) -> None:
        try:
            self.result = self._operation()
        except Exception as error:
            self.error = str(error)


class RollbackDatabaseTab(QWidget):
    """
    Вкладка управления сохранёнными при откатах базами данных.
    
    Отображает таблицу снимков из backups/rollback/ с возможностью:
    - Восстановить БД в 1 клик
    - Сравнить снимок с текущей рабочей БД
    - Выполнить слияние (Merge) без потери данных
    - Удалить устаревший снимок
    """

    database_restored = Signal()
    database_merged = Signal()

    def __init__(
        self,
        service: UpdateService,
        *,
        backup_service=None,
        toast=None,
        parent=None,
    ):
        super().__init__(parent)
        self.service = service
        self.backup_service = backup_service
        self.toast = toast
        self._snapshots: list[dict[str, Any]] = []
        self._selected_snapshot: dict[str, Any] | None = None
        self._operation_worker: _DatabaseOperationWorker | None = None
        self._operation_title = "Операция с базой данных"
        self._operation_success_handler: Callable[[Any], None] | None = None
        self._pending_merge_service: DatabaseMergeService | None = None

        self._setup_ui()
        self.refresh()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        desc = QLabel(
            "Список баз данных, сохранённых автоматически перед предыдущими процедурами отката. "
            "Вы можете восстановить любую базу, сравнить её различия с текущим состоянием или объединить записи."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#475467; font-size:12px;")
        layout.addWidget(desc)

        # Таблица снимков
        self.table = QTableWidget(self)
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels([
            "Дата снимка",
            "Переход версий",
            "Схема БД",
            "Запросов / Истории",
            "Целостность",
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setStyleSheet(
            "QTableWidget { background:#FFFFFF; border:1px solid #E2E8F0; "
            "border-radius:8px; gridline-color:#F1F5F9; font-size:12px; }"
            "QHeaderView::section { background:#F8FAFC; color:#334155; font-weight:700; "
            "font-size:12px; padding:8px 10px; border:1px solid #E2E8F0; }"
        )
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(110)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for col, width in ((0, 155), (1, 155), (2, 115), (3, 180)):
            self.table.setColumnWidth(col, width)

        self.table.currentCellChanged.connect(lambda row, col, prev_row, prev_col: self._on_row_changed(row))
        layout.addWidget(self.table, 1)

        # Панель подробностей выбранного снимка
        details_box = QFrame(self)
        details_box.setObjectName("snapshotDetailsBox")
        details_box.setStyleSheet(
            "QFrame#snapshotDetailsBox { background:#F8FAFC; border:1px solid #E2E8F0; border-radius:8px; }"
        )
        det_layout = QVBoxLayout(details_box)
        det_layout.setContentsMargins(12, 9, 12, 9)
        det_layout.setSpacing(4)

        self.details_label = QLabel("Выберите снимок для просмотра подробностей")
        self.details_label.setWordWrap(True)
        self.details_label.setStyleSheet("color:#334155; font-size:11px; border:none;")
        det_layout.addWidget(self.details_label)
        layout.addWidget(details_box)

        # Кнопки действий (строго 4 штатные кнопки с гарантированной шириной без обрезки текста)
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        self.btn_compare = QPushButton("🔍 Сравнить с текущей", self)
        self.btn_compare.setMinimumHeight(38)
        self.btn_compare.setMinimumWidth(190)
        self.btn_compare.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.btn_compare.setStyleSheet(
            "QPushButton { background:#F1F5F9; color:#1E293B; border:1px solid #CBD5E1; "
            "border-radius:8px; padding:6px 14px; font-size:12px; font-weight:650; }"
            "QPushButton:hover { background:#E2E8F0; }"
            "QPushButton:disabled { background:#F8FAFC; color:#94A3B8; border-color:#E2E8F0; }"
        )
        self.btn_compare.clicked.connect(self._on_compare_clicked)

        self.btn_merge = QPushButton("🔀 Объединить с текущей", self)
        self.btn_merge.setMinimumHeight(38)
        self.btn_merge.setMinimumWidth(205)
        self.btn_merge.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.btn_merge.setStyleSheet(
            f"QPushButton {{ background:{PRIMARY_COLOR}; color:white; border:none; "
            "border-radius:8px; padding:6px 16px; font-size:12px; font-weight:700; }"
            f"QPushButton:hover {{ background:{PRIMARY_HOVER}; }}"
            "QPushButton:disabled { background:#CBD5E1; color:#64748B; }"
        )
        self.btn_merge.clicked.connect(self._on_merge_clicked)

        self.btn_restore = QPushButton("🔄 Восстановить эту БД", self)
        self.btn_restore.setMinimumHeight(38)
        self.btn_restore.setMinimumWidth(195)
        self.btn_restore.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.btn_restore.setStyleSheet(
            "QPushButton { background:#FEF3C7; color:#92400E; border:1px solid #FCD34D; "
            "border-radius:8px; padding:6px 14px; font-size:12px; font-weight:650; }"
            "QPushButton:hover { background:#FDE68A; }"
            "QPushButton:disabled { background:#F8FAFC; color:#94A3B8; border-color:#E2E8F0; }"
        )
        self.btn_restore.clicked.connect(self._on_restore_clicked)

        self.btn_delete = QPushButton("🗑️ Удалить", self)
        self.btn_delete.setMinimumHeight(38)
        self.btn_delete.setMinimumWidth(120)
        self.btn_delete.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.btn_delete.setStyleSheet(
            "QPushButton { background:#FFFFFF; color:#DC2626; border:1px solid #FECACA; "
            "border-radius:8px; padding:6px 14px; font-size:12px; font-weight:600; }"
            "QPushButton:hover { background:#FEE2E2; }"
            "QPushButton:disabled { background:#F8FAFC; color:#94A3B8; border-color:#E2E8F0; }"
        )
        self.btn_delete.clicked.connect(self._on_delete_clicked)

        btn_layout.addWidget(self.btn_compare)
        btn_layout.addWidget(self.btn_merge)
        btn_layout.addWidget(self.btn_restore)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_delete)
        layout.addLayout(btn_layout)

        self._set_buttons_enabled(False)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for btn in (
            getattr(self, "btn_compare", None),
            getattr(self, "btn_merge", None),
            getattr(self, "btn_restore", None),
            getattr(self, "btn_delete", None),
        ):
            if btn is not None:
                btn.setEnabled(enabled)

    def refresh(self) -> None:
        """Перезагрузить список сохранённых снимков БД."""
        self._snapshots = self.service.list_saved_rollback_databases()
        self.table.setRowCount(len(self._snapshots))

        for row, snap in enumerate(self._snapshots):
            self._ensure_snapshot_counts(snap)
            created = str(snap.get("created_at") or snap.get("timestamp") or "")[:19].replace("T", " ")
            versions = f"v{snap.get('source_version', '?')} → v{snap.get('target_version', '?')}"
            schemas = f"v{snap.get('source_schema', '?')} → v{snap.get('target_schema', '?')}"
            counts = f"{snap.get('requests_count', '?')} / {snap.get('history_count', '?')}"
            verified = "✅ OK" if snap.get("verified") else "⚠️ Не проверена"

            for col, val in enumerate((created, versions, schemas, counts, verified)):
                item = QTableWidgetItem(val)
                if col in (1, 2, 3, 4):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, col, item)

        if not self._snapshots:
            self.details_label.setText("Сохранённых при откатах баз данных пока нет.")
            self._set_buttons_enabled(False)
            self._selected_snapshot = None
        else:
            self.table.selectRow(0)

    def _on_row_changed(self, row: int) -> None:
        if 0 <= row < len(self._snapshots):
            self._selected_snapshot = self._snapshots[row]
            self._set_buttons_enabled(True)
            self._render_details(self._selected_snapshot)
        else:
            self._selected_snapshot = None
            self._set_buttons_enabled(False)
            self.details_label.setText("Выберите снимок для просмотра подробностей")

    def _render_details(self, snap: dict[str, Any]) -> None:
        db_path = self._resolve_snapshot_db_path(snap)
        size_str = "файл не найден"
        if db_path and db_path.is_file():
            size_mb = db_path.stat().st_size / (1024 * 1024)
            size_str = f"{size_mb:.2f} МБ"

        file_name = escape(db_path.name, quote=False) if db_path else "Не определён"
        src_ver = escape(str(snap.get("source_version")), quote=False)
        src_sch = escape(str(snap.get("source_schema")), quote=False)
        tgt_ver = escape(str(snap.get("target_version")), quote=False)
        tgt_sch = escape(str(snap.get("target_schema")), quote=False)
        req_cnt = escape(str(snap.get("requests_count", "?")), quote=False)
        hist_cnt = escape(str(snap.get("history_count", "?")), quote=False)

        lines = [
            f"<b>Файл снимка:</b> {file_name} ({size_str})",
            f"<b>Переход:</b> версия {src_ver} (схема {src_sch}) → "
            f"версия {tgt_ver} (схема {tgt_sch})",
            f"<b>Записей:</b> запросов — {req_cnt}, записей истории — {hist_cnt}",
        ]
        if snap.get("warnings"):
            cleaned_warnings = [
                escape(repair_cyrillic_mojibake(str(w)), quote=False)
                for w in snap["warnings"]
            ]
            lines.append(f"<b>Предупреждения при откате:</b> {', '.join(cleaned_warnings)}")
        self.details_label.setText("<br>".join(lines))

    def _ensure_snapshot_counts(self, snap: dict[str, Any]) -> None:
        if snap.get("requests_count") is not None and snap.get("history_count") is not None:
            return
        db_path = self._resolve_snapshot_db_path(snap)
        if not db_path or not db_path.is_file():
            return
        try:
            with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
                conn.execute("PRAGMA query_only = ON")
                tables = {
                    str(r[0])
                    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                if snap.get("requests_count") is None and "requests" in tables:
                    snap["requests_count"] = int(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
                if snap.get("history_count") is None and "request_history" in tables:
                    snap["history_count"] = int(conn.execute("SELECT COUNT(*) FROM request_history").fetchone()[0])
        except sqlite3.Error:
            pass

    def _resolve_snapshot_db_path(self, snap: dict[str, Any]) -> Path | None:
        rollback_root = self.service.rollback_backups_dir.resolve()

        filename = snap.get("database_file")
        if filename:
            clean_name = Path(filename).name
            candidate = (rollback_root / clean_name).resolve()
            if candidate.parent == rollback_root and candidate.is_file():
                return candidate

        raw_path = snap.get("database_path")
        if raw_path:
            candidate = Path(raw_path).resolve()
            try:
                if candidate.parent == rollback_root and candidate.is_file():
                    return candidate
            except (ValueError, OSError):
                pass

        return None

    # ── Фоновые операции и создание сервиса с корректными путями ─────────

    def _make_merge_service(self) -> DatabaseMergeService:
        """Use the configured document store, not a guessed data/documents path."""
        paths = getattr(self.service, "paths", None)
        runtime_root = getattr(paths, "runtime", None)
        document_service = getattr(self.backup_service, "documents", None)
        documents_dir = getattr(document_service, "documents_dir", None)
        if runtime_root is None:
            data_dir = Path(self.service._database_path).parent
            runtime_root = data_dir.parent if data_dir.name.lower() == "data" else data_dir
        if documents_dir is None:
            documents_dir = Path(self.service._database_path).parent / "documents"
        return DatabaseMergeService(
            self.service._database_path,
            runtime_root=runtime_root,
            documents_dir=documents_dir,
        )

    def _start_database_operation(
        self,
        title: str,
        operation: Callable[[], Any],
        on_success: Callable[[Any], None],
    ) -> None:
        current = self._operation_worker
        if current is not None and current.isRunning():
            return
        self._operation_title = title
        self._operation_success_handler = on_success
        self._set_buttons_enabled(False)
        if getattr(self, "table", None) is not None:
            self.table.setEnabled(False)
        worker = _DatabaseOperationWorker(operation, parent=self)
        self._operation_worker = worker
        worker.finished.connect(self._on_database_operation_finished)
        worker.start(QThread.Priority.LowPriority)

    def _wait_for_operation(self, timeout_ms: int = 5000) -> None:
        """Wait for background worker to complete and pump Qt events (used by tests/shutdown)."""
        worker = self._operation_worker
        if worker is not None and worker.isRunning():
            worker.wait(timeout_ms)
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.processEvents()

    def _on_database_operation_finished(self) -> None:
        # Process the outcome only after QThread has fully stopped, so a success
        # callback may close the parent dialog without destroying a running thread.
        worker = self._operation_worker
        success_handler = self._operation_success_handler
        self._operation_worker = None
        self._operation_success_handler = None
        if worker is not None:
            error = worker.error
            result = worker.result
            worker.deleteLater()
        else:
            error = "Фоновая операция завершилась без результата."
            result = None
        if getattr(self, "table", None) is not None:
            self.table.setEnabled(True)
        self._set_buttons_enabled(self._selected_snapshot is not None)
        if error:
            self._pending_merge_service = None
            QMessageBox.critical(self, self._operation_title, error)
            return
        if success_handler is not None:
            success_handler(result)

    def _on_merge_operation_succeeded(self, result) -> None:
        self._pending_merge_service = None
        if result.is_full_success:
            QMessageBox.information(
                self,
                "Слияние выполнено",
                f"Слияние успешно завершено!\n\n{result.summary}\n\n"
                "Перед слиянием создана точка восстановления текущей базы.",
            )
            self.database_merged.emit()
        elif result.is_partial:
            error_details = "\n".join(result.errors[:10])
            if len(result.errors) > 10:
                error_details += f"\n...и ещё {len(result.errors) - 10} ошибок"
            QMessageBox.warning(
                self,
                "Слияние выполнено частично",
                f"Слияние выполнено с замечаниями по отдельным записям.\n\n{result.summary}\n\n"
                f"Ошибки:\n{error_details}\n\n"
                "Перед слиянием создана точка восстановления текущей базы.",
            )
            self.database_merged.emit()
        else:
            QMessageBox.critical(
                self,
                "Ошибка слияния",
                "Произошла ошибка при слиянии данных:\n" + "\n".join(result.errors[:15]),
            )

    def _on_restore_operation_succeeded(self, restored_path: Path) -> None:
        QMessageBox.information(
            self,
            "Восстановление завершено",
            "База данных успешно восстановлена из выбранного снимка.\n"
            "Текущая база предварительно сохранена в резервную копию.",
        )
        self.database_restored.emit()

    def _restore_snapshot_file(self, db_path: Path) -> Path:
        """Validate, back up and restore the selected DB snapshot off the GUI thread."""
        live_db = Path(self.service._database_path)
        live_db.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            prefix=f".{live_db.name}.restore_candidate_",
            suffix=".tmp",
            dir=live_db.parent,
            delete=False,
        )
        candidate_tmp = Path(handle.name)
        handle.close()
        try:
            # Validate a private same-volume candidate before touching the live DB.
            shutil.copy2(db_path, candidate_tmp)
            with closing(sqlite3.connect(candidate_tmp, timeout=5.0)) as test_conn:
                test_conn.execute("PRAGMA query_only = ON")
                check = test_conn.execute("PRAGMA integrity_check").fetchone()
                if not check or check[0] != "ok":
                    detail = check[0] if check else "неизвестная ошибка"
                    raise RuntimeError(f"Целостность файла снимка нарушена: {detail}")

            snap_schema = get_schema_version(candidate_tmp)
            if snap_schema > CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Снимок создан в более новой версии программы (схема v{snap_schema}, "
                    f"текущая v{CURRENT_SCHEMA_VERSION}). Восстановление невозможно без обновления программы."
                )
            if snap_schema < CURRENT_SCHEMA_VERSION:
                migrate_database(candidate_tmp, target_schema=CURRENT_SCHEMA_VERSION)
                with closing(sqlite3.connect(candidate_tmp, timeout=5.0)) as test_conn:
                    test_conn.execute("PRAGMA query_only = ON")
                    check = test_conn.execute("PRAGMA integrity_check").fetchone()
                    if not check or check[0] != "ok":
                        raise RuntimeError("Целостность базы нарушена после миграции схемы.")

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            if self.backup_service:
                self.backup_service.create_backup(f"pre_snapshot_restore_{timestamp}")
            else:
                backup_target = live_db.with_name(
                    f"{live_db.stem}.before_restore_{timestamp}{live_db.suffix}"
                )
                try:
                    with closing(sqlite3.connect(live_db, timeout=30.0)) as src, closing(
                        sqlite3.connect(backup_target)
                    ) as dst:
                        src.backup(dst)
                except Exception:
                    with closing(sqlite3.connect(live_db, timeout=10.0)) as ck_conn:
                        ck_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    shutil.copy2(live_db, backup_target)

            # Preferred path uses SQLite's backup API. Only the fallback performs
            # an atomic same-volume replacement, after both WALs are checkpointed.
            try:
                with closing(sqlite3.connect(candidate_tmp, timeout=10.0)) as src_conn, closing(
                    sqlite3.connect(live_db, timeout=10.0)
                ) as dst_conn:
                    dst_conn.execute("PRAGMA busy_timeout = 10000")
                    src_conn.backup(dst_conn)
                    # The backup API has committed a consistent destination at
                    # this point. A busy checkpoint must not turn a successful
                    # restore into the fallback file-replacement path.
                    try:
                        dst_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except sqlite3.Error:
                        pass
            except Exception:
                with closing(sqlite3.connect(candidate_tmp, timeout=10.0)) as candidate_conn:
                    candidate_conn.execute("PRAGMA busy_timeout = 10000")
                    checkpoint = candidate_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if checkpoint and int(checkpoint[0]) != 0:
                        raise RuntimeError("Не удалось выполнить checkpoint WAL для снимка базы.")
                    mode = candidate_conn.execute("PRAGMA journal_mode = DELETE").fetchone()
                    if mode and str(mode[0]).lower() != "delete":
                        raise RuntimeError("Не удалось отключить WAL у снимка базы.")
                with closing(sqlite3.connect(live_db, timeout=10.0)) as live_conn:
                    live_conn.execute("PRAGMA busy_timeout = 10000")
                    checkpoint = live_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if checkpoint and int(checkpoint[0]) != 0:
                        raise RuntimeError("Не удалось выполнить checkpoint WAL рабочей базы.")
                    mode = live_conn.execute("PRAGMA journal_mode = DELETE").fetchone()
                    if mode and str(mode[0]).lower() != "delete":
                        raise RuntimeError("Не удалось безопасно переключить рабочую базу из WAL.")
                os.replace(candidate_tmp, live_db)
            return live_db
        finally:
            candidate_tmp.unlink(missing_ok=True)
            Path(f"{candidate_tmp}-wal").unlink(missing_ok=True)
            Path(f"{candidate_tmp}-shm").unlink(missing_ok=True)

    # ── Обработчики кнопок ────────────────────────────────────

    def _on_compare_clicked(self) -> None:
        if not self._selected_snapshot:
            return
        db_path = self._resolve_snapshot_db_path(self._selected_snapshot)
        if not db_path or not db_path.is_file():
            QMessageBox.critical(self, "Ошибка", "Файл базы данных снимка не найден на диске.")
            return

        merge_service = self._make_merge_service()
        self._start_database_operation(
            "Сравнение баз данных",
            lambda: merge_service.analyze_merge(db_path),
            self._on_compare_analysis_ready,
        )

    def _on_compare_analysis_ready(self, analysis) -> None:
        dialog = MergePreviewDialog(analysis, compare_only=True, parent=self)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def _on_merge_clicked(self) -> None:
        if not self._selected_snapshot:
            return
        db_path = self._resolve_snapshot_db_path(self._selected_snapshot)
        if not db_path or not db_path.is_file():
            QMessageBox.critical(self, "Ошибка", "Файл базы данных снимка не найден на диске.")
            return

        merge_service = self._make_merge_service()
        self._pending_merge_service = merge_service
        self._start_database_operation(
            "Анализ баз данных",
            lambda: merge_service.analyze_merge(db_path),
            self._on_merge_analysis_ready,
        )

    def _on_merge_analysis_ready(self, analysis) -> None:
        merge_service = self._pending_merge_service
        if merge_service is None:
            QMessageBox.critical(self, "Ошибка слияния", "Сервис слияния не был сохранён.")
            return

        dialog = MergePreviewDialog(analysis, compare_only=False, parent=self)
        try:
            accepted = dialog.exec() == MergePreviewDialog.DialogCode.Accepted
            params = dialog.get_merge_params() if accepted else None
        finally:
            dialog.deleteLater()
        if not accepted or params is None:
            self._pending_merge_service = None
            return

        strategy, resolutions = params
        self._start_database_operation(
            "Слияние баз данных",
            lambda: merge_service.execute_merge(
                analysis,
                strategy=strategy,
                conflict_resolutions=resolutions,
                backup_service=self.backup_service,
            ),
            self._on_merge_operation_succeeded,
        )

    def _on_restore_clicked(self) -> None:
        if not self._selected_snapshot:
            return
        db_path = self._resolve_snapshot_db_path(self._selected_snapshot)
        if not db_path or not db_path.is_file():
            QMessageBox.critical(self, "Ошибка", "Файл базы данных снимка не найден на диске.")
            return

        confirm = QMessageBox.question(
            self,
            "Подтверждение восстановления",
            f"Вы уверены, что хотите восстановить базу данных из снимка от "
            f"{self._selected_snapshot.get('created_at', '')}?\n\n"
            "Текущая рабочая база будет предварительно скопирована в безопасный архив. "
            "После восстановления данные рабочей базы будут полностью заменены состоянием из снимка.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        # Validation, backup, migration and restore can touch hundreds of MB.
        # Keep the UI responsive and disable conflicting actions until completion.
        self._start_database_operation(
            "Восстановление базы данных",
            lambda: self._restore_snapshot_file(db_path),
            self._on_restore_operation_succeeded,
        )

    def _on_delete_clicked(self) -> None:
        if not self._selected_snapshot:
            return
        confirm = QMessageBox.question(
            self,
            "Удаление снимка",
            "Удалить этот снимок и его манифест из каталога backups/rollback/?\n"
            "Это действие необратимо.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        db_path = self._resolve_snapshot_db_path(self._selected_snapshot)
        if db_path and db_path.is_file():
            # Гарантируем, что файл удаляется только внутри rollback_backups_dir
            if db_path.resolve().parent == self.service.rollback_backups_dir.resolve():
                try:
                    db_path.unlink()
                except Exception as e:
                    QMessageBox.warning(self, "Внимание", f"Не удалось удалить файл БД: {e}")

        # Удаление соответствующего json
        for meta_file in self.service.rollback_backups_dir.glob("*.json"):
            try:
                raw = loads_json_with_fallback(meta_file)
                if raw.get("created_at") == self._selected_snapshot.get("created_at") or (
                    db_path and raw.get("database_file") == db_path.name
                ):
                    meta_file.unlink(missing_ok=True)
                    break
            except Exception:
                continue

        self.refresh()
