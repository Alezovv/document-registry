from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional


_DATE_FORMAT = "%d.%m.%Y"
TERMINAL_STATUSES: frozenset[str] = frozenset({"Исполнен", "Отклонен", "Отклонён"})


@dataclass
class Request:
    id: Optional[int] = None
    received_date: str = ""
    due_date: str = ""
    completed_date: str = ""
    from_department: str = ""
    from_position: str = ""
    from_person: str = ""
    to_person: str = ""
    request_subject: str = ""
    note: str = ""
    status: str = "Новый"
    source_document: str = ""
    is_pinned: bool = False
    source_sha256: str = ""
    source_size_bytes: int = 0

    @staticmethod
    def _parse_date(value: str) -> date | None:
        try:
            return datetime.strptime((value or "").strip(), _DATE_FORMAT).date()
        except (TypeError, ValueError):
            return None

    def is_overdue(self, reference_date: date | None = None) -> bool:
        if self.status in TERMINAL_STATUSES:
            return False
        due = self._parse_date(self.due_date)
        if due is None:
            return False
        return due < (reference_date or date.today())

    def is_due_today(self, reference_date: date | None = None) -> bool:
        if self.status in TERMINAL_STATUSES:
            return False
        due = self._parse_date(self.due_date)
        return bool(due and due == (reference_date or date.today()))

    def display_status(self, reference_date: date | None = None) -> str:
        return "Просрочен" if self.is_overdue(reference_date) else (self.status or "Новый")


@dataclass
class CaseRecord:
    """User-managed case grouping for related requests."""

    id: Optional[int] = None
    title: str = ""
    note: str = ""
    created_at: str = ""
    updated_at: str = ""
    request_count: int = 0
