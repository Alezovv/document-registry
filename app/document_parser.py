from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.models import Request

logger = logging.getLogger(__name__)

PARSER_VERSION = "1.16.0"

FIELD_NAMES = (
    "received_date",
    "from_department",
    "from_position",
    "from_person",
    "to_person",
    "request_subject",
)

LATIN_LOOKALIKE_MAP = str.maketrans(
    {
        "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н",
        "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т",
        "X": "Х", "Y": "У", "a": "а", "c": "с", "e": "е",
        "o": "о", "p": "р", "x": "х", "y": "у",
    }
)

TARGET_STOP_WORDS = {
    "руки", "дело", "лечении", "запрос", "основании", "адрес",
    "выписку", "карту", "историю", "справку", "сведения", "информацию",
    "документы", "перечень", "комиссии", "производству", "материалам",
    "уведомление", "контроль", "согласование"
}


FIELD_LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "received_date": (
        "дата поступления", "дата регистрации", "дата входящего",
        "входящая дата", "вх дата", "дата",
    ),
    "from_department": (
        "отдел", "подразделение", "организация", "учреждение",
        "организация отправитель", "отправитель организация",
    ),
    "from_position": ("должность", "должность отправителя"),
    "from_person": (
        "от кого", "подписант", "фио отправителя",
        "ф и о отправителя",
    ),
    "to_person": (
        "на кого", "кому", "получатель", "адресат", "фио получателя",
        "ф и о получателя",
    ),
    "request_subject": (
        "суть запроса", "тема запроса", "предмет запроса", "тема",
        "содержание запроса", "содержание",
    ),
}

SUBJECT_STOP_MARKERS = (
    "приложение", "приложения", "исполнитель", "телефон", "тел.",
    "электронная почта", "e-mail", "email", "с уважением",
    "начальник", "заместитель", "директор", "главный врач",
    "подпись",
)

# Lightweight semantic anchors for the registry field ``request_subject``.
# They are deliberately lexical/rule-based: no extra model is loaded and RAM
# usage stays essentially unchanged.
SUBJECT_REQUEST_ANCHORS = (
    "прошу", "просим", "просит", "предлагаю", "ходатайствую",
    "необходимо", "требуется",
)
SUBJECT_ACTION_VERBS = (
    "выдать", "предоставить", "направить", "выслать", "сообщить",
    "подготовить", "уточнить", "перераспределить", "произвести",
    "поручить", "организовать", "обеспечить", "рассмотреть", "оказать",
    "осуществить", "провести", "проверить", "принять", "согласовать",
    "вернуть", "выделить", "доставить", "отловить", "госпитализировать",
)
SUBJECT_CONTEXT_ANCHORS = (
    "в связи с", "по вопросу", "касательно", "относительно",
    "в целях", "для проведения", "сообщаем", "уведомляем",
    "информируем", "направляем", "направляю", "в ответ на",
    "на ваше обращение", "рассмотрев",
)
SUBJECT_OBJECT_TERMS = (
    "сведения", "информац", "документ", "карт", "справк", "выписк",
    "лечени", "пациент", "граждан", "дтп", "провер", "материал",
    "сыворот", "отлов", "медицин", "обращен", "запрос", "копи",
    "заключени", "характеристик", "данн", "помощ",
)

# ``request_subject`` is intentionally allowed to contain a useful paragraph,
# not only a 300-character preview.  The parser still stops at signatures and
# contact blocks so this does not turn into a copy of the whole document.
SUBJECT_MAX_CHARS = 1200
SUBJECT_MAX_LINES = 10

RUSSIAN_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

# Small local dictionaries intentionally cover only stable, common official terms.
# They are used to recover OCR typos, never to invent a missing value.
ORGANISATION_TERMS = (
    "ГИБДД", "ОБДПС", "ГУ МВД", "УМВД", "ОМВД", "МВД", "полиция",
    "прокуратура", "следственный комитет", "суд", "ФССП", "ФСИН",
    "ГБУЗ", "ГАУЗ", "ГУЗ", "ЦРБ", "ГКБ", "больница", "поликлиника",
    "министерство", "департамент", "управление", "отдел", "администрация",
)

POSITION_TERMS = (
    "главный врач", "мировой судья", "судья", "инспектор ИАЗ",
    "оперуполномоченный", "следователь", "дознаватель",
    "участковый уполномоченный", "начальник", "заместитель начальника",
    "директор", "заведующий", "лейтенант полиции", "капитан полиции",
    "майор полиции", "подполковник полиции",
)

FIELD_CONFIDENCE_FLOOR = {
    "received_date": 0.58,
    "from_department": 0.56,
    "from_position": 0.56,
    "from_person": 0.62,
    "to_person": 0.64,
    "request_subject": 0.58,
}


@dataclass(slots=True)
class ParseDiagnostics:
    field_confidence: dict[str, float] = field(default_factory=dict)
    field_sources: dict[str, str] = field(default_factory=dict)
    document_number: str = ""
    document_number_confidence: float = 0.0
    # Top candidates are useful for diagnostics/tuning and contain no OCR text
    # unless a developer explicitly logs this in a debug build.
    field_candidate_scores: dict[str, list[float]] = field(default_factory=dict)
    field_regions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


class DocumentParserError(RuntimeError):
    pass


class DocumentParser:
    """
    Детерминированный парсер документов (100% локально, без LLM и внешних NLP-библиотек).
    Использует пространственный анализ OCR и глубокий парсинг шаблонов имени файла.
    """

    def __init__(self) -> None:
        self.last_diagnostics = ParseDiagnostics()
        self._subject_candidate_scores: list[float] = []

    def parse(
        self,
        ocr_data: str | dict[str, Any],
        file_name: str = "",
    ) -> Request:
        if isinstance(ocr_data, dict):
            text = ocr_data.get("text", "")
            source_blocks = ocr_data.get("blocks", [])
            blocks = []
            for block in source_blocks:
                copied = dict(block)
                if isinstance(copied.get("text"), str):
                    copied["text"] = self._prepare_ocr_text(copied["text"])
                blocks.append(copied)
            if not file_name:
                file_name = ocr_data.get("file_name", "")
        else:
            text = str(ocr_data)
            blocks = []

        text = self._prepare_ocr_text(text)
        if not text and not file_name:
            raise DocumentParserError("Текст после OCR пуст и имя файла не передано.")

        explicit = self._extract_labeled_fields(text)
        spatial = self._extract_spatial_labeled_fields(blocks)
        for key, value in spatial.items():
            explicit.setdefault(key, value)

        zones = self._layout_zones(text, blocks)
        header_text = zones["header"]
        body_text = zones["body"]
        footer_text = zones["footer"]
        signature_text = zones.get("signature", "") or footer_text
        addressee_text = zones["addressee"]
        incoming_text = zones["incoming"]
        content_text = zones.get("content", "") or body_text
        self._subject_candidate_scores = []

        heuristic = {
            "received_date": (
                self._extract_incoming_date(incoming_text or text, blocks)
                or self._extract_received_date(header_text, text)
            ),
            "from_department": self._extract_department(header_text or text),
            "from_position": self._extract_position(signature_text, body_text),
            "from_person": self._extract_signer(signature_text or text),
            # Addressee zone is useful for labelled/form letters, but the semantic
            # target extractor still requires person-context before accepting a FIO.
            "to_person": (
                self._extract_to_person(content_text)
                or self._extract_to_person(body_text)
                or self._extract_to_person(addressee_text)
                or self._extract_to_person(text)
            ),
            "request_subject": (
                self._extract_subject_with_scores(content_text)
                or self._extract_subject_with_scores(text)
            ),
        }

        result: dict[str, str] = {}
        confidence: dict[str, float] = {}
        sources: dict[str, str] = {}
        for field_name in FIELD_NAMES:
            if field_name in explicit and explicit[field_name]:
                result[field_name] = explicit[field_name]
                confidence[field_name] = 0.97 if field_name in spatial else 0.95
                sources[field_name] = "label/layout" if field_name in spatial else "label"
            else:
                value = heuristic.get(field_name, "")
                result[field_name] = value
                confidence[field_name] = self._heuristic_confidence(field_name, value, blocks, zones)
                sources[field_name] = "layout/heuristic" if value else "none"

        if file_name:
            file_meta = self.extract_from_filename(file_name)
            previous = dict(result)
            result = self._merge_with_filename(result, file_meta, protected_fields=set(explicit))
            for field_name in FIELD_NAMES:
                if result.get(field_name) and result.get(field_name) != previous.get(field_name):
                    confidence[field_name] = max(confidence.get(field_name, 0.0), 0.88)
                    sources[field_name] = "filename"

        result = self._final_validate(result)
        for field_name in FIELD_NAMES:
            value = result.get(field_name, "")
            score = confidence.get(field_name, 0.0)
            if value and score < FIELD_CONFIDENCE_FLOOR[field_name]:
                logger.info(
                    "Поле %s отброшено как сомнительное (confidence=%.2f)",
                    field_name, score,
                )
                result[field_name] = ""
                sources[field_name] = "rejected"

        document_number, document_number_confidence = self._extract_document_number(
            header_text + "\n" + "\n".join(text.splitlines()[:12]), blocks
        )
        field_regions = self._locate_field_regions(result, blocks, sources)
        self.last_diagnostics = ParseDiagnostics(
            field_confidence={name: round(confidence.get(name, 0.0), 3) for name in FIELD_NAMES},
            field_sources=sources,
            document_number=document_number,
            document_number_confidence=round(document_number_confidence, 3),
            field_candidate_scores={
                "request_subject": [round(score, 3) for score in self._subject_candidate_scores[:5]]
            },
            field_regions=field_regions,
        )
        logger.debug("Распознано: %s; confidence=%s", result, self.last_diagnostics.field_confidence)
        return self._to_request(result)

    # ================================================================
    # ПАРСИНГ ИМЕНИ ФАЙЛА
    # ================================================================

    # ================================================================
    # ПАРСИНГ ИМЕНИ ФАЙЛА
    # ================================================================

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

    # ================================================================
    # ЯВНЫЕ ПОДПИСИ ПОЛЕЙ
    # ================================================================

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

    @classmethod
    def _normalize_date_value(cls, value: str) -> str:
        from datetime import date

        digit_map = str.maketrans({"O": "0", "o": "0", "О": "0", "I": "1", "l": "1", "|": "1"})
        numeric = re.search(
            r"(?<!\w)([0-9OoОIl|]{1,2})[./-]([0-9OoОIl|]{1,2})[./-]([0-9OoОIl|]{2,4})(?!\w)",
            value,
        )
        if numeric:
            try:
                day = int(numeric.group(1).translate(digit_map))
                month = int(numeric.group(2).translate(digit_map))
                year_text = numeric.group(3).translate(digit_map)
                year = int("20" + year_text if len(year_text) == 2 else year_text)
                if 2015 <= year <= 2035:
                    date(year, month, day)
                    return f"{day:02d}.{month:02d}.{year:04d}"
            except ValueError:
                pass

        word_date = re.search(
            r"\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(\d{4})\b",
            value,
            flags=re.IGNORECASE,
        )
        if word_date:
            try:
                day = int(word_date.group(1))
                month = RUSSIAN_MONTHS[word_date.group(2).lower()]
                year = int(word_date.group(3))
                date(year, month, day)
                return f"{day:02d}.{month:02d}.{year:04d}"
            except (KeyError, ValueError):
                pass
        return ""

    @classmethod
    def _normalize_spaced_date(cls, value: str) -> str:
        """Parse stamp-like dates where OCR separates day/month/year by spaces."""
        from datetime import date

        digit_map = str.maketrans({"O": "0", "o": "0", "О": "0", "I": "1", "l": "1", "|": "1"})
        match = re.search(
            r"(?<!\d)([0-9OoОIl|]{1,2})\s+([0-9OoОIl|]{1,2})\s+([0-9OoОIl|]{2,4})(?!\d)",
            value or "",
        )
        if not match:
            return ""
        try:
            day = int(match.group(1).translate(digit_map))
            month = int(match.group(2).translate(digit_map))
            year_text = match.group(3).translate(digit_map)
            year = int("20" + year_text if len(year_text) == 2 else year_text)
            if not 2015 <= year <= 2035:
                return ""
            date(year, month, day)
            return f"{day:02d}.{month:02d}.{year:04d}"
        except ValueError:
            return ""

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

    # ================================================================
    # ИЗВЛЕЧЕНИЕ ПОЛЕЙ ИЗ OCR ТЕКСТА
    # ================================================================

    def _extract_subject_with_scores(self, text: str) -> str:
        value, scores = self._extract_subject_ranked(text)
        if scores:
            self._subject_candidate_scores.extend(scores)
            self._subject_candidate_scores = sorted(
                self._subject_candidate_scores, reverse=True
            )[:8]
        return value

    @classmethod
    def _extract_subject(cls, text: str) -> str:
        """Compatibility wrapper returning only the best subject candidate."""
        return cls._extract_subject_ranked(text)[0]

    @classmethod
    def _extract_subject_ranked(cls, text: str) -> tuple[str, list[float]]:
        """Return the best semantic subject and candidate scores.

        The parser deliberately stays deterministic and lightweight.  Instead of
        feeding the whole OCR text to another model, it ranks short local text
        windows using request verbs, business-language anchors, layout-safe noise
        filters and object words.  This gives ``request_subject`` a useful fallback
        for letters that do not literally contain ``Прошу предоставить``.
        """
        if not text:
            return "", []

        raw_lines = []
        for raw in text.splitlines():
            line = re.sub(r"\s+", " ", raw or "").strip()
            if not line or re.match(r"^---\s*Страница\s+\d+\s*---$", line, re.IGNORECASE):
                continue
            raw_lines.append(line)
        if not raw_lines:
            return "", []

        candidates: list[tuple[float, str]] = []

        def add(raw_value: str, base: float) -> None:
            value = cls._clean_subject(raw_value)
            if not value or cls._subject_is_noise(value):
                return
            score = base + cls._subject_quality_score(value)
            # De-duplicate near-identical windows while keeping the stronger one.
            key = cls._label_key(value)
            for index, (old_score, old_value) in enumerate(candidates):
                old_key = cls._label_key(old_value)
                if key == old_key or (
                    min(len(key), len(old_key)) >= 18
                    and SequenceMatcher(None, key, old_key).ratio() >= 0.93
                ):
                    if score > old_score:
                        candidates[index] = (score, value)
                    return
            candidates.append((score, value))

        request_pattern = re.compile(
            r"(?i)\b(?:прошу|просим|просит|предлагаю|ходатайствую)\b"
        )
        action_pattern = re.compile(
            r"(?i)\b(?:" + "|".join(map(re.escape, SUBJECT_ACTION_VERBS)) + r")\w*\b"
        )
        context_pattern = re.compile(
            r"(?i)\b(?:" + "|".join(map(re.escape, SUBJECT_CONTEXT_ANCHORS)) + r")\b"
        )

        def is_boundary(line: str) -> bool:
            normalized = cls._label_key(line)
            if any(normalized.startswith(cls._label_key(marker)) for marker in SUBJECT_STOP_MARKERS):
                return True
            return bool(re.match(
                r"(?i)^(?:исх\.?|вх\.?|№|дата\b|тел\.?|телефон|исп\.?|"
                r"исполнитель|e-mail|email|подпись\b)",
                line,
            ))

        def collect_lines(start: int) -> str:
            parts: list[str] = []
            total = 0
            for offset, candidate_line in enumerate(raw_lines[start:start + SUBJECT_MAX_LINES]):
                if offset and is_boundary(candidate_line):
                    break
                if offset and cls._looks_like_person_value(candidate_line):
                    # A lone FIO after a complete sentence is normally a signature.
                    joined = " ".join(parts).rstrip()
                    if joined.endswith((".", "!", "?")):
                        break
                projected = total + len(candidate_line) + (1 if parts else 0)
                if projected > SUBJECT_MAX_CHARS:
                    remaining = SUBJECT_MAX_CHARS - total - (1 if parts else 0)
                    if remaining > 20:
                        parts.append(candidate_line[:remaining].rstrip())
                    break
                parts.append(candidate_line)
                total = projected
            return " ".join(parts)

        # A semantic thought is commonly split over many OCR lines. Collect the
        # paragraph from the anchor until a signature/contact boundary instead
        # of hard-cutting it to two or three lines.
        for index, line in enumerate(raw_lines):
            paragraph = collect_lines(index)
            two_line = " ".join(raw_lines[index:index + 2])

            if request_pattern.search(line):
                add(paragraph, 104.0)
                continue
            if action_pattern.search(line):
                add(paragraph, 94.0)
            if context_pattern.search(line):
                add(paragraph, 84.0)

            # A useful generic fallback for informational letters with no explicit
            # request verb (e.g. incident reports or explanatory responses).
            if 18 <= len(line) <= SUBJECT_MAX_CHARS and not cls._subject_is_noise(line):
                add(two_line if len(two_line) <= SUBJECT_MAX_CHARS else line, 44.0)

        # Some PDFs expose a whole paragraph as one native-text block. Sentence
        # groups rescue those documents while still preserving several connected
        # sentences when the subject is more than one sentence long.
        flat = re.sub(r"\s+", " ", " ".join(raw_lines)).strip()
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", flat) if part.strip()]
        for index, sentence in enumerate(sentences):
            if len(sentence) < 18:
                continue
            group = " ".join(sentences[index:index + 3])[:SUBJECT_MAX_CHARS].rstrip()
            if request_pattern.search(sentence):
                add(group, 102.0)
            elif action_pattern.search(sentence):
                add(group, 92.0)
            elif context_pattern.search(sentence):
                add(group, 82.0)

        if not candidates:
            return "", []
        candidates.sort(key=lambda item: (item[0], -abs(len(item[1]) - 120)), reverse=True)
        return candidates[0][1], [item[0] for item in candidates[:8]]

    @classmethod
    def _subject_quality_score(cls, value: str) -> float:
        low = cls._label_key(value)
        score = 0.0
        length = len(value)
        if 28 <= length <= 260:
            score += 12.0
        elif 18 <= length <= 700:
            score += 8.0
        elif length <= SUBJECT_MAX_CHARS:
            score += 4.0
        else:
            score -= 12.0

        if any(anchor in low for anchor in SUBJECT_REQUEST_ANCHORS):
            score += 8.0
        if any(re.search(rf"(?i)\b{re.escape(verb)}\w*\b", value) for verb in SUBJECT_ACTION_VERBS):
            score += 10.0
        if any(term in low for term in SUBJECT_OBJECT_TERMS):
            score += 8.0
        if re.search(r"(?i)\b(?:в отношении|по факту|по вопросу|в связи с|для проведения)\b", value):
            score += 6.0
        if re.search(r"https?://|www\.|\S+@\S+", value, re.IGNORECASE):
            score -= 20.0
        return score

    @classmethod
    def _subject_is_noise(cls, value: str) -> bool:
        value = re.sub(r"\s+", " ", (value or "")).strip(" .,:;—–-")
        if len(value) < 12 or sum(ch.isalpha() for ch in value) < 8:
            return True
        low = cls._label_key(value)
        if not low:
            return True
        if re.match(
            r"(?i)^(?:исх\.?|вх\.?|№|дата\b|тел\.?|телефон|исп\.?|"
            r"исполнитель|e-mail|email|адрес\b|кому\b|от кого\b|"
            r"начальник\b|заместитель\b|директор\b|главный врач\b|"
            r"судья\b|мировой судья\b|подпись\b)",
            value,
        ):
            return True
        if cls._looks_like_person_value(value):
            return True
        # Pure letterheads must not become the subject, but organisation names
        # inside a real sentence are allowed.
        if cls._looks_like_organisation(value) and not (
            any(re.search(rf"(?i)\b{re.escape(verb)}\w*\b", value) for verb in SUBJECT_ACTION_VERBS)
            or any(anchor in low for anchor in SUBJECT_CONTEXT_ANCHORS)
        ):
            words = re.findall(r"[А-ЯЁа-яёA-Za-z]+", value)
            if len(words) <= 12:
                return True
        if re.fullmatch(r"[\d\s№./\-–—:()]+", value):
            return True
        return False

    # ================================================================
    # ЗОНИРОВАНИЕ ДОКУМЕНТА
    # ================================================================

    @classmethod
    def _reconstruct_layout_text(cls, blocks: list[dict[str, Any]]) -> str:
        """Rebuild logical OCR lines from block geometry.

        PaddleOCR can return neighbouring fragments as separate blocks.  Joining
        fragments that share a baseline gives the parser much stronger context
        without performing another OCR pass.
        """
        prepared: list[dict[str, Any]] = []
        for block in blocks or []:
            text = re.sub(r"[ \t]+", " ", str(block.get("text", "")).strip())
            if not text:
                continue
            try:
                page = int(block.get("page_index", 0) or 0)
                x1 = float(block.get("x1", 0) or 0)
                x2 = float(block.get("x2", x1) or x1)
                y1 = float(block.get("y1", 0) or 0)
                y2 = float(block.get("y2", y1) or y1)
                center_y = float(block.get("center_y", (y1 + y2) / 2.0) or 0)
                height = max(1.0, float(block.get("height", y2 - y1 or 1.0) or 1.0))
            except (TypeError, ValueError):
                continue
            prepared.append({
                "text": text, "page": page, "x1": x1, "x2": x2,
                "center_y": center_y, "height": height,
            })
        if not prepared:
            return ""

        output: list[str] = []
        pages = sorted({int(item["page"]) for item in prepared})
        for page in pages:
            page_blocks = sorted(
                (item for item in prepared if int(item["page"]) == page),
                key=lambda item: (float(item["center_y"]), float(item["x1"])),
            )
            lines: list[dict[str, Any]] = []
            for block in page_blocks:
                best_line = None
                best_distance = None
                for line in lines:
                    tolerance = max(float(line["height"]), float(block["height"])) * 0.72
                    distance = abs(float(line["center_y"]) - float(block["center_y"]))
                    if distance <= tolerance and (best_distance is None or distance < best_distance):
                        best_line = line
                        best_distance = distance
                if best_line is None:
                    lines.append({
                        "center_y": float(block["center_y"]),
                        "height": float(block["height"]),
                        "blocks": [block],
                    })
                else:
                    best_line["blocks"].append(block)
                    centers = [float(item["center_y"]) for item in best_line["blocks"]]
                    best_line["center_y"] = sum(centers) / len(centers)
                    best_line["height"] = max(float(item["height"]) for item in best_line["blocks"])

            for line in sorted(lines, key=lambda item: float(item["center_y"])):
                line_blocks = sorted(line["blocks"], key=lambda item: float(item["x1"]))
                line_text = " ".join(str(item["text"]) for item in line_blocks)
                line_text = re.sub(r"\s+", " ", line_text).strip()
                if line_text:
                    output.append(line_text)
        return "\n".join(output)

    @classmethod
    def _layout_zones(cls, text: str, blocks: list[dict[str, Any]]) -> dict[str, str]:
        """Build conservative page zones from OCR coordinates.

        Coordinates are used only when a usable geometry is present. Otherwise the
        legacy line-based split remains the fallback, which preserves v1.13 behavior.
        """
        header, body, footer = cls._split_zones(text, blocks)
        fallback_lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        signature_start = max(0, len(fallback_lines) - max(6, int(len(fallback_lines) * 0.45)))
        zones = {
            "header": header,
            "body": body,
            "footer": footer,
            "signature": "\n".join(fallback_lines[signature_start:]),
            "incoming": "",
            "addressee": "",
            "content": body,
        }
        if not blocks:
            return zones

        usable: list[tuple[dict[str, Any], float, float, float, float, float, float]] = []
        for block in blocks:
            try:
                x1 = float(block.get("x1", 0))
                y1 = float(block.get("y1", 0))
                x2 = float(block.get("x2", x1))
                y2 = float(block.get("y2", y1))
                page_w = float(block.get("page_width") or 0)
                page_h = float(block.get("page_height") or 0)
            except (TypeError, ValueError):
                continue
            if page_w <= 0:
                page_w = max((float(b.get("x2", 0) or 0) for b in blocks), default=0.0)
            if page_h <= 0:
                page_h = max((float(b.get("y2", 0) or 0) for b in blocks), default=0.0)
            if page_w <= 0 or page_h <= 0:
                continue
            usable.append((block, x1, y1, x2, y2, page_w, page_h))

        if not usable:
            return zones

        first_page_index = min(int(item[0].get("page_index", 0) or 0) for item in usable)
        last_page_index = max(int(item[0].get("page_index", 0) or 0) for item in usable)

        def collect(
            predicate, *, first_page_only: bool = False, last_page_only: bool = False
        ) -> str:
            selected = []
            for item in usable:
                block, x1, y1, x2, y2, page_w, page_h = item
                block_page = int(block.get("page_index", 0) or 0)
                if first_page_only and block_page != first_page_index:
                    continue
                if last_page_only and block_page != last_page_index:
                    continue
                if predicate(x1, y1, x2, y2, page_w, page_h):
                    selected.append(block)
            selected.sort(
                key=lambda b: (
                    int(b.get("page_index", 0) or 0),
                    float(b.get("y1", 0) or 0),
                    float(b.get("x1", 0) or 0),
                )
            )
            return cls._reconstruct_layout_text(selected)

        # Incoming stamps are normally in the upper-left quadrant. Keep the zone
        # deliberately wider because handwritten date blocks may sit beside "Вх.".
        zones["incoming"] = collect(
            lambda x1, y1, x2, y2, w, h: y1 <= h * 0.38 and x1 <= w * 0.58,
            first_page_only=True,
        )
        # Addressee blocks are typically upper-right. This text is not accepted as
        # to_person by itself; semantic context is still required downstream.
        zones["addressee"] = collect(
            lambda x1, y1, x2, y2, w, h: y1 <= h * 0.48 and x1 >= w * 0.42,
            first_page_only=True,
        )
        # Main semantic content is wider than the historical body zone.  Some
        # official templates start the request just below a large letterhead, at
        # ~20% of page height, and others continue almost to the signature block.
        zones["content"] = collect(
            lambda x1, y1, x2, y2, w, h: h * 0.18 <= y1 <= h * 0.84,
        ) or body
        zones["signature"] = collect(
            lambda x1, y1, x2, y2, w, h: y1 >= h * 0.56,
            last_page_only=True,
        ) or zones["signature"]
        return zones


    @classmethod
    def _locate_field_regions(
        cls,
        result: dict[str, str],
        blocks: list[dict[str, Any]],
        sources: dict[str, str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Map final parser fields back to normalized PDF regions.

        This is deliberately a post-processing step: extraction rules remain
        deterministic, while the UI receives enough provenance to highlight
        the text that contributed to a field. Filename-only fallbacks have no
        PDF region and are therefore omitted.
        """
        output: dict[str, list[dict[str, Any]]] = {}
        if not blocks:
            return output

        prepared = []
        for block in blocks:
            text = str(block.get("text", "")).strip()
            key = cls._label_key(text)
            try:
                x1 = float(block.get("x1", 0.0))
                y1 = float(block.get("y1", 0.0))
                x2 = float(block.get("x2", x1))
                y2 = float(block.get("y2", y1))
                width = float(block.get("page_width", 0.0) or 0.0)
                height = float(block.get("page_height", 0.0) or 0.0)
                page_index = int(block.get("page_index", 0) or 0)
            except (TypeError, ValueError):
                continue
            if not key or width <= 0 or height <= 0:
                continue
            prepared.append((block, key, x1, y1, x2, y2, width, height, page_index))

        for field_name in FIELD_NAMES:
            value = (result.get(field_name) or "").strip()
            if not value or sources.get(field_name) in {"filename", "rejected", "none"}:
                continue
            value_key = cls._label_key(value)
            value_tokens = {t for t in value_key.split() if len(t) >= 3}
            ranked = []
            for item in prepared:
                block, block_key, *_rest = item
                block_tokens = {t for t in block_key.split() if len(t) >= 3}
                if not block_tokens:
                    continue
                if field_name == "received_date":
                    score = 1.0 if value in cls._all_dates(str(block.get("text", ""))) else 0.0
                else:
                    overlap = len(value_tokens & block_tokens) / max(1, min(len(value_tokens), len(block_tokens)))
                    similarity = SequenceMatcher(None, value_key, block_key).ratio()
                    contained = 1.0 if value_key in block_key or block_key in value_key else 0.0
                    score = max(contained, overlap * 0.9, similarity * 0.82)
                threshold = 0.42 if field_name == "request_subject" else 0.62
                if score >= threshold:
                    ranked.append((score, item))

            ranked.sort(key=lambda pair: (-pair[0], pair[1][8], pair[1][3], pair[1][2]))
            max_regions = 10 if field_name == "request_subject" else 3
            selected = []
            seen = set()
            for score, item in ranked[:max_regions * 2]:
                _block, _key, x1, y1, x2, y2, width, height, page_index = item
                rect = (
                    max(0.0, min(1.0, x1 / width)),
                    max(0.0, min(1.0, y1 / height)),
                    max(0.0, min(1.0, x2 / width)),
                    max(0.0, min(1.0, y2 / height)),
                )
                signature = (page_index,) + tuple(round(v, 5) for v in rect)
                if signature in seen:
                    continue
                seen.add(signature)
                selected.append({
                    "page_index": page_index,
                    "rect_norm": [round(v, 6) for v in rect],
                    "score": round(float(score), 3),
                })
                if len(selected) >= max_regions:
                    break
            if selected:
                output[field_name] = selected
        return output

    @classmethod
    def _heuristic_confidence(
        cls,
        field_name: str,
        value: str,
        blocks: list[dict[str, Any]],
        zones: dict[str, str],
    ) -> float:
        if not value:
            return 0.0
        base = {
            "received_date": 0.82,
            "from_department": 0.74,
            "from_position": 0.72,
            "from_person": 0.76,
            "to_person": 0.74,
            "request_subject": 0.72,
        }.get(field_name, 0.65)

        if field_name == "received_date" and value in cls._all_dates(zones.get("incoming", "")):
            base += 0.10
        if field_name == "from_department" and cls._contains_dictionary_term(value, ORGANISATION_TERMS):
            base += 0.07
        if field_name == "from_position" and cls._contains_dictionary_term(value, POSITION_TERMS):
            base += 0.08
        if field_name in {"from_person", "to_person"} and cls._looks_like_person_value(value):
            base += 0.07
        if field_name == "request_subject" and len(value) >= 24:
            base += 0.05

        # OCR confidence nudges the score but never dominates semantic/layout rules.
        block_scores = []
        value_key = cls._label_key(value)
        for block in blocks or []:
            text_key = cls._label_key(str(block.get("text", "")))
            if not text_key or not value_key:
                continue
            if value_key in text_key or text_key in value_key:
                try:
                    score = block.get("confidence")
                    if score is not None:
                        block_scores.append(float(score))
                except (TypeError, ValueError):
                    pass
        if block_scores:
            avg = sum(block_scores) / len(block_scores)
            base += (avg - 0.70) * 0.12
        return max(0.0, min(0.94, base))

    @classmethod
    def _extract_document_number(
        cls, header: str, blocks: list[dict[str, Any]]
    ) -> tuple[str, float]:
        patterns = (
            r"(?i)(?:исх\.?\s*)?№\s*([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9/\-]{1,30})",
            r"(?i)\bN[oо]\.?\s*([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9/\-]{1,30})",
        )
        for pattern in patterns:
            match = re.search(pattern, header or "")
            if match:
                value = match.group(1).strip(" .,:;—–-")
                if value:
                    return value, 0.86
        # A split "№" + value is common in OCR forms. Use spatial proximity only.
        for block in blocks or []:
            if not re.search(r"(?i)(?:^|\s)(?:№|N[oо])(?:\s|$)", str(block.get("text", ""))):
                continue
            try:
                lx2 = float(block.get("x2", 0))
                lcy = float(block.get("center_y", 0))
                lh = max(1.0, float(block.get("height", 1)))
            except (TypeError, ValueError):
                continue
            candidates = []
            for candidate in blocks:
                if candidate is block:
                    continue
                text = str(candidate.get("text", "")).strip()
                if not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9/\-]{1,30}", text):
                    continue
                try:
                    cx1 = float(candidate.get("x1", 0))
                    ccy = float(candidate.get("center_y", 0))
                except (TypeError, ValueError):
                    continue
                if cx1 >= lx2 - 2 and abs(ccy - lcy) <= max(14.0, lh * 1.4):
                    candidates.append((cx1 - lx2 + abs(ccy - lcy), text))
            if candidates:
                return min(candidates)[1], 0.82
        return "", 0.0

    @classmethod
    def _all_dates(cls, text: str) -> set[str]:
        values: set[str] = set()
        for match in re.finditer(r"[0-9OoОIl|]{1,2}[./-][0-9OoОIl|]{1,2}[./-][0-9OoОIl|]{2,4}", text or ""):
            value = cls._normalize_date_value(match.group(0))
            if value:
                values.add(value)
        spaced = cls._normalize_spaced_date(text or "")
        if spaced:
            values.add(spaced)
        return values

    @classmethod
    def _contains_dictionary_term(cls, value: str, terms: tuple[str, ...]) -> bool:
        key = cls._label_key(value)
        return any(cls._label_key(term) in key for term in terms)

    @classmethod
    def _fuzzy_dictionary_term(
        cls, text: str, terms: tuple[str, ...], *, threshold: float = 0.84
    ) -> str:
        words = re.findall(r"[А-ЯЁа-яёA-Za-z]{3,}", text or "")
        if not words:
            return ""
        normalized_words = [cls._label_key(word) for word in words]
        best = (0.0, "")
        for term in terms:
            term_key = cls._label_key(term)
            term_words = term_key.split()
            width = max(1, len(term_words))
            for index in range(0, max(1, len(normalized_words) - width + 1)):
                phrase = " ".join(normalized_words[index:index + width])
                score = SequenceMatcher(None, phrase, term_key).ratio()
                if score > best[0]:
                    best = (score, term)
        return best[1] if best[0] >= threshold else ""

    @staticmethod
    def _split_zones(text: str, blocks: list[dict[str, Any]]) -> tuple[str, str, str]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        total = len(lines)
        if total == 0:
            return "", "", ""

        if blocks:
            try:
                page_indices = [int(b.get("page_index", 0) or 0) for b in blocks]
                first_page = min(page_indices) if page_indices else 0
                last_page = max(page_indices) if page_indices else first_page
            except (TypeError, ValueError):
                first_page = last_page = 0
            first_blocks = [b for b in blocks if int(b.get("page_index", 0) or 0) == first_page]
            last_blocks = [b for b in blocks if int(b.get("page_index", 0) or 0) == last_page]
            max_y = max((float(b.get("y2", 0) or 0) for b in first_blocks), default=0.0)
            last_max_y = max((float(b.get("y2", 0) or 0) for b in last_blocks), default=max_y)
            if max_y > 0:
                header_blocks = [b for b in first_blocks if float(
                    b.get("y1", 0) or 0) < (max_y * 0.35)]
                footer_blocks = [b for b in last_blocks if float(
                    b.get("y1", 0) or 0) > (last_max_y * 0.70)]
                body_blocks = []
                for b in blocks:
                    page_height = float(b.get("page_height", 0) or 0)
                    if page_height <= 0:
                        page_height = max_y
                    y1 = float(b.get("y1", 0) or 0)
                    if (page_height * 0.28) <= y1 <= (page_height * 0.78):
                        body_blocks.append(b)

                sort_key = lambda x: (int(x.get("page_index", 0) or 0), float(x.get("y1", 0) or 0), float(x.get("x1", 0) or 0))
                h_text = "\n".join(str(b.get("text", "")) for b in sorted(header_blocks, key=sort_key))
                b_text = "\n".join(str(b.get("text", "")) for b in sorted(body_blocks, key=sort_key))
                f_text = "\n".join(str(b.get("text", "")) for b in sorted(footer_blocks, key=sort_key))
                return h_text, b_text, f_text

        h_end = max(1, int(total * 0.35))
        f_start = max(h_end, int(total * 0.70))
        return "\n".join(lines[:h_end]), "\n".join(lines[h_end:f_start]), "\n".join(lines[f_start:])

    # ================================================================
    # ИЗВЛЕЧЕНИЕ ПОЛЕЙ ИЗ ТЕКСТА
    # ================================================================

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
        # Prefer explicit registration/incoming-date context over arbitrary dates
        # such as an outgoing reference mentioned in the same header.
        priority_patterns = (
            r"дата\s+(?:поступления|регистрации|входящего)",
            r"(?:вх\.?|входящ\w*)\s*(?:№[^\n]{0,40})?\s*от",
            r"дата\s*[:—–-]",
        )
        for source in (header, full_text):
            lines = [line.strip() for line in (source or "").splitlines() if line.strip()]
            for pattern in priority_patterns:
                for line in lines:
                    if re.search(pattern, line, flags=re.IGNORECASE):
                        value = cls._normalize_date_value(line)
                        if value:
                            return value

        # Generic header date is only a fallback.
        for line in (header or "").splitlines():
            if re.search(r"\b(?:от|исх|вх|дата)\b", line, flags=re.IGNORECASE):
                value = cls._normalize_date_value(line)
                if value:
                    return value
        return cls._normalize_date_value(header) or cls._normalize_date_value(full_text)

    # ================================================================
    # НОРМАЛИЗАЦИЯ ИМЕН
    # ================================================================

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

    # ================================================================
    # ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ
    # ================================================================

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
