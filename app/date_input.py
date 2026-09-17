from __future__ import annotations

from datetime import date
import re


def _expand_year(year: int) -> int:
    if year < 100:
        return 2000 + year if year <= 79 else 1900 + year
    return year


def parse_friendly_date(text: str, *, today: date | None = None) -> date | None:
    """Parse human-friendly Russian-style dates used by the UI.

    Accepted examples: 17.09.2026, 17/9/2026, 17-09-26, 17092026,
    170926, 1709 (current year), and 17.09 (current year).
    Empty input returns None.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    current = today or date.today()

    digits = re.sub(r"\D", "", raw)
    parts = [part for part in re.split(r"[.\-/\s]+", raw) if part]

    day: int
    month: int
    year: int
    try:
        if len(parts) == 3:
            day, month, year = int(parts[0]), int(parts[1]), _expand_year(int(parts[2]))
        elif len(parts) == 2:
            day, month, year = int(parts[0]), int(parts[1]), current.year
        elif raw.isdigit() and len(digits) == 8:
            day, month, year = int(digits[:2]), int(digits[2:4]), int(digits[4:])
        elif raw.isdigit() and len(digits) == 6:
            day, month, year = int(digits[:2]), int(digits[2:4]), _expand_year(int(digits[4:]))
        elif raw.isdigit() and len(digits) == 4:
            day, month, year = int(digits[:2]), int(digits[2:]), current.year
        else:
            return None
        return date(year, month, day)
    except ValueError:
        return None



def format_date_while_typing(text: str) -> str:
    """Normalize interactive date input to the registry's dotted format.

    Digits are progressively grouped as DD.MM.YYYY, so typing 17092026
    becomes 17.09.2026 without requiring the user to type separators. Existing
    slash/dash separators are normalized to dots. The function intentionally
    does not validate the calendar date; validation remains in
    :func:`parse_friendly_date` when editing is committed.
    """
    raw = str(text or "")
    if not raw:
        return ""

    # Leave clearly non-date text alone so the normal commit-time validation
    # can reject it without surprising destructive edits while the user types.
    if re.search(r"[^0-9.\-/\s]", raw):
        return raw

    # If the user chose to type separators manually (or a separator was
    # inserted automatically on the previous keystroke), keep the component
    # boundaries and normalize separators to dots.  When a two-digit month is
    # completed, append the second dot as well: typing 17 -> 17. and then 09
    # becomes 17.09. without another manual keystroke.
    if re.search(r"[.\-/\s]", raw):
        cleaned = re.sub(r"[\-/\s]+", ".", raw)
        cleaned = re.sub(r"\.{2,}", ".", cleaned)
        cleaned = cleaned[:10]
        if re.fullmatch(r"\d{2}\.\d{2}", cleaned):
            cleaned += "."
        return cleaned

    digits = re.sub(r"\D", "", raw)[:8]
    if len(digits) <= 1:
        return digits
    if len(digits) == 2:
        return digits + "."
    if len(digits) <= 3:
        return digits[:2] + "." + digits[2:]
    if len(digits) == 4:
        return digits[:2] + "." + digits[2:4] + "."
    return digits[:2] + "." + digits[2:4] + "." + digits[4:]

def format_date(value: date | None) -> str:
    return "" if value is None else value.strftime("%d.%m.%Y")
