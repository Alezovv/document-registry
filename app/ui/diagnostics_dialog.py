from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QVBoxLayout,
    QWidget,
)


class DiagnosticsDialog(QDialog):
    check_requested = Signal()
    package_requested = Signal()
    open_logs_requested = Signal()
    open_data_requested = Signal()
    copy_requested = Signal()
    install_ui_test_requested = Signal()
    rebuild_search_requested = Signal()
    duplicate_scan_requested = Signal()

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Центр диагностики DocumentRegistry")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.resize(920, 650)
        self.setMinimumSize(820, 560)
        self._value_labels: dict[str, QLabel] = {}
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        title = QLabel("Центр диагностики")
        title.setStyleSheet("font-size:24px; font-weight:750; color:#0F172A;")
        subtitle = QLabel("Версии, состояние базы, OCR, поиск, backup и история установок")
        subtitle.setStyleSheet("font-size:12px; color:#64748B;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.state_badge = QLabel("Проверка...")
        self.state_badge.setStyleSheet(
            "background:#F8FAFC; color:#475569; border:1px solid #E2E8F0; "
            "border-radius:9px; padding:7px 11px; font-weight:700;"
        )
        header.addWidget(self.state_badge)
        root.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_system_tab(), "Система")
        self.tabs.addTab(self._build_updates_tab(), "История обновлений")
        root.addWidget(self.tabs, 1)

        actions = QGridLayout()
        actions.setHorizontalSpacing(9)
        actions.setVerticalSpacing(8)
        action_specs = (
            ("Проверить состояние", self.check_requested),
            ("Создать диагностический пакет", self.package_requested),
            ("Открыть данные", self.open_data_requested),
            ("Открыть логи", self.open_logs_requested),
            ("Проверить окно установки", self.install_ui_test_requested),
            ("Перестроить поисковый индекс", self.rebuild_search_requested),
            ("Проверить дубликаты", self.duplicate_scan_requested),
            ("Скопировать сведения", self.copy_requested),
        )
        for index, (text, signal) in enumerate(action_specs):
            button = QPushButton(text)
            button.setMinimumHeight(38)
            button.setStyleSheet(
                "QPushButton { background:#FFFFFF; color:#344054; border:1px solid #D0D5DD; "
                "border-radius:9px; padding:7px 13px; font-weight:650; } "
                "QPushButton:hover { background:#F8FAFC; border-color:#98A2B3; }"
            )
            button.clicked.connect(signal.emit)
            actions.addWidget(button, index // 3, index % 3)
        for column in range(3):
            actions.setColumnStretch(column, 1)
        root.addLayout(actions)

    def _build_system_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 14, 10, 10)
        layout.setSpacing(12)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        metrics = (
            ("app", "Версия приложения"),
            ("parser", "Версия parser"),
            ("schema", "Схема БД"),
            ("db", "Состояние БД"),
            ("records", "Записи / документы"),
            ("search", "Полнотекстовый индекс"),
            ("ocr", "OCR-модели"),
            ("mode", "Режим производительности"),
            ("ram", "RAM приложения / доступно"),
            ("update", "Последняя установка"),
            ("backup", "Последний backup"),
            ("db_size", "Размер БД"),
            ("runtime", "Рабочая папка"),
        )
        for index, (key, title) in enumerate(metrics):
            card, label = self._metric_card(title)
            grid.addWidget(card, index // 3, index % 3)
            self._value_labels[key] = label
        for column in range(3):
            grid.setColumnStretch(column, 1)
        layout.addLayout(grid)
        layout.addStretch()
        return page

    def _build_updates_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 14, 10, 10)
        layout.setSpacing(9)
        hint = QLabel(
            "Здесь сохраняются реальные этапы standalone-updater: проверка пакета, backup, "
            "миграция БД, установка, проверка, откат и запуск. История не содержит документов."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#64748B; font-size:12px;")
        layout.addWidget(hint)

        self.history_table = QTableWidget()
        self.history_table.setColumnCount(6)
        self.history_table.setHorizontalHeaderLabels(
            ["Время", "Версия", "Состояние", "Этап", "%", "Сообщение"]
        )
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setAlternatingRowColors(True)
        header = self.history_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.history_table, 1)
        return page

    @staticmethod
    def _metric_card(title: str) -> tuple[QFrame, QLabel]:
        card = QFrame()
        card.setObjectName("diagnosticMetric")
        card.setMinimumHeight(90)
        card.setStyleSheet(
            "QFrame#diagnosticMetric { background:#FFFFFF; border:1px solid #E2E8F0; "
            "border-radius:11px; }"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(13, 10, 13, 10)
        layout.setSpacing(5)
        caption = QLabel(title.upper())
        caption.setStyleSheet("color:#64748B; font-size:10px; font-weight:750;")
        value = QLabel("—")
        value.setWordWrap(True)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        value.setStyleSheet("color:#0F172A; font-size:13px; font-weight:700;")
        layout.addWidget(caption)
        layout.addWidget(value, 1)
        return card, value

    @staticmethod
    def _format_size(size: int) -> str:
        value = float(max(0, int(size or 0)))
        for unit in ("Б", "КБ", "МБ", "ГБ"):
            if value < 1024.0 or unit == "ГБ":
                return f"{value:.1f} {unit}" if unit != "Б" else f"{int(value)} Б"
            value /= 1024.0
        return f"{value:.1f} ГБ"

    @staticmethod
    def _state_label(value: Any) -> str:
        key = str(value or "").strip().lower()
        return {
            "installing": "Установка",
            "success": "Успешно",
            "failed": "Ошибка",
            "failure": "Ошибка",
            "rollback": "Откат",
        }.get(key, str(value or ""))

    @staticmethod
    def _stage_label(value: Any) -> str:
        key = str(value or "").strip().lower()
        return {
            "starting": "Запуск",
            "waiting": "Ожидание приложения",
            "wait_for_exit": "Ожидание приложения",
            "verify": "Проверка пакета",
            "verify_package": "Проверка пакета",
            "extract": "Распаковка",
            "preflight": "Предварительная проверка",
            "backup": "Резервная копия",
            "database": "Подготовка БД",
            "migrate_database": "Миграция БД",
            "install": "Установка файлов",
            "final_check": "Финальная проверка",
            "verify_install": "Финальная проверка",
            "rollback": "Откат",
            "launch": "Запуск приложения",
            "done": "Готово",
            "complete": "Готово",
        }.get(key, str(value or ""))

    @staticmethod
    def _compact_timestamp(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return "—"
        try:
            parsed = datetime.fromisoformat(text)
            return parsed.astimezone().strftime("%d.%m.%Y %H:%M:%S")
        except (ValueError, OSError):
            return text.replace("T", " ")[:19]

    def refresh(self):
        try:
            data = self.service.overview()
        except Exception as error:
            self.state_badge.setText(f"Ошибка: {error}")
            self.state_badge.setStyleSheet(
                "background:#FFF1F0; color:#B42318; border:1px solid #FECDCA; "
                "border-radius:9px; padding:7px 11px; font-weight:700;"
            )
            return

        db_ok = str(data.get("database_check", "")).lower() == "ok"
        models_ok = bool(data.get("ocr_models_present"))
        self.state_badge.setText("● Система в норме" if db_ok and models_ok else "● Требует внимания")
        self.state_badge.setStyleSheet(
            (
                "background:#F0FDF4; color:#166534; border:1px solid #BBF7D0; "
                if db_ok and models_ok
                else "background:#FFF7ED; color:#9A3412; border:1px solid #FED7AA; "
            )
            + "border-radius:9px; padding:7px 11px; font-weight:700;"
        )

        search = data.get("search_index") or {}
        update = data.get("last_update") or {}
        backup = data.get("latest_backup") or {}
        paths = data.get("paths") or {}
        self._value_labels["app"].setText(str(data.get("application_version", "—")))
        self._value_labels["parser"].setText(str(data.get("parser_version", "—")))
        self._value_labels["schema"].setText(str(data.get("database_schema", "—")))
        self._value_labels["db"].setText(str(data.get("database_check", "—")))
        self._value_labels["records"].setText(
            f"{data.get('request_count', 0)} / {data.get('document_count', 0)}"
        )
        self._value_labels["search"].setText(
            f"{search.get('indexed', 0)} из {search.get('requests', 0)} · OCR: {search.get('with_ocr', 0)}"
        )
        self._value_labels["ocr"].setText(
            "Файлы моделей найдены" if data.get("ocr_models_present") else "Не хватает файлов модели"
        )
        self._value_labels["mode"].setText(str(data.get("performance_mode", "—")))
        ram_text = f"Сейчас: {data.get('process_ram', '—')} · пик: {data.get('process_peak_ram', '—')}"
        if data.get("memory_budget") not in {None, "", "n/a", "без жёсткого лимита"}:
            ram_text += f"\nЛимит: {data.get('memory_budget')} · {data.get('memory_pressure', '—')}"
        else:
            ram_text += f"\nСвободно системе: {data.get('available_ram', '—')}"
        self._value_labels["ram"].setText(ram_text)
        update_text = "Нет данных"
        if update:
            update_text = (
                f"{update.get('version', '—')} · {self._state_label(update.get('state', '—'))}\n"
                f"{self._compact_timestamp(update.get('updated_at'))}"
            )
        self._value_labels["update"].setText(update_text)
        backup_text = "Backup ещё не создан"
        if backup:
            backup_text = f"{backup.get('name', '—')}\n{self._compact_timestamp(backup.get('modified_at'))}"
            if backup.get("verified"):
                backup_text += (
                    f"\n✓ Проверен: {backup.get('verified_name', '—')} · "
                    f"{self._compact_timestamp(backup.get('verified_at'))}"
                )
            else:
                backup_text += "\nНе было глубокой проверки"
        self._value_labels["backup"].setText(backup_text)
        self._value_labels["db_size"].setText(self._format_size(data.get("database_size_bytes", 0)))
        self._value_labels["runtime"].setText(str(paths.get("runtime", "—")))
        self._fill_history()

    def _fill_history(self):
        try:
            rows = self.service.update_history(limit=300)
        except Exception:
            rows = []
        self.history_table.setRowCount(len(rows))
        for row_index, entry in enumerate(rows):
            values = (
                self._compact_timestamp(entry.get("timestamp") or entry.get("updated_at")),
                entry.get("version", ""),
                self._state_label(entry.get("state", "")),
                self._stage_label(entry.get("stage", "")),
                entry.get("progress", ""),
                entry.get("message", ""),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 4:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.history_table.setItem(row_index, column, item)
