from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.document_parser import PARSER_VERSION

REVIEWABLE_FIELDS = (
    "received_date",
    "from_department",
    "from_position",
    "from_person",
    "to_person",
    "request_subject",
)

FIELD_LABELS = {
    "received_date": "Дата поступления",
    "from_department": "Отдел",
    "from_position": "Должность",
    "from_person": "От кого",
    "to_person": "На кого",
    "request_subject": "Суть запроса",
}

REVIEW_DECISIONS = {"review", "suggestion", "low_confidence"}


def _compact_block(block: Any) -> dict[str, Any] | None:
    if not isinstance(block, dict):
        return None
    result: dict[str, Any] = {}
    for key in ("page_index", "rect_norm", "parser_zone", "text", "confidence"):
        value = block.get(key)
        if value is None:
            continue
        if key == "text":
            value = str(value)[:220]
        result[key] = value
    return result or None


def _compact_candidate(candidate: Any) -> dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None
    value = str(candidate.get("value", "") or "").strip()
    if not value:
        return None
    blocks = [item for item in (_compact_block(x) for x in candidate.get("source_blocks", [])[:2]) if item]
    return {
        "value": value,
        "confidence": round(float(candidate.get("confidence", 0.0) or 0.0), 3),
        "source": str(candidate.get("source", "") or ""),
        "zone": str(candidate.get("zone", "") or ""),
        "source_blocks": blocks,
    }


def compact_parser_diagnostics(diagnostics: Any) -> dict[str, Any]:
    """Keep only review/provenance data needed by the UI.

    Full parser diagnostics may contain duplicated NER entities and OCR blocks.
    Persisting only this compact subset keeps the database and runtime memory
    bounded while preserving confidence, alternatives and PDF source regions.
    """
    if not isinstance(diagnostics, dict):
        return {}

    field_results_in = diagnostics.get("field_results") or {}
    field_results: dict[str, dict[str, Any]] = {}
    for field in REVIEWABLE_FIELDS:
        payload = field_results_in.get(field)
        if not isinstance(payload, dict):
            continue
        alternatives = [item for item in (_compact_candidate(x) for x in payload.get("alternatives", [])[:3]) if item]
        blocks = [item for item in (_compact_block(x) for x in payload.get("source_blocks", [])[:3]) if item]
        field_results[field] = {
            "value": str(payload.get("value", "") or ""),
            "confidence": round(float(payload.get("confidence", 0.0) or 0.0), 3),
            "source": str((payload.get("extractors") or [""])[0] or ""),
            "decision": str(payload.get("decision", "empty") or "empty"),
            "margin": round(float(payload.get("margin", 0.0) or 0.0), 3),
            "warnings": [str(x) for x in (payload.get("warnings") or [])[:5]],
            "evidence": [str(x) for x in (payload.get("evidence") or [])[:6]],
            "alternatives": alternatives,
            "source_blocks": blocks,
        }

    regions_in = diagnostics.get("field_regions") or {}
    field_regions: dict[str, list[dict[str, Any]]] = {}
    if isinstance(regions_in, dict):
        for field in REVIEWABLE_FIELDS:
            blocks = [item for item in (_compact_block(x) for x in (regions_in.get(field) or [])[:3]) if item]
            if blocks:
                field_regions[field] = blocks

    result = {
        "format_version": 1,
        "parser_version": PARSER_VERSION,
        "field_results": field_results,
        "field_regions": field_regions,
        "field_confidence": {
            field: round(float(value or 0.0), 3)
            for field, value in (diagnostics.get("field_confidence") or {}).items()
            if field in REVIEWABLE_FIELDS
        },
        "field_sources": {
            field: str(value or "")
            for field, value in (diagnostics.get("field_sources") or {}).items()
            if field in REVIEWABLE_FIELDS
        },
        "field_decisions": {
            field: str(value or "")
            for field, value in (diagnostics.get("field_decisions") or {}).items()
            if field in REVIEWABLE_FIELDS
        },
        "review_fields": [
            str(field) for field in (diagnostics.get("review_fields") or [])
            if field in REVIEWABLE_FIELDS
        ],
        "document_type": str(diagnostics.get("document_type", "") or ""),
    }
    return result


def field_review_info(diagnostics: dict[str, Any] | None, field: str) -> dict[str, Any]:
    data = diagnostics if isinstance(diagnostics, dict) else {}
    payload = (data.get("field_results") or {}).get(field) or {}
    if not isinstance(payload, dict):
        payload = {}
    result = deepcopy(payload)
    result.setdefault("confidence", float((data.get("field_confidence") or {}).get(field, 0.0) or 0.0))
    result.setdefault("decision", str((data.get("field_decisions") or {}).get(field, "empty") or "empty"))
    result.setdefault("alternatives", [])
    result.setdefault("source_blocks", (data.get("field_regions") or {}).get(field, []))
    return result


def needs_review(diagnostics: dict[str, Any] | None, field: str) -> bool:
    return str(field_review_info(diagnostics, field).get("decision", "")) in REVIEW_DECISIONS
