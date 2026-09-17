from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Iterable

from app.models import Request

logger = logging.getLogger(__name__)


CORRECTABLE_FIELDS = ("from_department", "from_position", "from_person", "to_person")


def _key(value: str) -> str:
    value = (value or "").lower().replace("ё", "е")
    # Apply only very safe OCR substitutions inside Cyrillic words. Do not touch
    # standalone digits because they may identify a department/request number.
    value = re.sub(r"(?<=[а-я])0(?=[а-я])", "о", value)
    value = re.sub(r"[^а-яa-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _digits(value: str) -> tuple[str, ...]:
    # Only structural numeric tokens matter (e.g. ОП №8). A digit embedded inside
    # a word is usually an OCR confusion such as кадр0в / Белк0в and should not
    # block correction against the known spelling.
    return tuple(re.findall(r"(?<![А-ЯЁа-яёA-Za-z0-9])(\d+)(?![А-ЯЁа-яёA-Za-z0-9])", value or ""))


def best_known_value(value: str, candidates: Iterable[str], *, threshold: float = 0.96) -> str:
    """Return a known canonical spelling only when the fuzzy match is very safe.

    Digits must match exactly. This prevents dangerous corrections such as ОП №8
    becoming ОП №9 just because the strings are otherwise nearly identical.
    """
    source = (value or "").strip()
    source_key = _key(source)
    if len(source_key) < 4:
        return source

    source_digits = _digits(source)
    best_score = threshold
    best_value = source
    for candidate in candidates:
        candidate = str(candidate or "").strip()
        candidate_key = _key(candidate)
        if not candidate_key or candidate_key == source_key:
            if candidate_key == source_key and candidate:
                return candidate
            continue
        if source_digits != _digits(candidate):
            continue
        # Avoid matching values with very different lengths/structures.
        ratio_len = min(len(source_key), len(candidate_key)) / max(len(source_key), len(candidate_key))
        if ratio_len < 0.72:
            continue
        score = SequenceMatcher(None, source_key, candidate_key).ratio()
        if score > best_score:
            best_score = score
            best_value = candidate
    return best_value


def correct_request_with_known_values(
    request: Request,
    suggestions: dict[str, list[str]] | None,
) -> Request:
    if not suggestions:
        return request
    for field in CORRECTABLE_FIELDS:
        value = getattr(request, field, "")
        if not value:
            continue
        corrected = best_known_value(value, suggestions.get(field, ()))
        if corrected != value:
            logger.debug("Parser canonicalized %s: %r -> %r", field, value, corrected)
        setattr(request, field, corrected)
    return request
