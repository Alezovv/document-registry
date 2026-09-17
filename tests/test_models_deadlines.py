import unittest
from datetime import date

from app.models import Request


class DeadlineModelTests(unittest.TestCase):
    def test_overdue_is_computed_without_overwriting_workflow_status(self):
        request = Request(status="В работе", due_date="15.09.2026")
        self.assertTrue(request.is_overdue(date(2026, 9, 16)))
        self.assertEqual(request.display_status(date(2026, 9, 16)), "Просрочен")
        self.assertEqual(request.status, "В работе")

    def test_completed_request_is_not_overdue(self):
        request = Request(status="Исполнен", due_date="01.09.2026")
        self.assertFalse(request.is_overdue(date(2026, 9, 16)))
        self.assertEqual(request.display_status(date(2026, 9, 16)), "Исполнен")

    def test_due_today(self):
        request = Request(status="Новый", due_date="16.09.2026")
        self.assertTrue(request.is_due_today(date(2026, 9, 16)))
        self.assertFalse(request.is_overdue(date(2026, 9, 16)))


if __name__ == "__main__":
    unittest.main()
