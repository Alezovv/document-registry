from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.db_migrations import DatabaseRollbackComparison, get_schema_version
from app.text_encoding import repair_cyrillic_mojibake
from app.ui.date_controls import NoWheelComboBox
from app.ui.dialogs.rollback_database_tab import RollbackDatabaseTab
from app.ui.styles import PRIMARY_COLOR, PRIMARY_HOVER
from app.update_service import UpdateError, UpdateInfo, UpdateService


class RollbackVersionDialog(QDialog):
    """Dialog for selecting an older version, viewing its description, and inspecting DB differences."""

    database_restored = Signal()
    database_merged = Signal()

    def __init__(
        self,
        service: UpdateService,
        current_version: str,
        versions: list[UpdateInfo],
        *,
        initial_error: str = "",
        backup_service=None,
        toast=None,
        parent=None,
    ):
        super().__init__(parent)
        self.service = service
        self.backup_service = backup_service
        self.toast = toast
        self.current_version = str(current_version or "").strip()
        self._versions: list[UpdateInfo] = list(versions or [])
        self._selected_info: UpdateInfo | None = None
        self._current_comparison: DatabaseRollbackComparison | None = None
        self._initial_error = str(initial_error or "").strip()

        self.setWindowTitle("Управление версиями и базами данных")
        self.setMinimumSize(840, 580)
        self.resize(880, 630)
        self._build_ui()
        self._populate_versions()

    def selected_update_info(self) -> UpdateInfo | None:
        return self._selected_info

    def current_comparison(self) -> DatabaseRollbackComparison | None:
        return self._current_comparison

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        title = QLabel("Управление версиями и базами данных")
        title.setStyleSheet("font-size:20px; font-weight:750; color:#0F172A;")
        current_schema = get_schema_version(self.service._database_path)
        subtitle = QLabel(
            f"Текущая установленная версия: {self.current_version} (схема БД v{current_schema})."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color:#64748B; font-size:12px;")
        root.addWidget(title)
        root.addWidget(subtitle)

        self.tabs = QTabWidget(self)
        self.tabs.setStyleSheet(
            "QTabWidget::pane { border:1px solid #E2E8F0; border-radius:10px; background:#FFFFFF; }"
            "QTabBar::tab { background:#F1F5F9; color:#475467; font-weight:650; font-size:12px; "
            "padding:8px 18px; border-top-left-radius:8px; border-top-right-radius:8px; margin-right:4px; }"
            f"QTabBar::tab:selected {{ background:#FFFFFF; color:#0F172A; border-bottom:2px solid {PRIMARY_COLOR}; }}"
        )

        # ── Вкладка 1: Откат версии программы ─────────────────
        versions_tab = QWidget()
        v_layout = QVBoxLayout(versions_tab)
        v_layout.setContentsMargins(16, 14, 16, 14)
        v_layout.setSpacing(12)

        if self._initial_error:
            net_banner = QLabel(
                f"Примечание: {repair_cyrillic_mojibake(self._initial_error)}\n"
                "Вы можете выбрать локальный пакет обновления (.zip) вручную."
            )
            net_banner.setWordWrap(True)
            net_banner.setStyleSheet(
                "background:#FFF7ED; color:#9A3412; border:1px solid #FED7AA; "
                "border-radius:9px; padding:9px 12px; font-size:11px; font-weight:600;"
            )
            v_layout.addWidget(net_banner)

        selector_card = QFrame(versions_tab)
        selector_card.setObjectName("selectorCard")
        selector_card.setStyleSheet(
            "QFrame#selectorCard { background:#FFFFFF; border:1px solid #E2E8F0; border-radius:11px; }"
        )
        selector_layout = QVBoxLayout(selector_card)
        selector_layout.setContentsMargins(16, 14, 16, 14)
        selector_layout.setSpacing(8)

        selector_label = QLabel("Выберите версию для отката")
        selector_label.setStyleSheet("font-size:13px; font-weight:700; color:#1E293B; border:none;")
        selector_layout.addWidget(selector_label)

        combo_row = QHBoxLayout()
        combo_row.setSpacing(10)
        self.version_combo = NoWheelComboBox(selector_card)
        self.version_combo.setMinimumHeight(40)
        self.version_combo.currentIndexChanged.connect(self._on_version_changed)

        self.choose_file_button = QPushButton("Выбрать пакет (.zip)…", selector_card)
        self.choose_file_button.setMinimumHeight(40)
        self.choose_file_button.setMinimumWidth(190)
        self.choose_file_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.choose_file_button.setStyleSheet(
            "QPushButton { background:#F8FAFC; color:#344054; border:1px solid #D0D5DD; "
            "border-radius:9px; padding:8px 16px; font-weight:650; } "
            "QPushButton:hover { background:#F1F5F9; border-color:#98A2B3; }"
        )
        self.choose_file_button.clicked.connect(self._choose_local_package)

        combo_row.addWidget(self.version_combo, 1)
        combo_row.addWidget(self.choose_file_button)
        selector_layout.addLayout(combo_row)

        self.version_meta_label = QLabel("")
        self.version_meta_label.setWordWrap(True)
        self.version_meta_label.setStyleSheet("color:#475467; font-size:11px; border:none;")
        selector_layout.addWidget(self.version_meta_label)
        v_layout.addWidget(selector_card)

        desc_label = QLabel("Описание выбранной версии")
        desc_label.setStyleSheet("font-size:13px; font-weight:700; color:#1E293B;")
        v_layout.addWidget(desc_label)

        self.description_box = QTextEdit(versions_tab)
        self.description_box.setReadOnly(True)
        self.description_box.setMinimumHeight(110)
        self.description_box.setStyleSheet(
            "QTextEdit { background:#F8FAFC; color:#1E293B; border:1px solid #E2E8F0; "
            "border-radius:10px; padding:10px; font-size:12px; }"
        )
        v_layout.addWidget(self.description_box, 1)

        self.db_warning_frame = QFrame(versions_tab)
        self.db_warning_frame.setObjectName("dbWarningFrame")
        db_layout = QVBoxLayout(self.db_warning_frame)
        db_layout.setContentsMargins(14, 12, 14, 12)
        db_layout.setSpacing(6)
        self.db_warning_title = QLabel()
        self.db_warning_title.setWordWrap(True)
        self.db_warning_title.setStyleSheet("font-size:12px; font-weight:750; border:none;")
        self.db_warning_body = QLabel()
        self.db_warning_body.setWordWrap(True)
        self.db_warning_body.setStyleSheet("font-size:11px; border:none;")
        db_layout.addWidget(self.db_warning_title)
        db_layout.addWidget(self.db_warning_body)
        v_layout.addWidget(self.db_warning_frame)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch()

        self.cancel_button = QPushButton("Отмена", versions_tab)
        self.cancel_button.setMinimumHeight(40)
        self.cancel_button.setMinimumWidth(115)
        self.cancel_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.cancel_button.setStyleSheet(
            "QPushButton { background:#FFFFFF; color:#344054; border:1px solid #D0D5DD; "
            "border-radius:10px; padding:8px 18px; font-weight:650; } "
            "QPushButton:hover { background:#F8FAFC; }"
        )
        self.cancel_button.clicked.connect(self.reject)

        self.rollback_button = QPushButton("Откатиться на выбранную версию", versions_tab)
        self.rollback_button.setMinimumHeight(40)
        self.rollback_button.setMinimumWidth(265)
        self.rollback_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.rollback_button.setStyleSheet(
            f"QPushButton {{ background:{PRIMARY_COLOR}; color:white; border:none; "
            "border-radius:10px; padding:8px 20px; font-weight:700; } "
            f"QPushButton:hover {{ background:{PRIMARY_HOVER}; }} "
            "QPushButton:disabled { background:#CBD5E1; color:#64748B; }"
        )
        self.rollback_button.clicked.connect(self._accept_selected)

        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.rollback_button)
        v_layout.addLayout(buttons)

        self.tabs.addTab(versions_tab, "Доступные версии для отката")

        # ── Вкладка 2: Сохранённые снимки БД ──────────────────
        self.database_tab = RollbackDatabaseTab(
            self.service,
            backup_service=self.backup_service,
            toast=self.toast,
            parent=self,
        )
        self.database_tab.database_restored.connect(self.database_restored.emit)
        self.database_tab.database_merged.connect(self.database_merged.emit)
        self.tabs.addTab(self.database_tab, "Сохранённые снимки БД")
        root.addWidget(self.tabs, 1)

    def _populate_versions(self) -> None:
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        for info in self._versions:
            size_mb = info.size_bytes / (1024 * 1024)
            source_label = "локальный пакет" if info.package_path is not None else "Google Drive"
            label = (
                f"Версия {info.version}  ·  {size_mb:.1f} МБ  ·  "
                f"схема БД v{info.database_schema} ({source_label})"
            )
            self.version_combo.addItem(label, info)
        self.version_combo.blockSignals(False)

        if self._versions:
            self.version_combo.setCurrentIndex(0)
            self._on_version_changed(0)
        else:
            self._selected_info = None
            self._current_comparison = None
            self.version_meta_label.setText(
                "В источнике обновлений пока нет более старых версий. "
                "Нажмите «Выбрать пакет (.zip)…», чтобы указать архив старой версии вручную."
            )
            self.description_box.setPlainText(
                "Доступные старые версии не найдены в manifest.json или локальном кэше."
            )
            self.db_warning_frame.setStyleSheet(
                "QFrame#dbWarningFrame { background:#F8FAFC; border:1px solid #E2E8F0; border-radius:10px; }"
            )
            self.db_warning_title.setStyleSheet("color:#334155; font-size:12px; font-weight:700; border:none;")
            self.db_warning_title.setText("Ожидание выбора версии")
            self.db_warning_body.setStyleSheet("color:#64748B; font-size:11px; border:none;")
            self.db_warning_body.setText(
                "При выборе старой версии программа проверит совместимость базы данных "
                "и автоматически сохранит текущую базу данных перед откатом."
            )
            self.rollback_button.setEnabled(False)

    def _on_version_changed(self, index: int) -> None:
        if index < 0 or index >= len(self._versions):
            self._selected_info = None
            self._current_comparison = None
            self.rollback_button.setEnabled(False)
            return

        info = self._versions[index]
        self._selected_info = info
        self.rollback_button.setEnabled(True)

        size_mb = info.size_bytes / (1024 * 1024)
        source_text = (
            f"Локальный файл ({info.package_path.name})"
            if info.package_path is not None
            else "Публичный канал Google Drive"
        )
        self.version_meta_label.setText(
            f"Переход: {self.current_version} → {info.version}  ·  "
            f"Размер: {size_mb:.1f} МБ  ·  Схема БД версии: v{info.database_schema}  ·  "
            f"Источник: {source_text}"
        )

        notes_text = repair_cyrillic_mojibake((info.notes or "").strip())
        if not notes_text:
            notes_text = f"Версия DocumentRegistry {info.version}.\nПодробное описание изменений для этой сборки не указано."
        self.description_box.setPlainText(notes_text)

        try:
            comparison = self.service.check_database_compatibility(info, self.current_version)
        except Exception as error:
            comparison = None
            self.db_warning_frame.setStyleSheet(
                "QFrame#dbWarningFrame { background:#FFF7ED; border:1px solid #FED7AA; border-radius:10px; }"
            )
            self.db_warning_title.setStyleSheet("color:#9A3412; font-size:12px; font-weight:750; border:none;")
            self.db_warning_title.setText("Предупреждение проверки базы данных")
            self.db_warning_body.setStyleSheet("color:#7C2D12; font-size:11px; border:none;")
            self.db_warning_body.setText(
                f"Не удалось предварительно сравнить схему БД: {error}\n"
                "Текущая база данных будет принудительно сохранена перед откатом."
            )
            return

        self._current_comparison = comparison
        if comparison.has_differences:
            self.db_warning_frame.setStyleSheet(
                "QFrame#dbWarningFrame { background:#FFFBEB; border:1px solid #FDE68A; border-radius:10px; }"
            )
            self.db_warning_title.setStyleSheet("color:#92400E; font-size:12px; font-weight:750; border:none;")
            self.db_warning_title.setText(
                "⚠ Обнаружены различия в базе данных (будет выдано предупреждение и сохранена текущая БД)"
            )
            bullet_lines = "\n".join(f"• {line}" for line in comparison.warnings)
            self.db_warning_body.setStyleSheet("color:#78350F; font-size:11px; border:none;")
            self.db_warning_body.setText(
                f"{bullet_lines}\n\n"
                f"Текущая база данных (версии {self.current_version}, схема v{comparison.current_schema}) "
                "будет автоматически сохранена в папку backups/rollback/ перед переходом на более старую версию."
            )
        else:
            self.db_warning_frame.setStyleSheet(
                "QFrame#dbWarningFrame { background:#F0FDF4; border:1px solid #BBF7D0; border-radius:10px; }"
            )
            self.db_warning_title.setStyleSheet("color:#166534; font-size:12px; font-weight:750; border:none;")
            self.db_warning_title.setText(
                f"● Схема базы данных совместима (v{comparison.current_schema})"
            )
            self.db_warning_body.setStyleSheet("color:#14532D; font-size:11px; border:none;")
            self.db_warning_body.setText(
                f"Различий в структуре БД между версиями {self.current_version} и {info.version} не обнаружено. "
                "Перед переходом на более старую версию копия текущей базы данных также будет сохранена в backups/rollback/."
            )

    def _choose_local_package(self) -> None:
        start_dir = str(self.service.download_dir if self.service.download_dir.exists() else Path.home())
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите пакет старой версии (.zip)",
            start_dir,
            "Пакет обновления DocumentRegistry (*.zip)",
        )
        if not file_path:
            return
        try:
            info = self.service.inspect_package_file(
                file_path,
                current_version=self.current_version,
            )
        except UpdateError as error:
            QMessageBox.warning(self, "Некорректный пакет для отката", str(error))
            return

        self._versions = [
            existing for existing in self._versions if existing.version != info.version
        ]
        self._versions.insert(0, info)
        self._populate_versions()
        self.version_combo.setCurrentIndex(0)

    def _accept_selected(self) -> None:
        if self._selected_info is None:
            return
        self.accept()
