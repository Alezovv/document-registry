from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.db_merge import MergeAnalysis, MergeConflict, MergeStrategy
from app.ui.styles import PRIMARY_COLOR, PRIMARY_HOVER


class ConflictWidget(QFrame):
    """Виджет отображения одного конфликта с возможностью выбора значения для каждого поля."""

    resolution_changed = Signal(tuple, MergeStrategy)

    def __init__(self, conflict: MergeConflict, parent=None):
        super().__init__(parent)
        self.conflict = conflict
        self.selected_strategy = MergeStrategy.KEEP_CURRENT
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setObjectName("conflictCard")
        self.setStyleSheet(
            "QFrame#conflictCard { background:#FFFFFF; border:1px solid #E2E8F0; border-radius:9px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        header = QLabel(f"<b>{self.conflict.display_label}</b>")
        header.setWordWrap(True)
        header.setStyleSheet("color:#1E293B; font-size:12px; border:none;")
        layout.addWidget(header)

        # Выбор решения для записи целиком
        choice_layout = QHBoxLayout()
        choice_layout.setSpacing(12)
        btn_group = QButtonGroup(self)

        self.rb_current = QRadioButton("Оставить текущую запись")
        self.rb_current.setChecked(True)
        self.rb_current.toggled.connect(self._on_toggled)

        self.rb_rollback = QRadioButton("Заменить версией из копии")
        self.rb_rollback.toggled.connect(self._on_toggled)

        btn_group.addButton(self.rb_current, 0)
        btn_group.addButton(self.rb_rollback, 1)

        choice_layout.addWidget(self.rb_current)
        choice_layout.addWidget(self.rb_rollback)
        choice_layout.addStretch()
        layout.addLayout(choice_layout)

        # Список различающихся полей
        diffs_group = QGroupBox("Различия в полях:")
        diffs_group.setStyleSheet("font-size:11px; color:#475467; font-weight:600;")
        diffs_layout = QVBoxLayout(diffs_group)
        diffs_layout.setContentsMargins(10, 10, 10, 10)
        diffs_layout.setSpacing(4)

        for diff in self.conflict.diffs:
            lbl = QLabel(
                f"• <b>{diff.field_name}</b>: текущее = «{diff.current_value}» ↔ "
                f"в копии = «{diff.rollback_value}»"
            )
            lbl.setWordWrap(True)
            lbl.setStyleSheet("color:#334155; font-size:11px; font-weight:normal; border:none;")
            diffs_layout.addWidget(lbl)

        layout.addWidget(diffs_group)

    def _on_toggled(self) -> None:
        if self.rb_rollback.isChecked():
            self.selected_strategy = MergeStrategy.KEEP_ROLLBACK
        else:
            self.selected_strategy = MergeStrategy.KEEP_CURRENT
        self.resolution_changed.emit(self.conflict.business_key, self.selected_strategy)

    def set_strategy(self, strategy: MergeStrategy) -> None:
        if strategy == MergeStrategy.KEEP_ROLLBACK:
            self.rb_rollback.setChecked(True)
        else:
            self.rb_current.setChecked(True)


class MergePreviewDialog(QDialog):
    """
    Диалог предварительного просмотра различий и разрешения конфликтов при слиянии БД.
    
    Режимы:
    - compare_only = True: информационный просмотр различий.
    - compare_only = False: выбор стратегий и запуск слияния.
    """

    def __init__(
        self,
        analysis: MergeAnalysis,
        *,
        compare_only: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.analysis = analysis
        self.compare_only = compare_only
        self.conflict_resolutions: dict[tuple, MergeStrategy] = {}
        self.default_strategy = MergeStrategy.KEEP_CURRENT
        self._conflict_widgets: list[ConflictWidget] = []

        title = "Сравнение баз данных" if compare_only else "Слияние и объединение записей базы данных"
        self.setWindowTitle(title)
        self.setMinimumSize(740, 540)
        self.resize(800, 600)
        self._build_ui()

    def get_merge_params(self) -> tuple[MergeStrategy, dict[tuple, MergeStrategy]]:
        """Вернуть стратегию по умолчанию и индивидуальные решения конфликтов."""
        if self.analysis.has_conflicts:
            return MergeStrategy.INTERACTIVE, self.conflict_resolutions
        return self.default_strategy, self.conflict_resolutions

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        # Заголовок
        title_text = "Сравнение текущей БД с сохранённой копией" if self.compare_only else "Слияние данных без потерь"
        title_lbl = QLabel(title_text)
        title_lbl.setStyleSheet("font-size:18px; font-weight:750; color:#0F172A;")
        root.addWidget(title_lbl)

        # Сводка (без каскадного padding на дочерние QLabel)
        summary_frame = QFrame(self)
        summary_frame.setObjectName("mergeSummaryFrame")
        summary_frame.setStyleSheet(
            "QFrame#mergeSummaryFrame { background:#F8FAFC; border:1px solid #E2E8F0; border-radius:10px; }"
        )
        summary_layout = QVBoxLayout(summary_frame)
        summary_layout.setContentsMargins(14, 12, 14, 12)
        summary_layout.setSpacing(5)

        stats = [
            f"Текущая рабочая база: <b>{self.analysis.current_total}</b> записей",
            f"Сохранённая копия отката: <b>{self.analysis.rollback_total}</b> записей",
            f"Полностью совпадают: <b>{self.analysis.identical_count}</b> записей",
            f"Новые в текущей базе (сохранятся): <b>{len(self.analysis.only_in_current)}</b>",
            f"Только в сохранённой копии (будут импортированы): <b>{len(self.analysis.only_in_rollback)}</b>",
            f"Конфликты данных: <b>{len(self.analysis.conflicts)}</b>",
        ]
        for s in stats:
            lbl = QLabel(s)
            lbl.setWordWrap(True)
            lbl.setStyleSheet("font-size:12px; color:#334155; border:none;")
            summary_layout.addWidget(lbl)
        root.addWidget(summary_frame)

        if not self.compare_only and self.analysis.has_conflicts:
            # Селектор общей стратегии
            strategy_layout = QHBoxLayout()
            strategy_layout.setSpacing(8)
            lbl_strat = QLabel("Стратегия по умолчанию для конфликтов:")
            lbl_strat.setStyleSheet("font-size:12px; font-weight:650; color:#1E293B;")

            self.strategy_combo = QComboBox(self)
            self.strategy_combo.setMinimumHeight(36)
            self.strategy_combo.addItem("Оставить текущую запись (KEEP_CURRENT)", MergeStrategy.KEEP_CURRENT)
            self.strategy_combo.addItem("Взять версию из копии (KEEP_ROLLBACK)", MergeStrategy.KEEP_ROLLBACK)
            self.strategy_combo.addItem("Оставить более свежую запись (KEEP_NEWER)", MergeStrategy.KEEP_NEWER)
            self.strategy_combo.currentIndexChanged.connect(self._on_strategy_changed)

            strategy_layout.addWidget(lbl_strat)
            strategy_layout.addWidget(self.strategy_combo, 1)
            root.addLayout(strategy_layout)

        # Список конфликтов в scroll area
        if self.analysis.has_conflicts:
            conflicts_label = QLabel(f"Обнаружено конфликтов: {len(self.analysis.conflicts)}")
            conflicts_label.setStyleSheet("font-size:13px; font-weight:700; color:#1E293B;")
            root.addWidget(conflicts_label)

            scroll = QScrollArea(self)
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet("QScrollArea { border:1px solid #CBD5E1; border-radius:9px; }")

            container = QWidget()
            cont_layout = QVBoxLayout(container)
            cont_layout.setContentsMargins(10, 10, 10, 10)
            cont_layout.setSpacing(8)

            for conflict in self.analysis.conflicts:
                cw = ConflictWidget(conflict, container)
                cw.resolution_changed.connect(self._on_conflict_resolved)
                self._conflict_widgets.append(cw)
                self.conflict_resolutions[conflict.business_key] = cw.selected_strategy
                cont_layout.addWidget(cw)

            cont_layout.addStretch()
            scroll.setWidget(container)
            root.addWidget(scroll, 1)
        else:
            no_conflicts = QLabel("✅ Конфликтующих записей не обнаружено. Слияние пройдёт полностью автоматически.")
            no_conflicts.setWordWrap(True)
            no_conflicts.setStyleSheet(
                "background:#ECFDF5; color:#065F46; border:1px solid #A7F3D0; "
                "border-radius:8px; padding:10px 14px; font-size:12px; font-weight:600;"
            )
            root.addWidget(no_conflicts)
            root.addStretch()

        # Кнопки диалога
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch()

        if self.compare_only:
            self.close_button = QPushButton("Закрыть", self)
            self.close_button.setMinimumHeight(38)
            self.close_button.setMinimumWidth(120)
            self.close_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            self.close_button.setStyleSheet(
                "QPushButton { background:#FFFFFF; color:#344054; border:1px solid #D0D5DD; "
                "border-radius:9px; padding:8px 20px; font-weight:650; } "
                "QPushButton:hover { background:#F8FAFC; }"
            )
            self.close_button.clicked.connect(self.accept)
            buttons.addWidget(self.close_button)
        else:
            self.cancel_button = QPushButton("Отмена", self)
            self.cancel_button.setMinimumHeight(38)
            self.cancel_button.setMinimumWidth(115)
            self.cancel_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            self.cancel_button.setStyleSheet(
                "QPushButton { background:#FFFFFF; color:#344054; border:1px solid #D0D5DD; "
                "border-radius:9px; padding:8px 18px; font-weight:650; } "
                "QPushButton:hover { background:#F8FAFC; }"
            )
            self.cancel_button.clicked.connect(self.reject)

            self.merge_button = QPushButton("🔀 Объединить данные", self)
            self.merge_button.setMinimumHeight(38)
            self.merge_button.setMinimumWidth(200)
            self.merge_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            self.merge_button.setStyleSheet(
                f"QPushButton {{ background:{PRIMARY_COLOR}; color:white; border:none; "
                "border-radius:9px; padding:8px 22px; font-weight:700; } "
                f"QPushButton:hover {{ background:{PRIMARY_HOVER}; }}"
            )
            self.merge_button.clicked.connect(self.accept)

            buttons.addWidget(self.cancel_button)
            buttons.addWidget(self.merge_button)

        root.addLayout(buttons)

    def _on_strategy_changed(self) -> None:
        self.default_strategy = self.strategy_combo.currentData()
        if self.default_strategy in (MergeStrategy.KEEP_CURRENT, MergeStrategy.KEEP_ROLLBACK):
            for cw in self._conflict_widgets:
                cw.set_strategy(self.default_strategy)

    def _on_conflict_resolved(self, key: tuple, strategy: MergeStrategy) -> None:
        self.conflict_resolutions[key] = strategy
