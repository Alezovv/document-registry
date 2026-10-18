from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

from app.app_paths import get_app_paths

logger = logging.getLogger(__name__)

# Pre-seeded canonical fingerprints for core recurring departments
DEFAULT_FINGERPRINTS: list[dict[str, Any]] = [
    {
        "department": "ГОСУДАРСТВЕННОЕ УЧРЕЖДЕНИЕ ЗДРАВООХРАНЕНИЯ «КЛИНИЧЕСКАЯ БОЛЬНИЦА СКОРОЙ МЕДИЦИНСКОЙ ПОМОЩИ № 15»",
        "ogrn": ["1023404361672"],
        "inn": ["3448012780"],
        "phones": ["671153", "697188", "8442671153", "8442697188"],
        "keywords": ["кбсмп 15", "кбсмп №15", "кбсмп № 15", "больница скорой медицинской помощи 15", "андижанская"],
    },
    {
        "department": "КОМИТЕТ ЗДРАВООХРАНЕНИЯ ВОЛГОГРАДСКОЙ ОБЛАСТИ",
        "ogrn": ["1023403441703"],
        "inn": ["3444054213"],
        "keywords": ["комитет здравоохранения волгоградской области"],
    },
    {
        "department": "Отдел полиции № 8 УМВД России по г. Волгограду",
        "phones": ["624002", "8442624002"],
        "keywords": ["оп 8 умвд", "оп №8 умвд", "оп № 8 умвд", "отдел полиции 8", "отдел полиции №8", "отдел полиции № 8", "панферова 40", "панферова д 40"],
    },
    {
        "department": "ОБДПС ГИБДД УМВД России по г. Волгограду",
        "keywords": ["обдпс гибдд", "ардатовская 36", "ардатовская д 36"],
    },
    {
        "department": "Красноармейский районный суд г. Волгограда",
        "keywords": ["красноармейский районный суд", "50 лет октября 4"],
    },
    {
        "department": "ОМВД России по Светлоярскому району",
        "keywords": ["омвд россии по светлоярскому", "омвд по светлоярскому", "светлоярскому району"],
    },
    {
        "department": "ОМВД России по Калачевскому району",
        "keywords": ["омвд россии по калачевскому", "омвд по калачевскому", "калачевскому району"],
    },
    {
        "department": "ОМВД России по Октябрьскому району",
        "keywords": ["омвд россии по октябрьскому", "омвд по октябрьскому", "октябрьскому району"],
    },
    {
        "department": "ОМВД России по Котовскому району",
        "keywords": ["омвд россии по котовскому", "омвд по котовскому", "котовскому району"],
    },
    {
        "department": "ОМВД России «Черноярский»",
        "keywords": ["омвд россии черноярский", "омвд черноярский"],
    },
    {
        "department": "ФКУ УИИ УФСИН России по Волгоградской области",
        "keywords": ["фку уии уфсин", "уии уфсин по волгоградской"],
    },
    {
        "department": "ФКУ ИК-26 УФСИН России по Волгоградской области",
        "keywords": ["фку ик 26", "фку ик-26", "ик 26 уфсин"],
    },
    {
        "department": "ФКУ ИЦ-2 УФСИН России по Волгоградской области",
        "keywords": ["фку иц 2", "фку иц-2", "иц 2 уфсин"],
    },
    {
        "department": "МОУ Детский сад № 285",
        "keywords": ["детский сад 285", "детский сад №285", "детский сад № 285", "моу детский сад № 285", "моу детский сад 285"],
    },
]


def _normalize_header_key(text: str) -> str:
    cleaned = (text or "").lower().replace("ё", "е")
    cleaned = re.sub(r"[^а-яa-z0-9]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


_OCR_DIGIT_MAP = str.maketrans({
    "O": "0", "o": "0", "О": "0", "о": "0",
    "I": "1", "l": "1", "|": "1", "!": "1", "i": "1",
    "З": "3", "з": "3",
    "S": "5", "s": "5",
    "Б": "6", "б": "6",
    "В": "8",
})

_LETTERHEAD_CONTEXT_RE = re.compile(
    r"(?i)\b(?:гуз|гбуз|гауз|мбу|мвд|умвд|омвд|гибдд|обдпс|оп\s*№?\s*\d+|"
    r"отдел\s+полиции|полици\w+|комитет|управлени\w+|департамент\w+|министерств\w+|"
    r"суд|прокуратур\w+|фсин|уфсин|фку|фссп|администраци\w+|больниц\w+|поликлиник\w+|"
    r"россии|волгоград\w*|огрн|инн|кпп|ул\.|улица|пр-т|проспект)\b"
)

_PROSE_BODY_MARKER_RE = re.compile(
    r"(?i)\b(?:письмо\s+по\s+делу|просим\s+направить|прошу\s+направить|"
    r"просим\s+предоставить|прошу\s+предоставить|уважаем\w+|сообщаем)\b"
)


def _hamming_le_one(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    diffs = 0
    for ch1, ch2 in zip(a, b):
        if ch1 != ch2:
            diffs += 1
            if diffs > 1:
                return False
    return True


def _extract_header_only_slice(text: str) -> str:
    lines: list[str] = []
    for raw_line in (text or "").splitlines()[:10]:
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r"(?i)^(?:уважаем\w+|прошу\b|просим\b|в\s+производстве\b|сообщаем\b)", line):
            break
        lines.append(line)
        if sum(len(x) for x in lines) >= 650:
            break
    return "\n".join(lines)


def _extract_digits_pattern(text: str, pattern: str) -> list[str]:
    matches = re.findall(pattern, text, flags=re.IGNORECASE)
    results: list[str] = []
    for m in matches:
        translated = str(m).translate(_OCR_DIGIT_MAP)
        val = re.sub(r"\D", "", translated)
        if val and val not in results:
            results.append(val)
    return results


def _extract_phones(text: str) -> list[str]:
    pattern = re.compile(
        r"(?:тел(?:ефон)?\.?|факс|т/ф|т\.)\s*[:\-]?\s*[^\d\n]{0,10}(?:(?:\+?7|8)[\s-]?)?(?:\(?\d{3,4}\)?[\s-]?)?(\d{2,3}[-\s]?\d{2}[-\s]?\d{2})\b",
        re.IGNORECASE,
    )
    found = pattern.findall(text)
    phones: list[str] = []
    for match in found:
        digits = re.sub(r"\D", "", match)
        if len(digits) in (6, 7) and digits not in phones:
            phones.append(digits)
    return phones


class BlankFingerprintMemory:
    """Memory of official letterhead requisites (OGRN, INN, phones, distinctive keywords).
    
    Provides deterministic 100% resolution of recurring departments even from
    degraded or poorly formatted OCR scans.
    """

    VERSION = 1

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else get_app_paths().data / "blank_fingerprints.json"
        self._lock = threading.RLock()
        self._entries: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return [dict(e) for e in DEFAULT_FINGERPRINTS]
        try:
            content = self.path.read_text(encoding="utf-8")
            data = json.loads(content)
            if isinstance(data, list):
                # Clean up any legacy overly broad keywords from previous versions
                for item in data:
                    if isinstance(item, dict) and "keywords" in item:
                        item["keywords"] = [
                            kw for kw in item["keywords"]
                            if kw.strip().lower() != "моу детский сад"
                        ]
                # Ensure all default fingerprints are present
                existing_depts = {item.get("department") for item in data if isinstance(item, dict)}
                merged = list(data)
                for default_entry in DEFAULT_FINGERPRINTS:
                    if default_entry["department"] not in existing_depts:
                        merged.append(dict(default_entry))
                return merged
        except Exception as error:
            logger.warning("Не удалось прочитать blank_fingerprints.json: %s", error)
        return [dict(e) for e in DEFAULT_FINGERPRINTS]

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._entries, ensure_ascii=False, indent=2)
            fd, tmp_name = tempfile.mkstemp(
                prefix="blank_fingerprints_",
                suffix=".tmp",
                dir=self.path.parent,
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except Exception as error:
            logger.warning("Не удалось сохранить blank_fingerprints.json: %s", error)

    def find_department(self, header_text: str) -> str | None:
        """Match header against known requisites and letterhead fingerprints."""
        if not header_text:
            return None

        header_slice = _extract_header_only_slice(header_text)
        if not header_slice:
            return None
        norm = _normalize_header_key(header_slice)
        if not norm:
            return None

        digit_like = r"[0-9OoОоIl|!iЗзSsБбВ]"
        ogrns = _extract_digits_pattern(header_slice, rf"\b(?:ОГРН|OGRN)\s*[:№]?\s*({digit_like}{{13}})\b")
        inns = _extract_digits_pattern(header_slice, rf"\b(?:ИНН|INN)(?:/[А-ЯA-Z]+)?\s*[:№]?\s*({digit_like}{{10}})\b")
        phones = _extract_phones(header_slice)
        has_letterhead_context = bool(_LETTERHEAD_CONTEXT_RE.search(header_slice)) and not bool(
            len(header_slice.splitlines()) == 1 and _PROSE_BODY_MARKER_RE.search(header_slice)
        )
        is_receiving_hospital_addressee = bool(
            re.search(r"(?i)\b(?:главн\w+\s+врач\w*|в\s+адрес|руководител\w+|директор\w+)\b", header_slice)
        )

        def _is_self_hospital(dept_name: str) -> bool:
            return "15" in dept_name and ("больница" in dept_name.lower() or "кбсмп" in dept_name.lower())

        with self._lock:
            for entry in self._entries:
                dept_name = str(entry["department"])
                if _is_self_hospital(dept_name) and is_receiving_hospital_addressee and not ogrns:
                    continue
                for known_ogrn in entry.get("ogrn", []):
                    if known_ogrn in ogrns or known_ogrn in norm:
                        return dept_name
                    if any(_hamming_le_one(known_ogrn, cand) for cand in ogrns):
                        return dept_name

            for entry in self._entries:
                dept_name = str(entry["department"])
                if _is_self_hospital(dept_name) and is_receiving_hospital_addressee and not inns:
                    continue
                for known_inn in entry.get("inn", []):
                    if known_inn in inns or known_inn in norm:
                        return dept_name

            if phones and has_letterhead_context:
                for entry in self._entries:
                    dept_name = str(entry["department"])
                    if _is_self_hospital(dept_name):
                        continue
                    for known_phone in entry.get("phones", []):
                        kp_digits = re.sub(r"\D", "", known_phone)
                        if any(kp_digits.endswith(p) or p.endswith(kp_digits) for p in phones):
                            return dept_name

            for entry in self._entries:
                dept_name = str(entry["department"])
                if _is_self_hospital(dept_name):
                    continue
                for kw in entry.get("keywords", []):
                    kw_norm = _normalize_header_key(kw)
                    if kw_norm and kw_norm in norm:
                        return dept_name

        return None

    def learn_from_header(self, header_text: str, confirmed_department: str) -> bool:
        """Register requisites from a confirmed document header."""
        confirmed_department = (confirmed_department or "").strip()
        if not confirmed_department or len(confirmed_department) < 4:
            return False

        header_slice = _extract_header_only_slice(header_text)
        digit_like = r"[0-9OoОоIl|!iЗзSsБбВ]"
        ogrns = _extract_digits_pattern(header_slice, rf"\b(?:ОГРН|OGRN)\s*[:№]?\s*({digit_like}{{13}})\b")
        inns = _extract_digits_pattern(header_slice, rf"\b(?:ИНН|INN)(?:/[А-ЯA-Z]+)?\s*[:№]?\s*({digit_like}{{10}})\b")
        phones: list[str] = []

        if not ogrns and not inns and not phones:
            return False

        with self._lock:
            matched_entry = None
            for entry in self._entries:
                if entry.get("department", "").lower() == confirmed_department.lower():
                    matched_entry = entry
                    break

            if matched_entry is None:
                matched_entry = {
                    "department": confirmed_department,
                    "ogrn": [],
                    "inn": [],
                    "phones": [],
                    "keywords": [],
                }
                self._entries.append(matched_entry)

            changed = False
            for o in ogrns:
                if o not in matched_entry["ogrn"]:
                    matched_entry["ogrn"].append(o)
                    changed = True
            for i in inns:
                if i not in matched_entry["inn"]:
                    matched_entry["inn"].append(i)
                    changed = True
            for p in phones:
                if p not in matched_entry["phones"]:
                    matched_entry["phones"].append(p)
                    changed = True

            if changed:
                self._save()
                return True

        return False


_GLOBAL_FINGERPRINT_MEMORY: BlankFingerprintMemory | None = None
_GLOBAL_LOCK = threading.RLock()


def get_blank_fingerprint_memory() -> BlankFingerprintMemory:
    global _GLOBAL_FINGERPRINT_MEMORY
    with _GLOBAL_LOCK:
        if _GLOBAL_FINGERPRINT_MEMORY is None:
            _GLOBAL_FINGERPRINT_MEMORY = BlankFingerprintMemory()
        return _GLOBAL_FINGERPRINT_MEMORY
