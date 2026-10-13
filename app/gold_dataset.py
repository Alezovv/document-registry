from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from app.parser_core import CandidateResolver, FieldNormalizer


GOLD_FORMAT_VERSION = 1
GOLD_FIELDS = (
    "received_date",
    "from_department",
    "from_position",
    "from_person",
    "to_person",
    "request_subject",
)


class GoldDatasetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GoldDocument:
    sha256: str
    file_name: str
    expected: dict[str, str]
    reviewed: bool


def _tokens(field: str, value: str) -> list[str]:
    key = FieldNormalizer.comparison_key(field, value)
    return [token for token in key.split() if token]


def _person_compatible(left: str, right: str) -> bool:
    a = _tokens("person", left)
    b = _tokens("person", right)
    if not a or not b or a[0] != b[0] or len(a[0]) < 3:
        return False
    if len(a) == 1 or len(b) == 1:
        return True
    tail = min(len(a), len(b), 3)
    return all(a[index][:1] == b[index][:1] for index in range(1, tail))


def _token_containment(field: str, left: str, right: str, *, minimum_shared: int = 2) -> bool:
    a = set(_tokens(field, left))
    b = set(_tokens(field, right))
    if not a or not b:
        return False
    shared = a & b
    coverage = len(shared) / max(1, min(len(a), len(b)))
    return len(shared) >= minimum_shared and coverage >= 0.80


def values_compatible(field: str, left: str, right: str) -> bool:
    a = FieldNormalizer.comparison_key(field, left)
    b = FieldNormalizer.comparison_key(field, right)
    if not a or not b:
        return False
    if a == b:
        return True
    if field == "from_department":
        return CandidateResolver.values_compatible(field, left, right)
    if field in {"from_person", "to_person"}:
        return _person_compatible(left, right)
    if field == "from_position":
        return _token_containment(field, left, right, minimum_shared=2)
    if field == "request_subject":
        return _token_containment(field, left, right, minimum_shared=3)
    return False


def evaluate_field(field: str, predicted: str, expected: str) -> dict[str, bool]:
    expected_key = FieldNormalizer.comparison_key(field, expected)
    predicted_key = FieldNormalizer.comparison_key(field, predicted)
    exact = bool(expected_key) and expected_key == predicted_key
    compatible = exact or values_compatible(field, predicted, expected)
    return {"exact": exact, "compatible": compatible}


def load_gold_dataset(path: str | Path, *, reviewed_only: bool = True) -> list[GoldDocument]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise GoldDatasetError(f"Не удалось прочитать Gold Dataset: {error}") from error
    if not isinstance(payload, dict) or int(payload.get("format_version", 0) or 0) != GOLD_FORMAT_VERSION:
        raise GoldDatasetError("Неподдерживаемый format_version Gold Dataset.")
    raw_documents = payload.get("documents")
    if not isinstance(raw_documents, list):
        raise GoldDatasetError("Gold Dataset должен содержать массив documents.")

    documents: list[GoldDocument] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_documents, start=1):
        if not isinstance(item, dict):
            raise GoldDatasetError(f"documents[{index}] должен быть объектом.")
        sha256 = str(item.get("sha256", "") or "").strip().lower()
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise GoldDatasetError(f"documents[{index}].sha256 некорректен.")
        if sha256 in seen:
            raise GoldDatasetError(f"SHA-256 {sha256[:12]}… встречается в Gold Dataset дважды.")
        seen.add(sha256)
        reviewed = bool(item.get("reviewed", False))
        if reviewed_only and not reviewed:
            continue
        expected_raw = item.get("expected")
        if not isinstance(expected_raw, dict):
            raise GoldDatasetError(f"documents[{index}].expected должен быть объектом.")
        expected = {
            field: str(expected_raw.get(field, "") or "").strip()
            for field in GOLD_FIELDS
        }
        if reviewed and not any(expected.values()):
            raise GoldDatasetError(f"documents[{index}] отмечен reviewed, но все expected-поля пусты.")
        documents.append(
            GoldDocument(
                sha256=sha256,
                file_name=str(item.get("file_name", "") or ""),
                expected=expected,
                reviewed=reviewed,
            )
        )
    return documents


def evaluate_request(request: Any, document: GoldDocument) -> dict[str, dict[str, bool]]:
    result: dict[str, dict[str, bool]] = {}
    for field in GOLD_FIELDS:
        expected = document.expected.get(field, "").strip()
        if not expected:
            continue
        predicted = str(getattr(request, field, "") or "").strip()
        result[field] = evaluate_field(field, predicted, expected)
    return result
