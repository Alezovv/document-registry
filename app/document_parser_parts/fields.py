from __future__ import annotations

import re
from typing import Any

from app.document_parser_parts.constants import ORGANISATION_TERMS, POSITION_TERMS, TARGET_STOP_WORDS


class FieldExtractionMixin:
    """Heuristic extraction of registry fields from OCR text."""

    @classmethod
    def _extract_department(cls, header: str) -> str:
        dept_regex = re.compile(
            r"\b(?:гуз|гбуз|гауз|кбсмп|бсмп|црб|гкб|кб\s*№?\s*\d+|дгкб|поликлиник\w*|"
            r"больниц\w*|медсанчаст\w*|мсч|диспансер\w*|клиник\w*|минздрав\w*|"
            r"лу\s+мвд|гу\s+мвд|мвд|умвд|омвд|увд|гувд|обдпс|гибдд|оп\s*№?\s*\d+|"
            r"прокуратур\w+|следственн\w+\s+комитет|ск\s+рф|судебн\w+\s+участок|суд\b|"
            r"фссп|гуфссп|уфссп|фсин|гуфсин|военкомат\w*|ифнс|уфнс|"
            r"отдел(?:ение)?\s+[А-ЯЁа-яё0-9№ -]{2,}|управлени\w+\s+[А-ЯЁа-яё -]{2,}|"
            r"департамент\w*|администраци\w*|комитет\w*)\b",
            flags=re.IGNORECASE,
        )
        lines = [line.strip(" .,:;-") for line in header.splitlines() if line.strip()]
        for index, line_clean in enumerate(lines):
            if not dept_regex.search(line_clean):
                continue
            val = re.sub(
                r"^(?:письмо\s*[-—–]\s*запрос|судебный\s+запрос|запрос)\s*",
                "",
                line_clean,
                flags=re.IGNORECASE,
            )
            if len(val) < 4:
                continue

            # Letterheads are frequently split after the authority name:
            # "ГУ МВД России" / "по Московской области".  Preserve a short
            # continuation while stopping before registration/addressee data.
            parts = [val]
            for extra in lines[index + 1:index + 3]:
                if re.search(
                    r"(?i)^(?:вх\.?|исх\.?|№|от\s+\d|дата\b|кому\b|"
                    r"начальник\w*\b|директор\w*\b|главн\w+\s+врач\b)",
                    extra,
                ):
                    break
                if cls._looks_like_person_value(extra):
                    break
                letters = [char for char in extra if char.isalpha()]
                uppercase_ratio = (
                    sum(char.isupper() for char in letters) / len(letters) if letters else 0.0
                )
                if re.match(r"(?i)^(?:по|г\.?|город|район|област|край|республик)", extra) or uppercase_ratio >= 0.72:
                    parts.append(extra)
                else:
                    break
            return re.sub(r"\s+", " ", " ".join(parts)).strip()[:200]

        for line_clean in lines:
            if 4 <= len(line_clean) <= 200 and cls._fuzzy_dictionary_term(
                line_clean, ORGANISATION_TERMS, threshold=0.80
            ):
                return line_clean[:200]
        return ""

    @classmethod
    def _extract_position(cls, footer: str, body: str) -> str:
        position_patterns = (
            r"инспектор\s+ИАЗ",
            r"главный\s+врач",
            r"мировой\s+судья|судья",
            r"оперуполномоченн\w+",
            r"следователь|дознаватель",
            r"УУП(?:\s+ОУУП\s+и\s+ПДН)?",
            r"участков\w+\s+уполномоченн\w+",
            r"врио\s+начальника|заместитель\w*|начальник\w*|директор\w*|заведующ\w+",
            r"лейтенант\s+полиции|капитан\s+полиции|майор\s+полиции|подполковник\s+полиции",
        )
        pattern = re.compile(r"\b(" + "|".join(position_patterns) + r")\b", re.IGNORECASE)

        candidate_lines: list[str] = []
        for source in (footer, body):
            for line in source.splitlines():
                # Executor/contact lines must not become the sender's position.
                if re.search(r"(?i)\b(?:исп\.?|исполнитель|тел\.?|составитель)\b", line):
                    continue
                candidate_lines.append(line)
                match = pattern.search(line)
                if match:
                    value = match.group(0).strip(" .,:;—–-")
                    # Preserve the rank when it is printed on the same police
                    # signature line, but never include the signer's name itself.
                    if re.match(r"(?i)^УУП", value):
                        rank = re.search(r"(?i)\b(?:лейтенант|капитан|майор|подполковник)\s+полиции\b", line)
                        if rank and rank.group(0).lower() not in value.lower():
                            value = f"{value}, {rank.group(0)}"
                    return value[:140]
        for line in candidate_lines:
            fuzzy = cls._fuzzy_dictionary_term(line, POSITION_TERMS, threshold=0.87)
            if fuzzy:
                return fuzzy[:140]
        return ""

    @classmethod
    def _extract_signer(cls, footer: str) -> str:
        # Electronic-signature boxes often contain the only machine-readable full
        # name of a judge/signer ("Владелец: Городилова Татьяна Олеговна").
        owner = re.search(
            r"(?i)\b(?:владелец|сертификат\s+выдан)\s*[:\-]?\s*"
            r"([А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+)",
            footer,
        )
        if owner:
            return cls._normalize_person_name(owner.group(1))

        lines = [line.strip() for line in footer.splitlines() if line.strip()]
        stop_words = {"подготовить", "исполнить", "рассмотреть", "дело", "копия", "согласовано"}
        candidates: list[tuple[float, str]] = []

        for index, line in enumerate(lines):
            local_context = " ".join(lines[max(0, index - 2):index + 1])
            if re.search(r"(?i)\b(?:исп\.?|исполнитель|составитель)\b", line):
                continue

            names: list[str] = []
            for match in re.finditer(r"\b([А-ЯЁ])\.?\s*([А-ЯЁ])\.?\s+([А-ЯЁа-яё-]{2,})\b", line):
                if match.group(3).lower() not in stop_words:
                    names.append(
                        f"{match.group(3).capitalize()} {match.group(1).upper()}.{match.group(2).upper()}."
                    )
            for match in re.finditer(r"\b([А-ЯЁа-яё-]{2,})\s+([А-ЯЁ])\.?\s*([А-ЯЁ])\.?\b", line):
                if match.group(1).lower() not in stop_words:
                    names.append(
                        f"{match.group(1).capitalize()} {match.group(2).upper()}.{match.group(3).upper()}."
                    )
            # Full FIO without an electronic-signature "owner" label.
            for match in re.finditer(
                r"\b([А-ЯЁ][а-яё-]{1,})\s+([А-ЯЁ][а-яё-]{1,})\s+([А-ЯЁ][а-яё-]{1,})\b",
                line,
            ):
                full_name = " ".join(match.groups())
                if not cls._looks_like_organisation(full_name):
                    names.append(cls._normalize_person_name(full_name))

            for name in names:
                score = 50.0 + (index / max(1, len(lines) - 1)) * 10.0
                if cls._contains_dictionary_term(local_context, POSITION_TERMS) or re.search(
                    r"(?i)\b(?:начальник|заместитель|судья|врач|директор|инспектор|"
                    r"следователь|дознаватель|уполномоченный)\b",
                    local_context,
                ):
                    score += 28.0
                if re.search(r"(?i)\b(?:исп\.?|исполнитель|составитель)\b", local_context):
                    score -= 70.0
                if re.search(r"(?i)(?:тел\.?|телефон|@|e-mail|email)", line):
                    score -= 35.0
                candidates.append((score, name))

        if not candidates:
            return ""
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    @classmethod
    def _extract_to_person(cls, text: str) -> str:
        """Extract the person the request concerns, not the letter addressee."""
        patterns = (
            # Medical/legal request formulations from real registry documents.
            r"(?:медицинск\w+\s+(?:карту|документаци\w*)[^\n]{0,120}?\b(?:на|в\s+отношении)\s*(?:гр\.?\s*)?)"
            r"(?P<name>[А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+(?:\s+(?:кызы|оглы))?)",
            r"(?:в\s+отношении|по\s+гражданину|на\s+гражданина|на\s+пациента|на\s+гр\.?)\s+"
            r"(?P<name>[А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+){1,3})",
            r"(?:пострадавш\w+|больн(?:ого|ой|ому|ым|ая|ую)|пациент(?:а|у|ом|ки|ке)?)\b[^\n]{0,50}?"
            r"(?P<name>[А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+(?:\s+(?:кызы|оглы))?)",
            # Initials form used in many police letters.
            r"(?:на\s+(?:гр\.?|гражданина|пациента)?\s*)"
            r"(?P<name>[А-ЯЁ][а-яё-]+\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            value = match.group("name").strip(" .,:;—–-")
            if value.split()[0].lower() in TARGET_STOP_WORDS:
                continue
            normalized = cls._normalize_target_name(value)
            if normalized:
                return normalized
        return ""

    @classmethod
    def _extract_incoming_date(cls, text: str, blocks: list[dict[str, Any]]) -> str:
        """Prefer the registry's incoming stamp date (Вх.) when present."""
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if not re.search(r"(?i)(?:\bвх\b|\bвх\.|\bвходящ\w*)", line):
                continue
            # Prefer a date on the stamp line itself.  Only then inspect nearby
            # lines; otherwise an outgoing letter date immediately below the stamp
            # can incorrectly win.
            value = cls._normalize_date_value(line) or cls._normalize_spaced_date(line)
            if value:
                return value
            neighborhood = " ".join(lines[index : min(len(lines), index + 3)])
            value = cls._normalize_spaced_date(neighborhood) or cls._normalize_date_value(neighborhood)
            if value:
                return value

        # OCR can split a stamp into multiple nearby blocks: one block with "Вх. №"
        # and another handwritten block containing the date.
        for block in blocks or []:
            block_text = str(block.get("text", ""))
            if not re.search(r"(?i)(?:\bвх\b|\bвх\.|\bвходящ\w*)", block_text):
                continue
            try:
                x1, y1 = float(block.get("x1", 0)), float(block.get("y1", 0))
                x2, y2 = float(block.get("x2", x1)), float(block.get("y2", y1))
            except (TypeError, ValueError):
                continue
            nearby = [block_text]
            for candidate in blocks:
                if candidate is block:
                    continue
                try:
                    cx1, cy1 = float(candidate.get("x1", 0)), float(candidate.get("y1", 0))
                    cx2, cy2 = float(candidate.get("x2", cx1)), float(candidate.get("y2", cy1))
                except (TypeError, ValueError):
                    continue
                horizontal_gap = max(0.0, cx1 - x2, x1 - cx2)
                vertical_gap = max(0.0, cy1 - y2, y1 - cy2)
                if horizontal_gap <= 180 and vertical_gap <= 75:
                    nearby.append(str(candidate.get("text", "")))
            combined = " ".join(nearby)
            value = cls._normalize_date_value(combined) or cls._normalize_spaced_date(combined)
            if value:
                return value
        return ""

    @classmethod
    def _extract_received_date(cls, header: str, full_text: str) -> str:
        """Extract the incoming/registration date conservatively.

        v1.22.0 experimented with cross-line ranking.  Proxy-gold showed that
        adjacent-line context could incorrectly promote an unrelated date.
        v1.22.1 restores the proven deterministic order from v1.21 while
        keeping the safer OCR date normalization introduced in v1.22.

        Only a date on the *same line* as an explicit incoming/registration
        marker may outrank the ordinary header fallback.
        """
        priority_patterns = (
            r"дата\s+(?:поступления|регистрации|входящего)",
            r"(?:вх\.?|входящ\w*)\s*(?:№[^\n]{0,40})?\s*от",
            r"(?:поступил\w*|зарегистрир\w*)\s*[:—–-]?",
            r"дата\s*[:—–-]",
        )

        # Highest-confidence evidence: marker and date are on the same line.
        for source in (header or "", full_text or ""):
            lines = [line.strip() for line in source.splitlines() if line.strip()]
            for pattern in priority_patterns:
                for line in lines:
                    if re.search(pattern, line, flags=re.IGNORECASE):
                        value = cls._normalize_date_value(line)
                        if value:
                            return value

        # Proven v1.21 fallback: search contextual header lines only.
        for line in (header or "").splitlines():
            if re.search(r"\b(?:от|исх|вх|дата)\b", line, flags=re.IGNORECASE):
                value = cls._normalize_date_value(line)
                if value:
                    return value

        # Last resort keeps visual order.  We intentionally do not borrow
        # context from neighbouring lines here.
        return cls._normalize_date_value(header) or cls._normalize_date_value(full_text)
