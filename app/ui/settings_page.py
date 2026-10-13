from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app import __version__
from app.performance_diagnostics import format_memory
from app.resource_profile import PERFORMANCE_MODE_LABELS, normalize_performance_mode
from app.update_config import UPDATE_DRIVE_FOLDER_URL
from app.ui.date_controls import NoWheelComboBox
from app.ui.styles import PRIMARY_COLOR, PRIMARY_HOVER
from app.ui.widgets import create_card


class SettingsPage(QWidget):
    save_requested = Signal(str, bool, str, str)
    update_check_requested = Signal(str)
    rollback_requested = Signal(str)
    ocr_check_requested = Signal()
    backup_requested = Signal()
    restore_requested = Signal()
    legacy_import_requested = Signal()
    diagnostics_center_requested = Signal()
    state_check_requested = Signal()
    diagnostic_package_requested = Signal()
    open_logs_requested = Signal()
    copy_info_requested = Signal()

    def __init__(
        self,
        documents_dir: str | Path,
        confirm_delete: bool,
        request_service=None,
        performance_summary: str = "",
        performance_diagnostics=None,
        performance_mode: str = "auto",
        auto_backup_path: str | Path | None = None,
        update_source_dir: str | Path = "",
        parent=None,
    ):
        super().__init__(parent)
        self.request_service = request_service
        self.performance_summary = performance_summary
        self.performance_diagnostics = performance_diagnostics
        self.performance_mode = normalize_performance_mode(performance_mode)
        self.auto_backup_path = Path(auto_backup_path) if auto_backup_path else None
        self.update_source_dir = str(update_source_dir or "").strip()
        self._diagnostic_labels: dict[str, QLabel] = {}
        self._build_ui(Path(documents_dir), confirm_delete, self.update_source_dir)

        self._diagnostics_timer = QTimer(self)
        self._diagnostics_timer.setInterval(3000)
        self._diagnostics_timer.timeout.connect(self.refresh_performance_info)

        self.refresh_storage_info()
        self.refresh_performance_info()

    def _build_ui(self, documents_dir: Path, confirm_delete: bool, update_source_dir: str):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(scroll)
        content.setObjectName('settingsContent')
        content.setStyleSheet('QWidget#settingsContent { background-color: transparent; }')
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 8, 0)
        root.setSpacing(16)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        header = QVBoxLayout()
        header.setSpacing(3)
        title = QLabel("Настройки")
        title.setStyleSheet("font-size:28px; font-weight:750; color:#0F172A; letter-spacing:-0.35px;")
        description = QLabel("Хранилище, резервные копии и состояние приложения")
        description.setStyleSheet("color:#64748B; font-size:13px; font-weight:500;")
        header.addWidget(title)
        header.addWidget(description)
        root.addLayout(header)

        storage_card = create_card()
        storage_layout = QVBoxLayout(storage_card)
        storage_layout.setContentsMargins(22, 20, 22, 20)
        storage_layout.setSpacing(10)
        storage_title = self._section_title("Хранилище документов")
        storage_description = QLabel("Папка, в которой приложение хранит исходные документы.")
        storage_description.setStyleSheet("color:#667085; font-size:12px;")
        storage_layout.addWidget(storage_title)
        storage_layout.addWidget(storage_description)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.documents_path = QLineEdit(str(documents_dir.resolve()))
        self.documents_path.setToolTip(str(documents_dir.resolve()))
        self.documents_path.setMinimumHeight(40)
        browse = QPushButton("Выбрать папку")
        browse.setMinimumHeight(40)
        browse.setStyleSheet(self._secondary_button_style())
        browse.clicked.connect(self._choose_folder)
        row.addWidget(self.documents_path, 1)
        row.addWidget(browse)
        storage_layout.addLayout(row)
        root.addWidget(storage_card)

        backup_card = create_card()
        backup_layout = QVBoxLayout(backup_card)
        backup_layout.setContentsMargins(22, 20, 22, 20)
        backup_layout.setSpacing(10)
        backup_title = self._section_title("Резервное копирование")
        backup_hint = QLabel(
            "Ручная копия содержит базу, PDF и корзину. Автобэкап обновляется раз в 7 дней "
            "и хранит одну последнюю копию."
        )
        backup_hint.setWordWrap(True)
        backup_hint.setStyleSheet("color:#667085; font-size:12px;")
        backup_buttons = QHBoxLayout()
        backup_buttons.setSpacing(10)
        create_backup = QPushButton("Создать копию")
        create_backup.setMinimumHeight(40)
        create_backup.setStyleSheet(
            "QPushButton { background:#12B76A; color:white; border:none; border-radius:10px; "
            "padding:8px 18px; font-weight:700; } "
            "QPushButton:hover { background:#039855; } "
            "QPushButton:pressed { background:#027A48; }"
        )
        restore_backup = QPushButton("Восстановить из копии")
        restore_backup.setMinimumHeight(40)
        restore_backup.setStyleSheet(
            "QPushButton { background:#F79009; color:white; border:none; border-radius:10px; "
            "padding:8px 18px; font-weight:700; } "
            "QPushButton:hover { background:#DC6803; } "
            "QPushButton:pressed { background:#B54708; }"
        )
        create_backup.setToolTip("Создать ZIP-копию базы данных, документов и корзины")
        restore_backup.setToolTip("Восстановить данные из ранее созданной резервной копии")
        create_backup.clicked.connect(self.backup_requested.emit)
        restore_backup.clicked.connect(self.restore_requested.emit)
        backup_buttons.addWidget(create_backup)
        backup_buttons.addWidget(restore_backup)
        backup_buttons.addStretch()
        self.auto_backup_info = QLabel()
        self.auto_backup_info.setStyleSheet(
            "background:#F0FDF4; color:#166534; border:1px solid #BBF7D0; "
            "border-radius:9px; padding:9px 11px; font-size:11px; font-weight:650;"
        )
        self.auto_backup_info.setWordWrap(True)
        backup_layout.addWidget(backup_title)
        backup_layout.addWidget(backup_hint)
        backup_layout.addWidget(self.auto_backup_info)
        backup_layout.addLayout(backup_buttons)
        root.addWidget(backup_card)

        update_card = create_card()
        update_layout = QVBoxLayout(update_card)
        update_layout.setContentsMargins(22, 20, 22, 20)
        update_layout.setSpacing(10)
        update_title = self._section_title("Обновления и управление версиями")
        update_hint = QLabel(
            "Основной режим: программа сама проверяет обновления вперёд в публичной папке Google Drive "
            "при запуске и затем раз в час. Дополнительно доступен откат на предыдущие версии с выбором "
            "сборки, просмотром её описания и автоматическим сохранением текущей базы данных."
        )
        update_hint.setWordWrap(True)
        update_hint.setStyleSheet("color:#667085; font-size:12px;")
        update_layout.addWidget(update_title)
        update_layout.addWidget(update_hint)

        update_source = QLabel(
            f'<a href="{UPDATE_DRIVE_FOLDER_URL}">Открыть папку обновлений Google Drive</a>'
            " · ссылка встроена в программу"
        )
        update_source.setOpenExternalLinks(True)
        update_source.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        update_source.setStyleSheet("color:#475467; font-size:11px;")
        update_layout.addWidget(update_source)

        # Backward-compatible hidden field: older settings.json may still contain
        # update_source_dir, but v1.18.4+ no longer needs a local Drive folder.
        self.update_source_path = QLineEdit(update_source_dir)
        self.update_source_path.hide()

        update_actions = QHBoxLayout()
        update_actions.setSpacing(10)
        self.update_version_label = QLabel(f"Установлена версия {__version__}")
        self.update_version_label.setStyleSheet("color:#344054; font-size:12px; font-weight:650;")

        self.check_update_button = QPushButton("Проверить обновления")
        self.check_update_button.setMinimumHeight(40)
        self.check_update_button.setStyleSheet(
            f"QPushButton {{ background-color:{PRIMARY_COLOR}; color:white; border:none; "
            "border-radius:10px; padding:8px 16px; font-weight:700; } "
            f"QPushButton:hover {{ background-color:{PRIMARY_HOVER}; }} "
            "QPushButton:pressed { background-color:#1E40AF; }"
        )
        self.check_update_button.setToolTip("Основной режим: проверить наличие более новой версии и обновиться вперёд")
        self.check_update_button.clicked.connect(
            lambda: self.update_check_requested.emit(self.update_source_path.text().strip())
        )

        self.rollback_button = QPushButton("Откат на старую версию")
        self.rollback_button.setMinimumHeight(40)
        self.rollback_button.setStyleSheet(self._secondary_button_style())
        self.rollback_button.setToolTip(
            "Дополнительная функция: выбрать более старую версию, посмотреть её описание и выполнить откат с сохранением текущей БД"
        )
        self.rollback_button.clicked.connect(
            lambda: self.rollback_requested.emit(self.update_source_path.text().strip())
        )

        update_actions.addWidget(self.update_version_label)
        update_actions.addStretch()
        update_actions.addWidget(self.check_update_button)
        update_actions.addWidget(self.rollback_button)
        update_layout.addLayout(update_actions)

        ocr_check_row = QHBoxLayout()
        ocr_check_row.setSpacing(10)
        ocr_check_hint = QLabel(
            "После обновления можно отдельно проверить, что PaddleOCR, его pipeline и внешние модели запускаются корректно."
        )
        ocr_check_hint.setWordWrap(True)
        ocr_check_hint.setStyleSheet("color:#667085; font-size:11px;")
        check_ocr = QPushButton("Проверить OCR")
        check_ocr.setMinimumHeight(40)
        check_ocr.setStyleSheet(self._secondary_button_style())
        check_ocr.clicked.connect(self.ocr_check_requested.emit)
        ocr_check_row.addWidget(ocr_check_hint, 1)
        ocr_check_row.addWidget(check_ocr)
        update_layout.addLayout(ocr_check_row)
        root.addWidget(update_card)

        legacy_card = create_card()
        legacy_layout = QVBoxLayout(legacy_card)
        legacy_layout.setContentsMargins(22, 20, 22, 20)
        legacy_layout.setSpacing(10)
        legacy_title = self._section_title("Данные из старой версии")
        legacy_hint = QLabel(
            "Переносит старую SQLite-базу в текущий формат. Перед заменой приложение "
            "автоматически создаст резервную копию текущей базы и документов. Функция "
            "ничего не делает в фоне и запускается только по этой кнопке."
        )
        legacy_hint.setWordWrap(True)
        legacy_hint.setStyleSheet("color:#667085; font-size:12px;")
        legacy_button = QPushButton("Импортировать старую базу")
        legacy_button.setMinimumHeight(40)
        legacy_button.setStyleSheet(
            "QPushButton { background:#475467; color:white; border:none; border-radius:10px; "
            "padding:8px 18px; font-weight:700; } "
            "QPushButton:hover { background:#344054; } "
            "QPushButton:pressed { background:#1D2939; }"
        )
        legacy_button.setToolTip(
            "Выбрать базу старой версии, перенести записи и найденные PDF в текущий формат"
        )
        legacy_button.clicked.connect(self.legacy_import_requested.emit)
        legacy_row = QHBoxLayout()
        legacy_row.addWidget(legacy_button)
        legacy_row.addStretch()
        legacy_layout.addWidget(legacy_title)
        legacy_layout.addWidget(legacy_hint)
        legacy_layout.addLayout(legacy_row)
        root.addWidget(legacy_card)

        performance_card = create_card()
        performance_layout = QVBoxLayout(performance_card)
        performance_layout.setContentsMargins(22, 20, 22, 20)
        performance_layout.setSpacing(14)
        performance_title = self._section_title("Производительность")
        performance_hint = QLabel(
            "Режим влияет на число OCR-потоков и стратегию памяти. Качество распознавания не меняется."
        )
        performance_hint.setWordWrap(True)
        performance_hint.setStyleSheet("color:#667085; font-size:12px;")
        performance_layout.addWidget(performance_title)
        performance_layout.addWidget(performance_hint)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(12)
        mode_label = QLabel("Режим нагрузки")
        mode_label.setStyleSheet("color:#344054; font-size:12px; font-weight:700;")
        self.performance_mode_combo = NoWheelComboBox()
        self.performance_mode_combo.setMinimumHeight(40)
        self.performance_mode_combo.setMinimumWidth(220)
        for key in ("economy", "auto", "performance"):
            self.performance_mode_combo.addItem(PERFORMANCE_MODE_LABELS[key], key)
        index = self.performance_mode_combo.findData(self.performance_mode)
        self.performance_mode_combo.setCurrentIndex(max(0, index))
        self.performance_mode_combo.currentIndexChanged.connect(self._update_performance_mode_hint)
        mode_row.addWidget(mode_label)
        mode_row.addStretch()
        mode_row.addWidget(self.performance_mode_combo)
        performance_layout.addLayout(mode_row)

        self.performance_mode_hint = QLabel()
        self.performance_mode_hint.setWordWrap(True)
        self.performance_mode_hint.setStyleSheet(
            "background:#F8FAFC; color:#475467; border:1px solid #EAECF0; "
            "border-radius:9px; padding:9px 11px; font-size:11px;"
        )
        performance_layout.addWidget(self.performance_mode_hint)
        self._update_performance_mode_hint()

        diagnostics_title = QLabel("Текущие значения")
        diagnostics_title.setStyleSheet("color:#344054; font-size:12px; font-weight:750; margin-top:2px;")
        performance_layout.addWidget(diagnostics_title)

        diagnostics_grid = QGridLayout()
        diagnostics_grid.setHorizontalSpacing(10)
        diagnostics_grid.setVerticalSpacing(10)
        for column in range(3):
            diagnostics_grid.setColumnStretch(column, 1)
        metrics = (
            ("profile", "Активный профиль"),
            ("cpu", "Процессор"),
            ("system_ram", "Оперативная память"),
            ("app_ram", "Память приложения"),
            ("peak_ram", "Пиковая память"),
            ("ram_budget", "Лимит RAM"),
            ("app_cpu", "CPU приложения"),
            ("memory_mode", "Режим памяти"),
            ("memory_pressure", "Давление памяти"),
        )
        for index, (key, label) in enumerate(metrics):
            tile, value_label = self._diagnostic_tile(label)
            diagnostics_grid.addWidget(tile, index // 3, index % 3)
            self._diagnostic_labels[key] = value_label
        performance_layout.addLayout(diagnostics_grid)
        root.addWidget(performance_card)

        ocr_card = create_card()
        ocr_layout = QVBoxLayout(ocr_card)
        ocr_layout.setContentsMargins(22, 20, 22, 20)
        ocr_layout.setSpacing(12)
        ocr_title = self._section_title("OCR")
        ocr_hint = QLabel(
            "Используется локальный PaddleOCR. Диагностика не загружает модель принудительно "
            "и не меняет выбранный режим производительности."
        )
        ocr_hint.setWordWrap(True)
        ocr_hint.setStyleSheet("color:#667085; font-size:12px;")
        ocr_layout.addWidget(ocr_title)
        ocr_layout.addWidget(ocr_hint)
        ocr_grid = QGridLayout()
        ocr_grid.setHorizontalSpacing(10)
        for column in range(3):
            ocr_grid.setColumnStretch(column, 1)
        for index, (key, label) in enumerate((
            ("ocr_threads", "OCR-потоки"),
            ("ocr_dpi", "OCR-разрешение"),
            ("ocr_model", "OCR-модель"),
        )):
            tile, value_label = self._diagnostic_tile(label)
            ocr_grid.addWidget(tile, 0, index)
            self._diagnostic_labels[key] = value_label
        ocr_layout.addLayout(ocr_grid)
        root.addWidget(ocr_card)

        diagnostics_card = create_card()
        diagnostics_layout = QVBoxLayout(diagnostics_card)
        diagnostics_layout.setContentsMargins(22, 20, 22, 20)
        diagnostics_layout.setSpacing(12)
        diagnostics_layout.addWidget(self._section_title("Диагностика"))
        diagnostics_hint = QLabel(
            "Пакет содержит только технические сведения, состояние БД и очищенные ошибки. "
            "PDF, OCR-текст, содержимое записей, ФИО и заметки не включаются."
        )
        diagnostics_hint.setWordWrap(True)
        diagnostics_hint.setStyleSheet("color:#667085; font-size:12px;")
        diagnostics_layout.addWidget(diagnostics_hint)

        diagnostics_buttons = QHBoxLayout()
        diagnostics_buttons.setSpacing(10)
        for text, signal in (
            ("Открыть центр диагностики", self.diagnostics_center_requested),
            ("Проверить состояние", self.state_check_requested),
            ("Создать диагностический пакет", self.diagnostic_package_requested),
            ("Открыть папку логов", self.open_logs_requested),
            ("Скопировать сведения", self.copy_info_requested),
        ):
            button = QPushButton(text)
            button.setMinimumHeight(40)
            button.setStyleSheet(self._secondary_button_style())
            button.clicked.connect(signal.emit)
            diagnostics_buttons.addWidget(button)
        diagnostics_buttons.addStretch()
        diagnostics_layout.addLayout(diagnostics_buttons)
        self.diagnostics_result_label = QLabel("Диагностика запускается только по запросу пользователя.")
        self.diagnostics_result_label.setWordWrap(True)
        self.diagnostics_result_label.setStyleSheet(
            "background:#F8FAFC; color:#475467; border:1px solid #EAECF0; "
            "border-radius:9px; padding:9px 11px; font-size:11px;"
        )
        diagnostics_layout.addWidget(self.diagnostics_result_label)
        root.addWidget(diagnostics_card)

        recovery_card = create_card()
        recovery_layout = QVBoxLayout(recovery_card)
        recovery_layout.setContentsMargins(24, 22, 24, 22)
        recovery_layout.setSpacing(8)
        recovery_title = self._section_title("Восстановление")
        self.trash_info = QLabel()
        self.trash_info.setStyleSheet("color:#344054; font-size:12px; font-weight:600;")
        hint = QLabel("Файлы корзины очищаются вместе с журналом на вкладке «История».")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#667085; font-size:12px;")
        recovery_layout.addWidget(recovery_title)
        recovery_layout.addWidget(self.trash_info)
        recovery_layout.addWidget(hint)
        root.addWidget(recovery_card)

        database_card = create_card()
        database_layout = QVBoxLayout(database_card)
        database_layout.setContentsMargins(24, 22, 24, 22)
        database_layout.setSpacing(8)
        database_title = self._section_title("База данных")
        database_status = QLabel("●  SQLite · локальная база · готова")
        database_status.setStyleSheet("color:#15803D; font-size:12px; font-weight:650;")
        database_layout.addWidget(database_title)
        database_layout.addWidget(database_status)
        root.addWidget(database_card)

        interface_card = create_card()
        interface_layout = QVBoxLayout(interface_card)
        interface_layout.setContentsMargins(24, 22, 24, 22)
        interface_layout.setSpacing(12)
        interface_title = self._section_title("Интерфейс")
        self.confirm_delete = QCheckBox("Подтверждать удаление записей")
        self.confirm_delete.setChecked(confirm_delete)
        save = QPushButton("Сохранить настройки")
        save.setMinimumHeight(40)
        save.setMaximumWidth(190)
        save.setStyleSheet(
            f"""
            QPushButton {{ background-color:{PRIMARY_COLOR}; color:white; border-radius:10px; padding:8px 18px; font-weight:700; }}
            QPushButton:hover {{ background-color:{PRIMARY_HOVER}; }}
            QPushButton:pressed {{ background-color:#1E40AF; }}
            """
        )
        save.clicked.connect(
            lambda: self.save_requested.emit(
                self.documents_path.text().strip(),
                self.confirm_delete.isChecked(),
                str(self.performance_mode_combo.currentData() or "auto"),
                self.update_source_path.text().strip(),
            )
        )
        interface_layout.addWidget(interface_title)
        interface_layout.addWidget(self.confirm_delete)
        interface_layout.addWidget(save)
        root.addWidget(interface_card)
        root.addStretch()

    @staticmethod
    def _section_title(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-size:16px; font-weight:750; color:#101828;")
        return label

    @staticmethod
    def _secondary_button_style() -> str:
        return (
            "QPushButton { background:#FFFFFF; color:#344054; border:1px solid #D0D5DD; "
            "border-radius:10px; padding:8px 16px; font-weight:650; } "
            "QPushButton:hover { background:#F8FAFC; border-color:#98A2B3; } "
            "QPushButton:pressed { background:#F2F4F7; }"
        )

    @staticmethod
    def _diagnostic_tile(title: str) -> tuple[QFrame, QLabel]:
        tile = QFrame()
        tile.setObjectName("diagnosticTile")
        tile.setMinimumHeight(78)
        tile.setStyleSheet(
            "QFrame#diagnosticTile { background:#F8FAFC; border:1px solid #EAECF0; border-radius:11px; }"
        )
        layout = QVBoxLayout(tile)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(4)
        caption = QLabel(title.upper())
        caption.setStyleSheet(
            "color:#667085; font-size:10px; font-weight:750; letter-spacing:0.45px; border:none;"
        )
        value = QLabel("—")
        value.setStyleSheet("color:#101828; font-size:14px; font-weight:750; border:none;")
        value.setWordWrap(True)
        layout.addWidget(caption)
        layout.addWidget(value)
        return tile, value

    def _update_performance_mode_hint(self):
        if not hasattr(self, "performance_mode_combo"):
            return
        mode = str(self.performance_mode_combo.currentData() or "auto")
        descriptions = {
            "economy": "Строгий бюджет памяти: 1 OCR-поток, цель до ~1 ГБ RAM, выгрузка OCR при давлении памяти и последовательные тяжёлые задачи.",
            "auto": "Рекомендуется: на ПК примерно с 4 ГБ RAM автоматически включается политика экономного режима; на более мощных системах лимиты мягче.",
            "performance": "Больше OCR-потоков и быстрый кэш памяти. Используйте только когда системе хватает RAM.",
        }
        suffix = " Применяется сразу после сохранения настроек."
        self.performance_mode_hint.setText(descriptions.get(mode, descriptions["auto"]) + suffix)

    def _choose_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Выберите папку документов", self.documents_path.text().strip()
        )
        if folder:
            self.documents_path.setText(folder)
            self.documents_path.setToolTip(folder)

    def _choose_update_folder(self):
        start = self.update_source_path.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(
            self, "Выберите папку обновлений Google Drive", start
        )
        if folder:
            self.update_source_path.setText(folder)
            self.update_source_path.setToolTip(folder)

    def set_values(
        self,
        documents_dir: str | Path,
        confirm_delete: bool,
        performance_mode: str | None = None,
        update_source_dir: str | Path | None = None,
    ):
        path = str(Path(documents_dir).resolve())
        self.documents_path.setText(path)
        self.documents_path.setToolTip(path)
        self.confirm_delete.setChecked(confirm_delete)
        if update_source_dir is not None:
            # Retained only for API compatibility with old settings pages.
            self.update_source_path.setText("")
        if performance_mode is not None:
            self.performance_mode = normalize_performance_mode(performance_mode)
            index = self.performance_mode_combo.findData(self.performance_mode)
            if index >= 0:
                self.performance_mode_combo.setCurrentIndex(index)
        self._update_performance_mode_hint()
        self.refresh_storage_info()
        self.refresh_performance_info()

    def refresh_storage_info(self):
        if self.request_service is None:
            self.trash_info.setText("Корзина восстановления: данные недоступны")
        else:
            stats = self.request_service.get_trash_stats()
            self.trash_info.setText(
                f"Корзина восстановления: {stats.file_count} файлов · {self._format_size(stats.size_bytes)}"
            )
        self._refresh_auto_backup_info()

    def refresh_performance_info(self):
        if not self._diagnostic_labels:
            return
        if self.performance_diagnostics is None:
            self._diagnostic_labels["profile"].setText(
                self.performance_summary or "Автоматический"
            )
            return
        try:
            snapshot = self.performance_diagnostics.snapshot()
        except Exception:
            self._diagnostic_labels["profile"].setText("Данные недоступны")
            return

        if snapshot.total_ram_bytes:
            total = format_memory(snapshot.total_ram_bytes)
            if snapshot.available_ram_bytes:
                available = format_memory(snapshot.available_ram_bytes)
                system_ram = f"{available} доступно · {total} всего"
            else:
                system_ram = f"{total} всего"
        else:
            system_ram = "Не определено"

        self._diagnostic_labels["profile"].setText(
            f"{snapshot.performance_mode} · {snapshot.profile_name}"
        )
        self._diagnostic_labels["cpu"].setText(f"{snapshot.logical_cpus} лог. потоков")
        self._diagnostic_labels["system_ram"].setText(system_ram)
        self._diagnostic_labels["app_ram"].setText(format_memory(snapshot.process_ram_bytes))
        self._diagnostic_labels["peak_ram"].setText(format_memory(snapshot.process_peak_ram_bytes))
        self._diagnostic_labels["ram_budget"].setText(
            format_memory(snapshot.memory_budget_bytes) if snapshot.memory_budget_bytes else "Без жёсткого лимита"
        )
        self._diagnostic_labels["memory_pressure"].setText(snapshot.memory_pressure)
        self._diagnostic_labels["app_cpu"].setText(f"{snapshot.process_cpu_percent:.1f} %")
        self._diagnostic_labels["ocr_threads"].setText(str(snapshot.ocr_threads))
        self._diagnostic_labels["ocr_dpi"].setText(
            f"{snapshot.ocr_dpi} / {snapshot.ocr_retry_dpi} DPI"
        )
        model_label = self._diagnostic_labels["ocr_model"]
        model_label.setText("● Загружена" if snapshot.ocr_model_loaded else "○ Не загружена")
        model_label.setStyleSheet(
            (
                "color:#15803D; font-size:14px; font-weight:750; border:none;"
                if snapshot.ocr_model_loaded
                else "color:#667085; font-size:14px; font-weight:750; border:none;"
            )
        )
        self._diagnostic_labels["memory_mode"].setText(snapshot.memory_mode)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_performance_info()
        if not self._diagnostics_timer.isActive():
            self._diagnostics_timer.start()

    def hideEvent(self, event):
        self._diagnostics_timer.stop()
        super().hideEvent(event)

    def set_diagnostics_message(self, message: str, ok: bool | None = None):
        if not hasattr(self, "diagnostics_result_label"):
            return
        self.diagnostics_result_label.setText(message)
        if ok is True:
            style = "background:#F0FDF4; color:#166534; border:1px solid #BBF7D0;"
        elif ok is False:
            style = "background:#FFF7ED; color:#9A3412; border:1px solid #FED7AA;"
        else:
            style = "background:#F8FAFC; color:#475467; border:1px solid #EAECF0;"
        self.diagnostics_result_label.setStyleSheet(
            style + " border-radius:9px; padding:9px 11px; font-size:11px;"
        )

    def _refresh_auto_backup_info(self):
        if not hasattr(self, "auto_backup_info"):
            return
        if self.auto_backup_path is None:
            self.auto_backup_info.setText("Автобэкап: путь не настроен")
            return
        if not self.auto_backup_path.is_file():
            self.auto_backup_info.setText(
                "Автобэкап: будет создан автоматически при ближайшем запуске приложения."
            )
            return
        try:
            stat = self.auto_backup_path.stat()
            stamp = datetime.fromtimestamp(stat.st_mtime).strftime("%d.%m.%Y %H:%M")
            size = self._format_size(stat.st_size)
            self.auto_backup_info.setText(
                f"Автобэкап: {stamp} · {size} · следующая замена через 7 дней после этой даты"
            )
            self.auto_backup_info.setToolTip(str(self.auto_backup_path))
        except OSError:
            self.auto_backup_info.setText("Автобэкап: не удалось прочитать состояние файла")

    @staticmethod
    def _format_size(size: int) -> str:
        if size < 1024:
            return f"{size} Б"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} КБ"
        if size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} МБ"
        return f"{size / (1024 * 1024 * 1024):.2f} ГБ"
