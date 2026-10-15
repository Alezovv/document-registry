from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any

from PySide6.QtCore import Qt, Signal
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
from app.db_migrations import CURRENT_SCHEMA_VERSION, migrate_database
from app.text_encoding import loads_json_with_fallback, repair_cyrillic_mojibake
from app.ui.dialogs.merge_preview_dialog import MergePreviewDialog
from app.ui.styles import PRIMARY_COLOR, PRIMARY_HOVER
from app.update_service import UpdateService


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
        self.btn_compare.setEnabled(enabled)
        self.btn_merge.setEnabled(enabled)
        self.btn_restore.setEnabled(enabled)
        self.btn_delete.setEnabled(enabled)

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

        lines = [
            f"<b>Файл снимка:</b> {db_path.name if db_path else 'Не определён'} ({size_str})",
            f"<b>Переход:</b> версия {snap.get('source_version')} (схема {snap.get('source_schema')}) → "
            f"версия {snap.get('target_version')} (схема {snap.get('target_schema')})",
            f"<b>Записей:</b> запросов — {snap.get('requests_count', '?')}, записей истории — {snap.get('history_count', '?')}",
        ]
        if snap.get("warnings"):
            cleaned_warnings = [repair_cyrillic_mojibake(str(w)) for w in snap["warnings"]]
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
        raw_path = snap.get("database_path")
        if raw_path and Path(raw_path).is_file():
            return Path(raw_path)
        filename = snap.get("database_file")
        if filename:
            candidate = self.service.rollback_backups_dir / filename
            if candidate.is_file():
                return candidate
        return None

    # ── Обработчики кнопок ────────────────────────────────────

    def _on_compare_clicked(self) -> None:
        if not self._selected_snapshot:
            return
        db_path = self._resolve_snapshot_db_path(self._selected_snapshot)
        if not db_path or not db_path.is_file():
            QMessageBox.critical(self, "Ошибка", "Файл базы данных снимка не найден на диске.")
            return

        merge_service = DatabaseMergeService(self.service._database_path)
        analysis = merge_service.analyze_merge(db_path)

        dialog = MergePreviewDialog(analysis, compare_only=True, parent=self)
        dialog.exec()

    def _on_merge_clicked(self) -> None:
        if not self._selected_snapshot:
            return
        db_path = self._resolve_snapshot_db_path(self._selected_snapshot)
        if not db_path or not db_path.is_file():
            QMessageBox.critical(self, "Ошибка", "Файл базы данных снимка не найден на диске.")
            return

        merge_service = DatabaseMergeService(self.service._database_path)
        analysis = merge_service.analyze_merge(db_path)

        dialog = MergePreviewDialog(analysis, compare_only=False, parent=self)
        if dialog.exec() != MergePreviewDialog.DialogCode.Accepted:
            return

        strategy, resolutions = dialog.get_merge_params()
        result = merge_service.execute_merge(
            analysis,
            strategy=strategy,
            conflict_resolutions=resolutions,
            backup_service=self.backup_service,
        )

        if result.success:
            QMessageBox.information(
                self,
                "Слияние выполнено",
                f"Слияние успешно завершено!\n\n{result.summary}\n\n"
                "Перед слиянием создана точка восстановления текущей базы.",
            )
            self.database_merged.emit()
        else:
            QMessageBox.critical(
                self,
                "Ошибка слияния",
                f"Произошла ошибка при слиянии данных:\n" + "\n".join(result.errors),
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
            f"Текущая рабочая база будет предварительно скопирована в безопасный архив. "
            f"После восстановления данные рабочей базы будут полностью заменены состоянием из снимка.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        # 1. Защитный бэкап рабочей БД
        if self.backup_service:
            self.backup_service.create_backup("pre_snapshot_restore")
        else:
            cur_db = self.service._database_path
            backup_target = cur_db.with_name(f"{cur_db.stem}.before_restore{cur_db.suffix}")
            import shutil
            shutil.copy2(cur_db, backup_target)

        # 2. Подмена рабочего файла
        try:
            import shutil
            shutil.copy2(db_path, self.service._database_path)
            # Очистка WAL/SHM файлов текущей базы
            wal = self.service._database_path.with_name(self.service._database_path.name + "-wal")
            shm = self.service._database_path.with_name(self.service._database_path.name + "-shm")
            if wal.is_file():
                wal.unlink(missing_ok=True)
            if shm.is_file():
                shm.unlink(missing_ok=True)

            # Ensure the restored database is migrated up to the running app's schema
            migrate_database(self.service._database_path, target_schema=CURRENT_SCHEMA_VERSION)

            QMessageBox.information(
                self,
                "Восстановление завершено",
                "База данных успешно восстановлена из выбранного снимка.\n"
                "Текущая база предварительно сохранена в резервную копию.",
            )
            self.database_restored.emit()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка восстановления", f"Не удалось восстановить базу: {e}")

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
