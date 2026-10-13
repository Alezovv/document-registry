from __future__ import annotations

import re
from pathlib import Path


class FilenameParsingMixin:
    """Filename metadata parsing and conservative merge policy."""

    @classmethod
    def extract_from_filename(cls, filename: str) -> dict[str, str]:
        """Extract high-confidence metadata from the operator's filename convention.

        Real registry files often follow a convention such as::

            <organisation> от <date> <sender> - <subject person>
            <organisation> № <number> от <date> - <recipient org> - об <subject>

        Only person-like fragments are allowed into ``to_person``.  This is
        important for filenames such as ``... - МБУ Северное - об отлове...``:
        the organisation is an addressee, not the person the request concerns.
        """
        if not filename:
            return {}

        raw_name = Path(filename).name
        raw_name = re.sub(r"\.pdf$", "", raw_name, flags=re.IGNORECASE).rstrip(" ")
        raw_name = re.sub(r"\.+$", "", raw_name).strip()
        cleaned = re.sub(
            r"^(?:письмо\s*[-—–]\s*запрос|судебный\s+запрос|запрос|письмо)\s*[-—–:]?\s*",
            "",
            raw_name,
            flags=re.IGNORECASE,
        )

        meta: dict[str, str] = {
            "received_date": "",
            "from_department": "",
            "from_person": "",
            "to_person": "",
            "request_subject": "",
        }

        # Date in a filename is normally the document date.  It remains only a
        # fallback: an incoming stamp found by OCR wins in ``parse``.
        date_match = re.search(
            r"\b(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})(?!\d)", cleaned
        )
        if date_match:
            meta["received_date"] = cls._normalize_date_value(date_match.group(0))

        fio_pat = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.?\s*[А-ЯЁ]\.?"
        # Some operators append a tiny marker after initials (e.g. ``А.И.к``).
        fio_loose = rf"{fio_pat}(?:[а-яё]\b)?"
        pair_match = re.search(
            rf"(?P<from>{fio_loose})\s*[-—–]\s*(?P<to>{fio_loose})",
            cleaned,
            flags=re.IGNORECASE,
        )
        if pair_match:
            meta["from_person"] = cls._normalize_person_name(pair_match.group("from"))
            meta["to_person"] = cls._normalize_target_name(pair_match.group("to"))

        # Determine where the sender organisation ends.  Prefer the registration
        # block nearest the date; otherwise the sender-person pair is a useful
        # delimiter for filenames without an explicit date.
        reg_match = None
        if date_match:
            date_re = re.escape(date_match.group(0))
            candidates = list(
                re.finditer(
                    rf"(?:№\s*[A-Za-zА-Яа-яЁё0-9/\-]+\s*)?\bот\s*{date_re}(?:\s*г\.?|\s*года)?(?:\s*№\s*[A-Za-zА-Яа-яЁё0-9/\-]+)?",
                    cleaned,
                    flags=re.IGNORECASE,
                )
            )
            if candidates:
                reg_match = candidates[-1]
            else:
                candidates = list(
                    re.finditer(
                        rf"\bот\s*{date_re}(?:\s*г\.?|\s*года)?(?:\s*№\s*[A-Za-zА-Яа-яЁё0-9/\-]+)?",
                        cleaned,
                        flags=re.IGNORECASE,
                    )
                )
                if candidates:
                    reg_match = candidates[-1]

        if reg_match:
            department = cleaned[: reg_match.start()].strip(" .,:;—–-")
            tail = cleaned[reg_match.end() :].strip(" .,:;—–-")
        elif pair_match:
            department = cleaned[: pair_match.start()].strip(" .,:;—–-")
            tail = cleaned[pair_match.start() :].strip(" .,:;—–-")
        else:
            department = cleaned
            tail = ""

        if cls._looks_like_organisation(department):
            meta["from_department"] = department[:200]

        if pair_match:
            tail = tail.replace(pair_match.group(0), " ")

        # Remove loose registration leftovers before classifying trailing pieces.
        tail = re.sub(
            r"^(?:№\s*[A-Za-zА-Яа-яЁё0-9/\-]+\s*)+", "", tail
        ).strip(" .,:;—–-")
        segments = [
            part.strip(" .,:;—–-")
            for part in re.split(r"\s+[-—–]\s+", tail)
            if part.strip(" .,:;—–-")
        ]

        for segment in segments:
            if cls._looks_like_person_value(segment):
                # Do not replace a deliberate person pair found above.
                if not meta["to_person"]:
                    meta["to_person"] = cls._normalize_target_name(segment)
                continue
            if cls._looks_like_subject_fragment(segment):
                subject = cls._subject_from_filename_fragment(segment)
                if subject:
                    meta["request_subject"] = subject
                continue
            # Organisation-like fragments are usually the addressee.  There is no
            # dedicated organisation-recipient field in the current Request model,
            # so they are intentionally not written into ``to_person``.

        return meta

    @staticmethod
    def _looks_like_organisation(value: str) -> bool:
        value = re.sub(r"\s+", " ", (value or "")).strip()
        if len(value) < 4:
            return False
        return bool(
            re.search(
                r"(?i)\b(?:ГУЗ|ГБУЗ|ГАУЗ|КБСМП|БСМП|ЦРБ|ГКБ|МБУ|МВД|УМВД|ОМВД|"
                r"ГИБДД|ОБДПС|суд|прокуратур|комитет|управлени|отдел|администраци|"
                r"больниц|поликлиник|учреждени|организаци|министерств|департамент)\b",
                value,
            )
        )

    @staticmethod
    def _looks_like_person_value(value: str) -> bool:
        value = re.sub(r"\s+", " ", (value or "")).strip(" .,:;—–-")
        return bool(
            re.fullmatch(
                r"[А-ЯЁ][а-яё-]{1,}\s+(?:[А-ЯЁ]\.\s*[А-ЯЁ]\.?|"
                r"[А-ЯЁ][а-яё-]{1,}\s+[А-ЯЁ][а-яё-]{1,}(?:\s+(?:кызы|оглы))?)",
                value,
            )
        )

    @classmethod
    def _looks_like_subject_fragment(cls, value: str) -> bool:
        value = re.sub(r"\s+", " ", (value or "")).strip(" .,:;—–-")
        if len(value) < 4 or cls._looks_like_organisation(value):
            return False
        if re.match(r"(?i)^(?:о|об|по|касательно|относительно)\s+", value):
            return True
        # A lower-case descriptive tail in the user's filename convention is a
        # subject ("сыворотка противостолбнячная"), not a recipient.
        letters = [ch for ch in value if ch.isalpha()]
        return bool(letters) and sum(ch.islower() for ch in letters) / len(letters) >= 0.75

    @staticmethod
    def _subject_from_filename_fragment(value: str) -> str:
        value = re.sub(r"\s+", " ", (value or "")).strip(" .,:;—–-")
        if not value:
            return ""
        return value[0].upper() + value[1:]

    def _merge_with_filename(
        self,
        result: dict[str, str],
        file_meta: dict[str, str],
        *,
        protected_fields: set[str] | None = None,
    ) -> dict[str, str]:
        """Merge operator filename metadata without overriding explicit form labels.

        For unlabelled official letters a structured filename is often more reliable
        for organisation/sender/subject-person than generic OCR heuristics, while an
        incoming stamp found inside the page must still win for ``received_date``.
        """
        protected_fields = protected_fields or set()

        date_candidate = file_meta.get("received_date", "")
        if (
            "received_date" not in protected_fields
            and date_candidate
            and self._looks_low_quality_value("received_date", result.get("received_date", ""))
        ):
            result["received_date"] = date_candidate

        dept_candidate = file_meta.get("from_department", "")
        if "from_department" not in protected_fields and dept_candidate:
            # Filenames in this workflow are operator-curated and normally contain
            # a much cleaner organisation name than OCR letterheads.
            result["from_department"] = dept_candidate

        for field in ("from_person", "to_person"):
            candidate = file_meta.get(field, "")
            if field not in protected_fields and candidate and self._looks_like_person_value(candidate):
                result[field] = candidate

        ocr_subj = result.get("request_subject", "")
        file_subj = file_meta.get("request_subject", "")
        if (
            "request_subject" not in protected_fields
            and file_subj
            and (not ocr_subj or self._looks_low_quality_value("request_subject", ocr_subj))
        ):
            result["request_subject"] = file_subj
        return result
