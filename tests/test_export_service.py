import tempfile
import unittest
from pathlib import Path

from app.export_service import ExportService
from app.models import Request


class ExportServiceTests(unittest.TestCase):
    def test_export_xlsx_contains_deadline_and_display_status(self):
        try:
            from openpyxl import load_workbook
        except ImportError:
            self.skipTest("openpyxl not installed")

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "registry.xlsx"
            request = Request(
                id=7,
                received_date="01.09.2026",
                due_date="02.09.2000",
                from_department="Отдел",
                from_person="Иванов",
                to_person="Петров",
                request_subject="Тест",
                status="В работе",
            )
            result = ExportService().export_xlsx([request], path)
            self.assertEqual(result.row_count, 1)
            workbook = load_workbook(path, read_only=True)
            try:
                sheet = workbook["Реестр"]
                self.assertEqual(sheet["C2"].value, "02.09.2000")
                self.assertEqual(sheet["K2"].value, "Просрочен")
            finally:
                workbook.close()


if __name__ == "__main__":
    unittest.main()
