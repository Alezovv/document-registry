from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from app.models import Request
from app.document_parser_parts.constants import (
    FIELD_LABEL_ALIASES,
    LATIN_LOOKALIKE_MAP,
    RUSSIAN_MONTHS,
    SUBJECT_MAX_CHARS,
    SUBJECT_STOP_MARKERS,
    TARGET_STOP_WORDS,
)


class NormalizationMixin:
    """Labels, date/FIO normalization and final value cleanup."""

    @classmethod
    def _label_key(cls, value: str) -> str:
        value = unicodedata.normalize("NFKC", value or "").lower().replace("ё", "е")
        # OCR often recognizes the letter О in short uppercase labels as zero.
        value = value.replace("0", "о")
        value = re.sub(r"[^а-яa-z0-9]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    @classmethod
    def _identify_label(cls, raw: str) -> tuple[str, str] | None:
        """Return (field, remainder) for an explicit/fuzzy form label."""
        raw = (raw or "").strip()
        if not raw:
            return None

        # First split around the separators commonly used by forms.  For a bare
        # label the whole line remains the candidate label.
        separator = re.search(r"\s*[:：]\s*|\s+[—–-]\s+", raw)
        if separator:
            label_part = raw[:separator.start()].strip()
            remainder = raw[separator.end():].strip()
        else:
            label_part = raw
            remainder = ""

        key = cls._label_key(label_part)
        if not key or len(key) > 42:
            return None

        best: tuple[float, str] | None = None
        for field, aliases in FIELD_LABEL_ALIASES.items():
            for alias in aliases:
                alias_key = cls._label_key(alias)
                # Exact label or "label value" without a colon.
                if key == alias_key:
                    return field, remainder
                if key.startswith(alias_key + " ") and alias_key != "дата":
                    # A long letterhead such as "ОТДЕЛ МВД РОССИИ ПО ..." is an
                    # organisation name, not a form label named "Отдел".
                    if alias_key in {"отдел", "организация", "учреждение", "подразделение"} and len(key.split()) > 5:
                        continue
                    inferred = label_part[len(alias):].strip(" .,:;—–-")
                    return field, remainder or inferred

                # Fuzzy matching is intentionally restricted to short label-like
                # lines so normal prose never becomes a field label by accident.
                if not remainder and len(key.split()) <= 4:
                    score = SequenceMatcher(None, key, alias_key).ratio()
                    if score >= 0.82 and (best is None or score > best[0]):
                        best = (score, field)
        if best:
            return best[1], remainder
        return None

    @classmethod
    def _extract_labeled_fields(cls, text: str) -> dict[str, str]:
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        result: dict[str, str] = {}
        index = 0
        while index < len(lines):
            identified = cls._identify_label(lines[index])
            if not identified:
                index += 1
                continue

            field, inline_value = identified
            if field in result:
                index += 1
                continue

            if field == "request_subject":
                parts: list[str] = []
                if inline_value:
                    parts.append(inline_value)
                lookahead = index + 1
                while lookahead < len(lines) and len(" ".join(parts)) < 420:
                    line = lines[lookahead]
                    if cls._identify_label(line):
                        break
                    normalized = cls._label_key(line)
                    if any(normalized.startswith(cls._label_key(marker)) for marker in SUBJECT_STOP_MARKERS):
                        break
                    # Registration/footer garbage should not become part of the subject.
                    if re.match(r"(?i)^(?:исх\.?|вх\.?|№|тел\.?|исполнитель\b)", line):
                        break
                    parts.append(line)
                    lookahead += 1
                    if len(parts) >= 5:
                        break
                value = cls._clean_labeled_value(field, " ".join(parts))
                if value:
                    result[field] = value
                index = max(index + 1, lookahead)
                continue

            value = inline_value
            if not value and index + 1 < len(lines):
                next_line = lines[index + 1]
                if not cls._identify_label(next_line):
                    value = next_line
                    index += 1
            value = cls._clean_labeled_value(field, value)
            if value:
                result[field] = value
            index += 1
        return result

    @classmethod
    def _extract_spatial_labeled_fields(cls, blocks: list[dict[str, Any]]) -> dict[str, str]:
        """Associate a label block with the nearest block to its right/below."""
        if not blocks:
            return {}
        prepared = [b for b in blocks if str(b.get("text", "")).strip()]
        result: dict[str, str] = {}
        for label_block in prepared:
            identified = cls._identify_label(str(label_block.get("text", "")))
            if not identified:
                continue
            field, inline = identified
            if field in result:
                continue
            if inline:
                value = cls._clean_labeled_value(field, inline)
                if value:
                    result[field] = value
                continue

            try:
                lx1 = float(label_block.get("x1", 0))
                lx2 = float(label_block.get("x2", lx1))
                ly1 = float(label_block.get("y1", 0))
                ly2 = float(label_block.get("y2", ly1))
                lcy = float(label_block.get("center_y", (ly1 + ly2) / 2.0))
                lh = max(1.0, float(label_block.get("height", ly2 - ly1 or 1.0)))
            except (TypeError, ValueError):
                continue

            right_candidates = []
            below_candidates = []
            for candidate in prepared:
                if candidate is label_block:
                    continue
                try:
                    cx1 = float(candidate.get("x1", 0))
                    cy1 = float(candidate.get("y1", 0))
                    ccy = float(candidate.get("center_y", cy1))
                except (TypeError, ValueError):
                    continue
                ctext = str(candidate.get("text", "")).strip()
                if not ctext or cls._identify_label(ctext):
                    continue
                if cx1 >= lx2 - 2 and abs(ccy - lcy) <= max(12.0, lh * 1.3):
                    right_candidates.append((cx1 - lx2 + abs(ccy - lcy) * 2, candidate))
                if cy1 >= ly2 - 1 and cy1 - ly2 <= max(35.0, lh * 2.5):
                    horizontal_penalty = abs(cx1 - lx1) * 0.15
                    below_candidates.append((cy1 - ly2 + horizontal_penalty, candidate))

            candidates = sorted(right_candidates, key=lambda item: item[0]) or sorted(
                below_candidates, key=lambda item: item[0]
            )
            if candidates:
                chosen_block = candidates[0][1]
                chosen_text = str(chosen_block.get("text", "")).strip()
                if field == "request_subject":
                    # Form layouts often wrap the subject over several blocks.
                    # Continue downward in the same value column, but stop before
                    # another field/footer marker.
                    try:
                        page = int(label_block.get("page_index", 0) or 0)
                        start_y = float(chosen_block.get("y1", ly2) or ly2)
                        end_y = float(chosen_block.get("y2", start_y) or start_y)
                        start_x = float(chosen_block.get("x1", lx1) or lx1)
                        page_h = float(label_block.get("page_height", 0) or 0)
                    except (TypeError, ValueError):
                        page, start_y, end_y, start_x, page_h = 0, ly2, ly2, lx1, 0.0
                    max_gap = max(110.0, lh * 7.0)
                    if page_h > 0:
                        max_gap = min(max_gap, page_h * 0.14)
                    continuations: list[tuple[float, float, str]] = []
                    for extra in prepared:
                        if extra is label_block or extra is chosen_block:
                            continue
                        try:
                            if int(extra.get("page_index", 0) or 0) != page:
                                continue
                            ex = float(extra.get("x1", 0) or 0)
                            ey = float(extra.get("y1", 0) or 0)
                        except (TypeError, ValueError):
                            continue
                        if ey < end_y - 1 or ey - end_y > max_gap:
                            continue
                        if ex < min(start_x, lx1) - max(20.0, lh * 1.5):
                            continue
                        etext = str(extra.get("text", "")).strip()
                        if not etext or cls._identify_label(etext):
                            continue
                        normalized = cls._label_key(etext)
                        if any(normalized.startswith(cls._label_key(marker)) for marker in SUBJECT_STOP_MARKERS):
                            continue
                        continuations.append((ey, ex, etext))
                    for _, _, extra_text in sorted(continuations)[:3]:
                        if len(chosen_text) >= 420:
                            break
                        chosen_text += " " + extra_text
                value = cls._clean_labeled_value(field, chosen_text)
                if value:
                    result[field] = value
        return result

    @classmethod
    def _clean_labeled_value(cls, field: str, value: str) -> str:
        value = re.sub(r"\s+", " ", (value or "")).strip(" \t\r\n:;—–-")
        if not value:
            return ""
        if field == "received_date":
            return cls._normalize_date_value(value)
        if field in {"from_person", "to_person"}:
            return cls._normalize_fio(value, target=(field == "to_person"))
        if field == "request_subject":
            return cls._clean_subject(value)
        if field == "from_department":
            value = re.sub(r"(?i)^(?:организация|учреждение|подразделение|отдел)\s*[:—–-]?\s*", "", value)
            return value.strip(" .,:;—–-")[:200]
        if field == "from_position":
            return value.strip(" .,:;—–-")[:120]
        return value[:300]

    @staticmethod
    def _date_digit_translation():
        # Restrict OCR character repair to tokens that already have a date
        # shape.  This avoids rewriting normal text while recovering common
        # stamp confusions such as O/О -> 0, I/l/| -> 1, З -> 3 and б -> 6.
        return str.maketrans({
            "O": "0", "o": "0", "О": "0", "о": "0",
            "I": "1", "i": "1", "l": "1", "|": "1",
            "З": "3", "з": "3", "Б": "6", "б": "6",
        })

    @classmethod
    def _date_from_parts(cls, day_raw: str, month_raw: str, year_raw: str) -> str:
        from datetime import date

        digit_map = cls._date_digit_translation()
        try:
            day = int(day_raw.translate(digit_map))
            month = int(month_raw.translate(digit_map))
            year_text = year_raw.translate(digit_map)
            year = int("20" + year_text if len(year_text) == 2 else year_text)
            if not 2015 <= year <= 2035:
                return ""
            date(year, month, day)
            return f"{day:02d}.{month:02d}.{year:04d}"
        except (TypeError, ValueError):
            return ""

    @classmethod
    def _all_normalized_dates(cls, value: str) -> list[str]:
        """Return unique plausible dates in visual order.

        OCR stamps often insert spaces around separators or replace one digit
        with a visually similar Cyrillic character.  We repair only complete
        date-shaped tokens and validate every result with ``datetime.date``.
        """
        text = value or ""
        digit = r"0-9OoОоIil|ЗзБб"
        patterns = (
            rf"(?<!\w)([{digit}]{{1,2}})\s*[./,\\-]\s*([{digit}]{{1,2}})\s*[./,\\-]\s*([{digit}]{{2,4}})(?!\w)",
            rf"(?<!\w)([{digit}]{{1,2}})\s+([{digit}]{{1,2}})\s+([{digit}]{{2,4}})(?!\w)",
        )
        found: list[tuple[int, str]] = []
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                normalized = cls._date_from_parts(*match.groups())
                if normalized:
                    found.append((match.start(), normalized))

        word_pattern = re.compile(
            r"\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|"
            r"сентября|октября|ноября|декабря)\s+(\d{4})\b",
            flags=re.IGNORECASE,
        )
        from datetime import date
        for match in word_pattern.finditer(text):
            try:
                day = int(match.group(1))
                month = RUSSIAN_MONTHS[match.group(2).lower()]
                year = int(match.group(3))
                date(year, month, day)
            except (KeyError, ValueError):
                continue
            if 2015 <= year <= 2035:
                found.append((match.start(), f"{day:02d}.{month:02d}.{year:04d}"))

        found.sort(key=lambda item: item[0])
        unique: list[str] = []
        for _offset, normalized in found:
            if normalized not in unique:
                unique.append(normalized)
        return unique

    @classmethod
    def _normalize_date_value(cls, value: str) -> str:
        dates = cls._all_normalized_dates(value)
        return dates[0] if dates else ""

    @classmethod
    def _normalize_spaced_date(cls, value: str) -> str:
        """Compatibility wrapper for stamp-like whitespace-separated dates."""
        text = value or ""
        digit = r"0-9OoОоIil|ЗзБб"
        match = re.search(
            rf"(?<!\w)([{digit}]{{1,2}})\s+([{digit}]{{1,2}})\s+([{digit}]{{2,4}})(?!\w)",
            text,
        )
        return cls._date_from_parts(*match.groups()) if match else ""

    @classmethod
    def _normalize_fio(cls, raw: str, *, target: bool = False) -> str:
        value = re.sub(r"(?i)^(?:гр\.?|гражданин(?:а|у)?|пациент(?:а|у)?|фио)\s+", "", raw or "")
        value = re.sub(r"\s+", " ", value).strip(" .,:;—–-")
        if not value:
            return ""

        # Surname + initials, including OCR output without perfect spacing/dots.
        # Search inside the value too: forms often contain a title before the FIO
        # ("Начальнику ОП №8 Белкову В.Н.").
        matches = list(re.finditer(r"([А-ЯЁа-яё-]{2,})\s+([А-ЯЁ])\.?\s*([А-ЯЁ])\.?", value))
        if matches:
            match = matches[-1]
            surname = match.group(1).capitalize()
            if target:
                surname = cls._normalize_target_surname(surname)
            return f"{surname} {match.group(2).upper()}.{match.group(3).upper()}."

        matches = list(re.finditer(r"([А-ЯЁ])\.?\s*([А-ЯЁ])\.?\s+([А-ЯЁа-яё-]{2,})", value))
        if matches:
            match = matches[-1]
            surname = match.group(3).capitalize()
            if target:
                surname = cls._normalize_target_surname(surname)
            return f"{surname} {match.group(1).upper()}.{match.group(2).upper()}."

        # Preserve full FIO instead of unnecessarily collapsing it to initials.
        # If a title precedes it, prefer the final three name-like words.
        parts = value.split()
        for count in (3, 2):
            if len(parts) >= count:
                candidate = parts[-count:]
                if all(re.fullmatch(r"[А-ЯЁа-яё-]{2,}", part) for part in candidate):
                    normalized = [part.capitalize() for part in candidate]
                    if target:
                        normalized[0] = cls._normalize_target_surname(normalized[0])
                    return " ".join(normalized)

        return value[:160]

    @staticmethod
    def _looks_low_quality_value(field: str, value: str) -> bool:
        value = (value or "").strip()
        if not value:
            return True
        if field == "received_date":
            return not bool(re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value))
        if field in {"from_person", "to_person"}:
            if value.split()[0].lower() in TARGET_STOP_WORDS:
                return True
            return len(value) < 4 or sum(ch.isalpha() for ch in value) < 3
        if field == "from_department":
            return len(value) < 3 or value.lower() in {"запрос", "письмо", "документ"}
        if field == "request_subject":
            return len(value) < 12 or sum(ch.isalpha() for ch in value) < 8
        return False

    @classmethod
    def _normalize_person_name(cls, raw: str) -> str:
        return cls._normalize_fio(raw, target=False)

    @classmethod
    def _normalize_target_name(cls, raw: str) -> str:
        return cls._normalize_fio(raw, target=True)

    @staticmethod
    def _normalize_target_surname(surname: str) -> str:
        val = surname.strip().capitalize()
        low = val.lower()
        # Common Russian dative/genitive endings found after "на гр." / "в
        # отношении".  Nominative surnames such as "Мамедова" must stay intact.
        for ending, replacement in (
            ("овой", "ова"), ("евой", "ева"), ("иной", "ина"),
            ("ову", "ов"), ("еву", "ев"), ("ину", "ин"),
        ):
            if low.endswith(ending) and len(val) > len(ending) + 1:
                return val[:-len(ending)] + replacement
        return val

    @staticmethod
    def _prepare_ocr_text(text: str) -> str:
        if not text:
            return ""
        text = unicodedata.normalize("NFKC", text)
        text = text.translate(LATIN_LOOKALIKE_MAP)
        # NFKC expands the numero sign № to "No"; restore the familiar Russian
        # notation so department names such as "ОП №8" remain readable.
        text = re.sub(r"\bN[оo]\s*(?=\d)", "№", text)
        return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines() if line.strip())

    @staticmethod
    def _clean_subject(val: str) -> str:
        val = re.sub(r"\s+", " ", val).strip()
        val = re.sub(r"^(?:просим\s+вас|просим|прошу\s+вас|прошу)\s+",
                     "", val, flags=re.IGNORECASE)
        val = re.sub(r"https?://\S+|www\.\S+|\S+@\S+", "", val)
        # Do not swallow signatures/contact blocks when a heuristic regex spans
        # several OCR lines.
        val = re.split(
            r"(?i)\b(?:приложени(?:е|я)|исполнитель|с уважением|телефон|тел\.|"
            r"электронная почта|e-mail|начальник|заместитель|директор|главный врач)\b",
            val, maxsplit=1,
        )[0]
        val = re.sub(r"\s+", " ", val).strip(" .,:;—–-")
        if val:
            val = val[0].upper() + val[1:]
        return val[:SUBJECT_MAX_CHARS]

    def _final_validate(self, result: dict[str, str]) -> dict[str, str]:
        for k, v in result.items():
            result[k] = re.sub(r"\s+", " ", v).strip()
        return result

    @staticmethod
    def _to_request(result: dict[str, str]) -> Request:
        return Request(
            received_date=result.get("received_date", ""),
            from_department=result.get("from_department", ""),
            from_position=result.get("from_position", ""),
            from_person=result.get("from_person", ""),
            to_person=result.get("to_person", ""),
            request_subject=result.get("request_subject", ""),
            status="Новый",
        )
