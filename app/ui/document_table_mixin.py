from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHeaderView, QTableWidgetItem


class DocumentTableMixin:
    def _refresh_documents_search(self, text):
        if not hasattr(self, "documents_table") or self.documents_table is None:
            return

        search_text = text.strip().lower()
        self.document_search_text = text
        for row in range(self.documents_table.rowCount()):
            item = self.documents_table.item(row, 0)
            if item:
                self.documents_table.setRowHidden(
                    row, search_text not in item.text().lower()
                )

    def _document_files(self):
        document_service = getattr(self, "document_service", None)
        if document_service is not None:
            return document_service.list_documents()

        return sorted(
            [file for file in self.documents_dir.iterdir() if file.is_file()],
            key=lambda file: file.name.lower(),
        )

    def display_documents(self, files):
        self.documents_table.setUpdatesEnabled(False)
        try:
            self.documents_table.setRowCount(0)
            vertical_header = self.documents_table.verticalHeader()
            vertical_header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
            vertical_header.setDefaultSectionSize(58)
            self.documents_table.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded
            )
            self.documents_table.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded
            )
            self.documents_table.setRowCount(len(files))

            for row, file in enumerate(files):
                try:
                    file_stat = file.stat()
                except OSError:
                    file_stat = None

                name_item = QTableWidgetItem(f"📄  {file.name}")
                name_item.setToolTip(str(file))
                self.documents_table.setItem(row, 0, name_item)

                if file_stat:
                    size = file_stat.st_size
                    size_text = (
                        f"{size / 1024:.1f} КБ"
                        if size < 1024 * 1024
                        else f"{size / (1024 * 1024):.1f} МБ"
                    )
                else:
                    size_text = "—"
                size_item = QTableWidgetItem(size_text)
                size_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.documents_table.setItem(row, 1, size_item)

                date_text = (
                    datetime.fromtimestamp(file_stat.st_mtime).strftime(
                        "%d.%m.%Y %H:%M"
                    )
                    if file_stat
                    else "—"
                )
                date_item = QTableWidgetItem(date_text)
                date_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.documents_table.setItem(row, 2, date_item)

                path_item = QTableWidgetItem(str(file.parent))
                path_item.setToolTip(str(file))
                path_item.setForeground(QColor("#667085"))
                self.documents_table.setItem(row, 3, path_item)

                actions_item = QTableWidgetItem()
                actions_item.setData(Qt.ItemDataRole.UserRole, str(file))
                self.documents_table.setItem(row, 4, actions_item)
        finally:
            self.documents_table.setUpdatesEnabled(True)

    def filter_documents(self):
        if not hasattr(self, "documents_table") or self.documents_table is None:
            return

        search_text = self.documents_search.text().strip().lower()
        self.documents_table.setUpdatesEnabled(False)
        try:
            for row in range(self.documents_table.rowCount()):
                item = self.documents_table.item(row, 0)
                if item is not None:
                    visible = not search_text or search_text in item.text().lower()
                    self.documents_table.setRowHidden(row, not visible)
        finally:
            self.documents_table.setUpdatesEnabled(True)

    def open_document_from_table(self, row, column):
        item = self.documents_table.item(row, 0)
        if item is None:
            return

        full_path = item.toolTip()
        if full_path:
            self.open_document(Path(full_path))
