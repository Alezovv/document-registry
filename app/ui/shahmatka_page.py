import os
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
    QLineEdit, QPushButton, QFileDialog, QMessageBox,
    QFormLayout
)
from PySide6.QtCore import QThread, Qt, Signal
from app.shahmatka_service import generate_shahmatka_xlsx


class ShahmatkaWorker(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, start_num: int, max_num: int, rows: int, file_path: str, parent=None):
        super().__init__(parent)
        self.start_num = int(start_num)
        self.max_num = int(max_num)
        self.rows = max(1, int(rows))
        self.file_path = str(file_path)

    def run(self):
        try:
            self.setPriority(QThread.Priority.LowPriority)
        except Exception:
            pass
        try:
            generate_shahmatka_xlsx(
                self.start_num, self.max_num, self.rows, self.file_path
            )
        except Exception as error:
            self.failed.emit(str(error))
        else:
            self.succeeded.emit(self.file_path)


class NumberInput(QWidget):
    def __init__(self, value, min_val, max_val, parent=None):
        super().__init__(parent)
        self.min_val = min_val
        self.max_val = max_val
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        
        self.btn_minus = QPushButton("-")
        self.btn_minus.setFixedSize(36, 38)
        self.btn_minus.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_minus.setStyleSheet(self._btn_style())
        self.btn_minus.clicked.connect(self.decrement)
        
        self.input_field = QLineEdit(str(value))
        self.input_field.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.input_field.setFixedHeight(38)
        self.input_field.setStyleSheet('''
            QLineEdit {
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 8px;
                font-size: 14px;
                color: #0F172A;
            }
            QLineEdit:hover { border-color: #94A3B8; }
            QLineEdit:focus { border: 1px solid #2563EB; background-color: #FFFFFF; }
        ''')
        self.input_field.editingFinished.connect(self._validate)
        
        self.btn_plus = QPushButton("+")
        self.btn_plus.setFixedSize(36, 38)
        self.btn_plus.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_plus.setStyleSheet(self._btn_style())
        self.btn_plus.clicked.connect(self.increment)
        
        layout.addWidget(self.btn_minus)
        layout.addWidget(self.input_field, 1)
        layout.addWidget(self.btn_plus)

    def _btn_style(self):
        return '''
            QPushButton {
                background-color: #F8FAFC;
                border: 1px solid #CBD5E1;
                border-radius: 8px;
                color: #475569;
                font-size: 18px;
                font-weight: bold;
                padding-bottom: 2px;
            }
            QPushButton:hover { background-color: #E2E8F0; color: #0F172A; }
            QPushButton:pressed { background-color: #CBD5E1; }
        '''
        
    def _validate(self):
        try:
            val = int(self.input_field.text().strip())
            val = max(self.min_val, min(self.max_val, val))
            self.input_field.setText(str(val))
        except ValueError:
            self.input_field.setText(str(self.min_val))
            
    def value(self):
        self._validate()
        return int(self.input_field.text())
        
    def increment(self):
        self._validate()
        val = int(self.input_field.text())
        if val < self.max_val:
            self.input_field.setText(str(val + 1))
            
    def decrement(self):
        self._validate()
        val = int(self.input_field.text())
        if val > self.min_val:
            self.input_field.setText(str(val - 1))


class ShahmatkaPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._generation_worker: ShahmatkaWorker | None = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(20)

        # Header
        header = QLabel("Создание шахматки")
        header.setStyleSheet("font-size: 24px; font-weight: bold; color: #0F172A;")
        layout.addWidget(header)

        description = QLabel("Сгенерировать Excel файл (А4, альбомная ориентация) для распечатки.")
        description.setStyleSheet("color: #64748B; font-size: 14px;")
        layout.addWidget(description)

        # Form
        form_layout = QFormLayout()
        form_layout.setSpacing(15)

        self.start_num_input = NumberInput(1, 1, 100000)
        self.max_num_input = NumberInput(10000, 1, 100000)
        self.step_input = NumberInput(28, 1, 100)
        self.step_input.setToolTip("Количество строк в одном блоке")

        form_layout.addRow(QLabel("Начальное число:", styleSheet="color: #344054; font-size: 14px; font-weight: 600;"), self.start_num_input)
        form_layout.addRow(QLabel("Конечное число:", styleSheet="color: #344054; font-size: 14px; font-weight: 600;"), self.max_num_input)
        form_layout.addRow(QLabel("Строк в столбце (шаг):", styleSheet="color: #344054; font-size: 14px; font-weight: 600;"), self.step_input)

        layout.addLayout(form_layout)

        # Generate Button
        self.generate_btn = QPushButton("Сгенерировать Excel")
        self.generate_btn.setMinimumHeight(42)
        self.generate_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.generate_btn.setStyleSheet('''
            QPushButton {
                background-color: #2563EB;
                color: white;
                border-radius: 9px;
                padding: 10px 26px;
                font-size: 14px;
                font-weight: 700;
            }
            QPushButton:hover { background-color: #1D4ED8; }
            QPushButton:pressed { background-color: #1E40AF; }
        ''')
        self.generate_btn.clicked.connect(self.generate_excel)
        
        btn_layout = QHBoxLayout()
        btn_layout.addWidget(self.generate_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addStretch()

    # Позволяет сбросить фокус при клике в пустое место
    def mousePressEvent(self, event):
        self.setFocus()
        super().mousePressEvent(event)

    def generate_excel(self):
        start_num = self.start_num_input.value()
        max_num = self.max_num_input.value()
        rows = self.step_input.value()

        if start_num > max_num:
            QMessageBox.warning(self, "Ошибка", "Начальное число не может быть больше конечного.")
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить файл", "shahmatka.xlsx", "Excel Files (*.xlsx)"
        )

        if not file_path:
            return

        if self._generation_worker is not None and self._generation_worker.isRunning():
            return

        self.generate_btn.setEnabled(False)
        self.generate_btn.setText("Формирование…")
        worker = ShahmatkaWorker(start_num, max_num, rows, file_path, parent=self)
        self._generation_worker = worker
        worker.succeeded.connect(self._on_generation_succeeded)
        worker.failed.connect(self._on_generation_failed)
        worker.finished.connect(self._on_generation_finished)
        worker.start(QThread.Priority.LowPriority)

    def _on_generation_succeeded(self, file_path: str):
        QMessageBox.information(self, "Успех", f"Файл успешно сохранен:\n{file_path}")
        try:
            if os.name == "nt":
                os.startfile(os.path.dirname(file_path))
        except Exception:
            pass

    def _on_generation_failed(self, message: str):
        QMessageBox.critical(self, "Ошибка", f"Не удалось создать файл:\n{message}")

    def _on_generation_finished(self):
        worker = self._generation_worker
        self._generation_worker = None
        self.generate_btn.setEnabled(True)
        self.generate_btn.setText("Сгенерировать Excel")
        if worker is not None:
            worker.deleteLater()
