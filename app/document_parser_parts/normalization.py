from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from app.document_parser_parts.similarity import ratio_upper_bound
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
        # Correct an OCR zero only when it is embedded between letters. Do not
        # rewrite real zeros in document numbers, dates, or numbered labels.
        value = re.sub(r"(?<=[а-яa-z])0(?=[а-яa-z])", "о", value)
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
                    if key.startswith(("адрес", "по адрес", "на адрес")) and alias_key == "адресат":
                        continue
                    # Different-length labels cannot match above the bound;
                    # avoid running SequenceMatcher for those pairs.
                    max_score = ratio_upper_bound(key, alias_key)
                    if max_score < 0.86 or (best is not None and max_score <= best[0]):
                        continue
                    score = SequenceMatcher(None, key, alias_key).ratio()
                    if score >= 0.86 and (best is None or score > best[0]):
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
            cleaned = cls._normalize_fio(value, target=(field == "to_person"))
            if any(w in TARGET_STOP_WORDS for w in cleaned.lower().split()):
                return ""
            return cleaned
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
            if len(year_text) == 2:
                yy = int(year_text)
                year = 2000 + yy if yy <= 35 else 1900 + yy
            else:
                year = int(year_text)
            if not 1920 <= year <= 2050:
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
            rf"(?<!\w)([{digit}]{{1,2}})\s*[./,\\]\s*([{digit}]{{1,2}})\s*[./,\\]\s*([{digit}]{{2,4}})(?:\s*(?:[гГrR]\.?|[гГ]ода))?(?!\w)",
            rf"(?<!\w)([{digit}]{{1,2}})\s+([{digit}]{{1,2}})\s+([{digit}]{{2,4}})(?:\s*(?:[гГrR]\.?|[гГ]ода))?(?!\w)",
        )
        iso_pattern = rf"(?<!\w)([{digit}]{{4}})-([{digit}]{{1,2}})-([{digit}]{{1,2}})(?!\w)"
        phone_marker_re = re.compile(r"(?i)\b(?:тел|факс|телефон|т/ф|phone|т)\b|\b8\s*\(|\+7")

        found: list[tuple[int, str]] = []
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                if phone_marker_re.search(text[max(0, match.start() - 30):match.start()]):
                    continue
                normalized = cls._date_from_parts(*match.groups()[:3])
                if normalized:
                    found.append((match.start(), normalized))

        for match in re.finditer(iso_pattern, text):
            if phone_marker_re.search(text[max(0, match.start() - 30):match.start()]):
                continue
            g = match.groups()
            normalized = cls._date_from_parts(g[2], g[1], g[0])
            if normalized:
                found.append((match.start(), normalized))

        word_pattern = re.compile(
            r"\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|"
            r"сентября|октября|ноября|декабря)\s+(\d{4})(?:\s*(?:[гГ]\.?|[гГ]ода))?(?!\w)",
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
            if 1920 <= year <= 2050:
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
            rf"(?<!\w)([{digit}]{{1,2}})\s+([{digit}]{{1,2}})\s+([{digit}]{{2,4}})(?:\s*(?:[гГrR]\.?|[гГ]ода))?(?!\w)",
            text,
        )
        return cls._date_from_parts(*match.groups()[:3]) if match else ""

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

        matches = list(re.finditer(
            r"(?:([А-ЯЁ])\.\s*([А-ЯЁ])\.\s*([А-ЯЁа-яё-]{2,})|([А-ЯЁ])\.?\s+([А-ЯЁ])\.?\s*([А-ЯЁа-яё-]{2,}))",
            value,
        ))
        if matches:
            match = matches[-1]
            groups = [g for g in match.groups() if g is not None]
            if len(groups) == 3:
                surname = groups[2].capitalize()
                if target:
                    surname = cls._normalize_target_surname(surname)
                return f"{surname} {groups[0].upper()}.{groups[1].upper()}."

        # Preserve full FIO instead of unnecessarily collapsing it to initials.
        # If a title precedes it, prefer the final three name-like words.
        parts = value.split()
        for count in (3, 2):
            if len(parts) >= count:
                candidate = parts[-count:]
                if all(re.fullmatch(r"[А-ЯЁа-яё-]{2,}", part) for part in candidate):
                    normalized = [part.capitalize() for part in candidate]
                    if target:
                        if count == 3:
                            normalized = cls._normalize_target_full_fio(normalized)
                        else:
                            normalized = cls._normalize_target_two_word_fio(normalized)
                    return " ".join(normalized)

        return value[:160]

    _INDECLINABLE_SURNAME_ENDINGS = ("ко", "их", "ых", "иа", "уа", "дзе", "швили", "янц")
    # These masculine adjectival surnames are easily confused with feminine
    # oblique forms by a suffix-only normalizer. Keep their declensions explicit.
    _MASCULINE_OY_SURNAME_FORMS = {
        "яровой": "Яровой", "ярового": "Яровой", "яровому": "Яровой",
        "яровым": "Яровой", "яровом": "Яровой",
        "боровой": "Боровой", "борового": "Боровой", "боровому": "Боровой",
        "боровым": "Боровой", "боровом": "Боровой",
        "кривой": "Кривой", "кривого": "Кривой", "кривому": "Кривой",
        "кривым": "Кривой", "кривом": "Кривой",
    }

    @classmethod
    def _normalize_male_surname(cls, sur: str) -> str:
        sur_low = sur.lower()
        if sur_low in cls._MASCULINE_OY_SURNAME_FORMS:
            return cls._MASCULINE_OY_SURNAME_FORMS[sur_low]
        if sur_low.endswith(cls._INDECLINABLE_SURNAME_ENDINGS):
            return sur
        if sur_low.endswith(("овым", "евым", "иным", "яном")):
            return sur[:-2]
        if sur_low.endswith(("ина", "ину", "ине", "ова", "ову", "ове", "ева", "еву", "еве", "яна", "яну", "яне")):
            return sur[:-1]
        if sur_low.endswith(("ского", "цкого", "скому", "цкому")):
            return sur[:-3] + "ий"
        if sur_low.endswith(("ским", "цким", "ском", "цком")):
            return sur[:-2] + "ий"
        if sur_low.endswith(("ового", "евого", "овому", "евому")):
            return sur[:-3]
        if sur_low.endswith(("ого", "ому")):
            return sur[:-3] + "ый"
        # Do not infer an adjective ending from plain -ым/-ом. Nominative
        # surnames such as «Пахом» or «Бергстром» may legitimately end this
        # way; oblique forms of common -ов/-ев/-ин surnames are handled above.
        return sur

    @classmethod
    def _normalize_female_surname(cls, sur: str) -> str:
        sur_low = sur.lower()
        if sur_low.endswith(cls._INDECLINABLE_SURNAME_ENDINGS):
            return sur
        if sur_low.endswith(("овой", "евой", "иной")):
            return sur[:-2] + "а"
        if sur_low.endswith(("ову", "еву", "ину")):
            return sur[:-1] + "а"
        if sur_low.endswith(("ской", "цкой")) or (
            sur_low.endswith(("ой", "ую")) and not sur_low.endswith(("овой", "евой", "иной"))
        ):
            return sur[:-2] + "ая"
        return sur

    @classmethod
    def _normalize_target_two_word_fio(cls, parts: list[str]) -> list[str]:
        if len(parts) != 2:
            return parts
        sur, name = parts[0], parts[1]
        sur_low = sur.lower()
        name_low = name.lower()

        MALE_GENITIVE_NAMES = {
            "дмитрия", "василия", "юрия", "валерия", "григория", "анатолия",
            "георгия", "евгения", "аркадия", "виталия", "геннадия", "илия",
        }
        if sur_low.endswith(("ова", "ева", "ина", "ая", "яя")) and name_low.endswith(("ия", "ья")):
            if name_low not in MALE_GENITIVE_NAMES:
                return [sur, name]

        is_female = False
        if sur_low in {"яровой", "боровой", "кривой"}:
            # These are attested masculine nominative surname forms, not
            # feminine suffixes merely because they end in «-овой/-вой».
            is_female = False
        elif sur_low.endswith(("овой", "евой", "иной", "ской", "цкой", "ая", "яя")):
            is_female = True
        elif sur_low.endswith(("ову", "еву", "ину")):
            if name_low.endswith(("ию", "ью", "ии", "ьи", "а", "я")):
                is_female = True
            elif name_low.endswith("у") and not name_low.endswith(("ову", "еву", "ину")):
                is_female = True

        if is_female:
            sur = cls._normalize_female_surname(sur)
            if name_low.endswith(("ию", "ие", "ии")):
                name = name[:-2] + "ия"
            elif name_low.endswith(("ью", "ье", "ьи")):
                name = name[:-2] + "ья"
            elif name_low.endswith(("ой", "ей")) and len(name) > 3:
                name = name[:-2] + "а"
            elif name_low.endswith(("у", "е")) and len(name) > 2:
                name = name[:-1] + "а"
        else:
            sur = cls._normalize_male_surname(sur)
            if name_low.endswith(("ия", "ию", "ие", "ием")):
                stem = name[:-3] if name_low.endswith("ием") else name[:-2]
                name = stem + "ий"
            elif name_low.endswith(("ея", "ею", "еем")):
                stem = name[:-3] if name_low.endswith("еем") else name[:-2]
                name = stem + "ей"
            elif name_low.endswith(("ря", "рю")):
                name = name[:-1] + "ь"
            elif name_low.endswith("рем"):
                name = name[:-2] + "ь"
            elif name_low.endswith(("ом", "ем")) and len(name) > 3:
                name = name[:-2]
            elif name_low.endswith(("а", "у", "е")) and len(name) > 3:
                name = name[:-1]

        return [sur.capitalize(), name.capitalize()]

    @classmethod
    def _normalize_target_full_fio(cls, parts: list[str]) -> list[str]:
        if len(parts) != 3:
            return parts
        sur, name, patr = parts[0], parts[1], parts[2]
        patr_low = patr.lower()
        name_low = name.lower()
        if re.search(r"(?:ович|евич|ич)[а-яё]*$", patr_low):
            patr = re.sub(r"(?:овича|овичу|овичем|овиче)$", "ович", patr, flags=re.I)
            patr = re.sub(r"(?:евича|евичу|евичем|евиче)$", "евич", patr, flags=re.I)
            patr = re.sub(r"(?:ича|ичу|ичем|иче)$", "ич", patr, flags=re.I)

            if name_low.endswith(("ия", "ию", "ие", "ием")):
                stem = name[:-3] if name_low.endswith("ием") else name[:-2]
                name = stem + "ий"
            elif name_low.endswith(("ея", "ею", "еем")):
                stem = name[:-3] if name_low.endswith("еем") else name[:-2]
                name = stem + "ей"
            elif name_low.endswith(("ря", "рю")):
                name = name[:-1] + "ь"
            elif name_low.endswith("рем"):
                name = name[:-2] + "ь"
            elif name_low.endswith(("ом", "ем")) and len(name) > 3:
                name = name[:-2]
            elif name_low.endswith(("а", "у", "е")) and len(name) > 3:
                name = name[:-1]

            sur = cls._normalize_male_surname(sur)
        elif re.search(r"(?:овн|евн|ичн|иничн)[а-яё]*$", patr_low):
            patr = re.sub(r"(?:овны|овне|овну|овной)$", "овна", patr, flags=re.I)
            patr = re.sub(r"(?:евны|евне|евну|евной)$", "евна", patr, flags=re.I)
            patr = re.sub(r"(?:ичны|ичне|ичну|ичной)$", "ична", patr, flags=re.I)
            patr = re.sub(r"(?:иничны|иничне|иничну|иничной)$", "инична", patr, flags=re.I)

            if name_low.endswith(("ьи", "ье", "ью", "ьей")):
                stem = name[:-3] if name_low.endswith("ьей") else name[:-2]
                name = stem + "ья"
            elif name_low.endswith(("ии", "ию", "ие", "ией")):
                stem = name[:-3] if name_low.endswith("ией") else name[:-2]
                name = stem + "ия"
            elif name_low.endswith(("ой", "ей")) and len(name) > 3:
                name = name[:-2] + "а"
            elif name_low.endswith(("ы", "е", "у", "о")) and len(name) > 2:
                name = name[:-1] + "а"

            sur = cls._normalize_female_surname(sur)
        return [sur.capitalize(), name.capitalize(), patr.capitalize()]

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
            return (
                len(value) < 3
                or value.lower() in {"запрос", "письмо", "документ"}
                or bool(re.match(r"^\d+[.\s]+(?:[A-Za-zА-Яа-яЁё]\s+)?\d{4}\b", value))
            )
        if field == "request_subject":
            return len(value) < 12 or sum(ch.isalpha() for ch in value) < 8
        return False

    @classmethod
    def _normalize_person_name(cls, raw: str) -> str:
        return cls._normalize_fio(raw, target=False)

    @classmethod
    def _normalize_target_name(cls, raw: str) -> str:
        return cls._normalize_fio(raw, target=True)

    @classmethod
    def _normalize_target_surname(cls, surname: str) -> str:
        val = surname.strip().capitalize()
        low = val.lower()
        if low in cls._MASCULINE_OY_SURNAME_FORMS:
            return cls._MASCULINE_OY_SURNAME_FORMS[low]
        if low.endswith(cls._INDECLINABLE_SURNAME_ENDINGS):
            return val
        for ending, replacement in (
            ("овой", "ова"), ("евой", "ева"), ("иной", "ина"),
            ("овым", "ов"), ("евым", "ев"), ("иным", "ин"),
            ("ову", "ов"), ("еву", "ев"), ("ину", "ин"),
            ("ове", "ов"), ("еве", "ев"), ("ине", "ин"),
            ("ского", "ский"), ("цкого", "цкий"),
            ("скому", "ский"), ("цкому", "цкий"),
            ("ским", "ский"), ("цким", "цкий"),
            ("ском", "ский"), ("цком", "цкий"),
            ("ской", "ская"), ("цкой", "цкая"), ("жной", "жная"),
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
        # Repair Latin characters mistakenly embedded by OCR inside predominantly Cyrillic words
        mixed_map = {"i": "и", "n": "н", "u": "и", "t": "т", "d": "д", "m": "м", "r": "г"}
        def _repair_word(m: re.Match) -> str:
            word = m.group(0)
            cyr_count = sum(1 for ch in word if ("а" <= ch.lower() <= "я" or ch.lower() == "ё"))
            if cyr_count >= 2:
                return "".join(mixed_map.get(ch, ch) for ch in word)
            return word
        text = re.sub(r"\b[A-Za-zА-ЯЁа-яё0-9\.\-_]+\b", _repair_word, text)
        # NFKC expands the numero sign № to "No"; restore the familiar Russian
        # notation so department names such as "ОП №8" remain readable.
        text = re.sub(r"\bN[оo]\s*(?=\d)", "№", text)
        return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines() if line.strip())

    _STATUTE_PREAMBLE_RE = re.compile(
        r"(?i)\b(?:ст\.|стать[иейя]|упк|гпк|коап|апк|ук\s+рф|фз|федеральн\w+\s+закон|"
        r"руководствуясь|на\s+основании|в\s+соответствии\s+с)\b"
    )

    @classmethod
    def _clean_subject(cls, val: str) -> str:
        val = re.sub(r"\s+", " ", val).strip()
        req_match = re.search(
            r"(?i)\b(?:просим\s+вас|просим|прошу\s+вас|прошу|ходатайствую|требуется|необходимо)\b",
            val,
        )
        if req_match and req_match.start() > 0:
            prefix = val[:req_match.start()].strip()
            if len(prefix) > 120 or cls._STATUTE_PREAMBLE_RE.search(prefix):
                val = val[req_match.start():].strip()
        val = re.sub(r"^(?:просим\s+вас|просим|прошу\s+вас|прошу)\s+",
                     "", val, flags=re.IGNORECASE)
        val = re.sub(r"https?://\S+|www\.\S+|\S+@\S+", "", val)
        # Do not swallow signatures/contact blocks when a heuristic regex spans
        # several OCR lines.
        val = re.split(
            r"(?i)\b(?:приложени(?:е|я)|исполнитель|с уважением|телефон|тел\.|"
            r"электронная почта|e-mail)\b",
            val, maxsplit=1,
        )[0]
        val = re.sub(r"\s+", " ", val).strip(" .,:;—–-?")
        if val:
            val = val[0].upper() + val[1:]
        return val[:SUBJECT_MAX_CHARS]

    def _final_validate(self, result: dict[str, str]) -> dict[str, str]:
        for k, v in result.items():
            result[k] = re.sub(r"\s+", " ", v).strip()
        dept = result.get("from_department", "")
        if re.search(r"(?i)(?:кбсмп|бсмп|больниц\w*[^.\n]{0,60}?15|кб\s*№?\s*15)", dept):
            is_canonical_fingerprint = bool(
                re.search(r"(?i)\bгосударственн\w+\s+учреждени\w+\s+здравоохранени\w+.*больниц\w*.*15", dept)
            )
            dept_cleaned = re.sub(
                r"(?i)\s*(?:государственн\w+\s+учреждени\w+.*|г[уа]з\s+.*|(?:«|\")?(?:клиническ\w+\s+)?(?:больниц\w*|кбсмп|бсмп)\s*.*15.*)",
                "",
                dept,
            ).strip(" .,:;-")
            if (
                len(dept_cleaned) >= 4
                and not re.match(r"^\d+[.\s]+(?:[A-Za-zА-Яа-яЁё]\s+)?\d{4}\b", dept_cleaned)
                and dept_cleaned.lower() not in {"гуз", "гбуз", "гауз", "больница", "кбсмп", "бсмп"}
            ):
                result["from_department"] = dept_cleaned
            elif is_canonical_fingerprint:
                # Authentic outgoing or internal document originating from the hospital itself
                result["from_department"] = dept
            else:
                result["from_department"] = ""
        target_person = result.get("to_person", "")
        if target_person:
            result["to_person"] = self._normalize_target_name(target_person)
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
