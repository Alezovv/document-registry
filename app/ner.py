from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable

from app.document_parser_parts.constants import TARGET_STOP_WORDS
from app.parser_core import (
    EntityGroup,
    FieldCandidate,
    FieldNormalizer,
    LayoutAnalyzer,
    PageZone,
    RoleAssignment,
    RoleType,
)


class NerLabel(str, Enum):
    PERSON = "PERSON"
    ORGANIZATION = "ORGANIZATION"
    LOCATION = "LOCATION"


@dataclass(slots=True)
class NerEntity:
    text: str
    label: str
    confidence: float
    zone: str = PageZone.UNKNOWN.value
    page_index: int | None = None
    source_blocks: list[dict[str, Any]] = field(default_factory=list)
    engine: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.text = re.sub(r"\s+", " ", str(self.text or "")).strip(" \t\r\n,;:")
        self.confidence = max(0.0, min(1.0, float(self.confidence or 0.0)))


class NerEngine(ABC):
    """Stable local NER contract.

    Engines return neutral entities only. They never write directly to Request
    fields. Layout-aware adapters below convert entities into FieldCandidate and
    semantic roles, which keeps future ONNX/transformer backends replaceable.
    """

    name = "abstract"

    @abstractmethod
    def extract(self, text: str, blocks: Iterable[dict[str, Any]] = ()) -> list[NerEntity]:
        raise NotImplementedError


class HeuristicRussianNerEngine(NerEngine):
    """Dependency-free Russian NER baseline for portable/offline builds.

    v1.20 deliberately starts with a conservative baseline so the NER plumbing
    can be benchmarked on real documents before adding a large model. It detects
    only high-precision person/organisation/location shapes and preserves the
    exact OCR span. A future ONNX engine can implement NerEngine without changing
    DocumentParser, cache or RoleResolver contracts.
    """

    name = "heuristic-ru-v1"

    _PERSON_INITIALS_RE = re.compile(
        r"(?<![А-ЯЁа-яё-])"
        r"[А-ЯЁ][а-яё-]{1,}(?:-[А-ЯЁ][а-яё-]{1,})?\s+"
        r"[А-ЯЁ]\.\s*[А-ЯЁ]\."
        r"(?![А-ЯЁа-яё-])"
    )
    # Some official letters print initials before the surname: «И. О. Иванов».
    _PERSON_INITIALS_FIRST_RE = re.compile(
        r"(?<![А-ЯЁа-яё-])"
        r"[А-ЯЁ]\.\s*[А-ЯЁ]\.\s*"
        r"[А-ЯЁ][а-яё-]{1,}(?:-[А-ЯЁ][а-яё-]{1,})?"
        r"(?![А-ЯЁа-яё-])"
    )
    _PERSON_FULL_RE = re.compile(
        r"(?<![А-ЯЁа-яё-])"
        r"[А-ЯЁ][а-яё-]{1,}(?:-[А-ЯЁ][а-яё-]{1,})?\s+"
        r"[А-ЯЁ][а-яё-]{1,}(?:-[А-ЯЁ][а-яё-]{1,})?\s+"
        r"[А-ЯЁ][а-яё-]{1,}(?:-[А-ЯЁ][а-яё-]{1,})?"
        r"(?![А-ЯЁа-яё-])"
    )
    _ORG_MARKER_RE = re.compile(
        r"(?i)(?:\b(?:ГУ|ГБУ|ГБУЗ|ГАУ|ГАУЗ|МБУ|МВД|УМВД|ОМВД|ГИБДД|ОБДПС|"
        r"МЧС|ФСБ|ФССП|ФНС|ПФР|СФР|ЦРБ|ГКБ|КБСМП)\b|"
        r"\b(?:министерств|департамент|комитет|управлени|отдел|администраци|"
        r"прокуратур|суд|больниц|поликлиник|учреждени|организаци|институт|"
        r"университет|служб|центр)\w*)"
    )
    _LOCATION_PATTERNS = (
        re.compile(r"(?i)(?<!\w)(?:г\.|город)\s+([А-ЯЁ][А-ЯЁа-яё-]{2,}(?:\s+[А-ЯЁ][А-ЯЁа-яё-]{2,}){0,2})"),
        re.compile(r"(?i)(?<!\w)([А-ЯЁ][а-яё-]{2,}(?:\s+[А-ЯЁ][а-яё-]{2,})?)\s+(?:область|край|республика|район)\b"),
    )
    _BAD_FULLNAME_PREFIXES = {
        "главный врач", "заместитель начальника", "начальник отдела",
        "старший инспектор", "ведущий специалист", "главный специалист",
    }

    @staticmethod
    def _ocr_confidence(block: dict[str, Any]) -> float:
        try:
            value = float(block.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if value > 1.0:
            value /= 100.0
        return max(0.0, min(1.0, value))

    @classmethod
    def _confidence(cls, base: float, block: dict[str, Any]) -> float:
        ocr = cls._ocr_confidence(block)
        if ocr <= 0.0:
            return base
        return max(0.0, min(0.98, base * 0.72 + ocr * 0.28))

    @staticmethod
    def _page(block: dict[str, Any]) -> int | None:
        raw = block.get("page_index")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _person_entities(cls, line: str, block: dict[str, Any], zone: str) -> list[NerEntity]:
        output: list[NerEntity] = []
        seen: set[tuple[int, int]] = set()
        for pattern, base in (
            (cls._PERSON_INITIALS_RE, 0.86),
            (cls._PERSON_INITIALS_FIRST_RE, 0.86),
            (cls._PERSON_FULL_RE, 0.82),
        ):
            for match in pattern.finditer(line):
                if any(start <= match.start() and match.end() <= end for start, end in seen):
                    continue
                value = match.group(0).strip()
                if value.lower() in cls._BAD_FULLNAME_PREFIXES:
                    continue
                seen.add(match.span())
                confidence = cls._confidence(base, block)
                if zone in {PageZone.ADDRESSEE.value, PageZone.SIGNATURE.value, PageZone.EXECUTOR_FOOTER.value}:
                    confidence = min(0.97, confidence + 0.04)
                output.append(NerEntity(
                    text=value,
                    label=NerLabel.PERSON.value,
                    confidence=confidence,
                    zone=zone,
                    page_index=cls._page(block),
                    source_blocks=[dict(block)],
                    engine=cls.name,
                    metadata={"span": [match.start(), match.end()]},
                ))
        return output

    @classmethod
    def _organization_entities(cls, line: str, block: dict[str, Any], zone: str) -> list[NerEntity]:
        if not cls._ORG_MARKER_RE.search(line):
            return []
        value = re.sub(r"\s+", " ", line).strip(" ,:;—–-")
        # Avoid swallowing a trailing labelled person/signature into the org span.
        value = re.split(r"(?i)\s+(?:кому|адресат|исполнитель|тел\.?|телефон)\s*[:—–-]", value, maxsplit=1)[0]
        value = cls._PERSON_INITIALS_RE.sub("", value).strip(" ,:;—–-")
        value = cls._PERSON_INITIALS_FIRST_RE.sub("", value).strip(" ,:;—–-")
        value = cls._PERSON_FULL_RE.sub("", value).strip(" ,:;—–-")
        if len(value) < 4 or len(value) > 180:
            return []
        confidence = cls._confidence(0.82, block)
        if zone == PageZone.HEADER.value:
            confidence = min(0.96, confidence + 0.06)
        return [NerEntity(
            text=value,
            label=NerLabel.ORGANIZATION.value,
            confidence=confidence,
            zone=zone,
            page_index=cls._page(block),
            source_blocks=[dict(block)],
            engine=cls.name,
        )]

    @classmethod
    def _location_entities(cls, line: str, block: dict[str, Any], zone: str) -> list[NerEntity]:
        output: list[NerEntity] = []
        for pattern in cls._LOCATION_PATTERNS:
            for match in pattern.finditer(line):
                value = match.group(0).strip(" ,:;—–-")
                output.append(NerEntity(
                    text=value,
                    label=NerLabel.LOCATION.value,
                    confidence=cls._confidence(0.80, block),
                    zone=zone,
                    page_index=cls._page(block),
                    source_blocks=[dict(block)],
                    engine=cls.name,
                    metadata={"span": [match.start(), match.end()]},
                ))
        return output

    @classmethod
    def _dedupe(cls, entities: Iterable[NerEntity]) -> list[NerEntity]:
        best: dict[tuple[str, str, int | None, str], NerEntity] = {}
        for entity in entities:
            key = (
                entity.label,
                re.sub(r"\s+", " ", entity.text.lower().replace("ё", "е")).strip(),
                entity.page_index,
                entity.zone,
            )
            previous = best.get(key)
            if previous is None or entity.confidence > previous.confidence:
                best[key] = entity
        return sorted(best.values(), key=lambda item: item.confidence, reverse=True)

    def extract(self, text: str, blocks: Iterable[dict[str, Any]] = ()) -> list[NerEntity]:
        annotated = LayoutAnalyzer.annotate_blocks(blocks)
        if not annotated:
            annotated = [
                {"text": line, "parser_zone": PageZone.UNKNOWN.value, "block_index": index}
                for index, line in enumerate(str(text or "").splitlines())
                if line.strip()
            ]
        entities: list[NerEntity] = []
        for block in annotated:
            line = str(block.get("text", "") or "").strip()
            if not line:
                continue
            zone = str(block.get("parser_zone", PageZone.UNKNOWN.value) or PageZone.UNKNOWN.value)
            entities.extend(self._person_entities(line, block, zone))
            entities.extend(self._organization_entities(line, block, zone))
            entities.extend(self._location_entities(line, block, zone))
        return self._dedupe(entities)


class NerCandidateAdapter:
    """Map neutral NER entities into existing parser candidates conservatively."""

    @staticmethod
    def _with_bonus(entity: NerEntity, bonus: float) -> float:
        return round(min(0.97, entity.confidence + bonus), 3)

    @classmethod
    def to_field_candidates(cls, entities: Iterable[NerEntity]) -> dict[str, list[FieldCandidate]]:
        output: dict[str, list[FieldCandidate]] = {
            "from_department": [],
            "from_person": [],
            "to_person": [],
        }
        for entity in entities:
            if entity.label == NerLabel.ORGANIZATION.value and entity.zone in {
                PageZone.HEADER.value,
                PageZone.INCOMING.value,
            }:
                output["from_department"].append(FieldCandidate(
                    field="from_department",
                    value=entity.text,
                    confidence=cls._with_bonus(entity, 0.02),
                    source="ner",
                    zone=entity.zone,
                    page_index=entity.page_index,
                    source_blocks=list(entity.source_blocks),
                    extractors=["ner"] + ([entity.engine] if entity.engine and entity.engine != "ner" else []),
                    metadata={"ner_label": entity.label, "ner_engine": entity.engine},
                ))
            elif entity.label == NerLabel.PERSON.value:
                words = [w.lower().strip(" .,:;—–-") for w in entity.text.split()]
                if any(w in TARGET_STOP_WORDS or "калмыков" in w for w in words):
                    continue
                if entity.zone == PageZone.ADDRESSEE.value:
                    output["to_person"].append(FieldCandidate(
                        field="to_person",
                        value=entity.text,
                        confidence=cls._with_bonus(entity, 0.05),
                        source="ner",
                        zone=entity.zone,
                        page_index=entity.page_index,
                        source_blocks=list(entity.source_blocks),
                        extractors=["ner"] + ([entity.engine] if entity.engine and entity.engine != "ner" else []),
                        metadata={"ner_label": entity.label, "ner_engine": entity.engine},
                    ))
                elif entity.zone == PageZone.SIGNATURE.value:
                    output["from_person"].append(FieldCandidate(
                        field="from_person",
                        value=entity.text,
                        confidence=cls._with_bonus(entity, 0.05),
                        source="ner",
                        zone=entity.zone,
                        page_index=entity.page_index,
                        source_blocks=list(entity.source_blocks),
                        extractors=["ner"] + ([entity.engine] if entity.engine and entity.engine != "ner" else []),
                        metadata={"ner_label": entity.label, "ner_engine": entity.engine},
                    ))
        return output

    @staticmethod
    def entity_counts(entities: Iterable[NerEntity]) -> dict[str, int]:
        return dict(Counter(entity.label for entity in entities))


class NerRoleAdapter:
    """Convert person entities into semantic roles without touching form fields."""

    @staticmethod
    def _marker_text(entity: NerEntity) -> str:
        return " ".join(str(block.get("text", "") or "") for block in entity.source_blocks).lower().replace("ё", "е")

    @classmethod
    def resolve(cls, entities: Iterable[NerEntity], *, document_type: str = "other") -> tuple[list[RoleAssignment], list[EntityGroup]]:
        assignments: list[RoleAssignment] = []
        groups: list[EntityGroup] = []
        for entity in entities:
            if entity.label != NerLabel.PERSON.value:
                continue
            role = RoleType.MENTIONED_PERSON.value
            confidence = max(0.50, entity.confidence - 0.08)
            marker_text = cls._marker_text(entity)
            evidence = ["ner", f"zone:{entity.zone}", f"document_type:{document_type}"]
            if entity.zone == PageZone.ADDRESSEE.value:
                role = RoleType.RECIPIENT.value
                confidence = min(0.96, entity.confidence + 0.04)
            elif entity.zone == PageZone.SIGNATURE.value:
                role = RoleType.SIGNER.value
                confidence = min(0.96, entity.confidence + 0.04)
            elif entity.zone == PageZone.EXECUTOR_FOOTER.value and re.search(r"\bисп(?:олнитель|олн|\.)", marker_text):
                role = RoleType.EXECUTOR.value
                confidence = min(0.97, entity.confidence + 0.05)
            elif entity.zone == PageZone.BODY.value:
                role = RoleType.MENTIONED_PERSON.value

            assignments.append(RoleAssignment(
                role=role,
                value=entity.text,
                confidence=round(confidence, 3),
                zone=entity.zone,
                source_blocks=list(entity.source_blocks),
                evidence=evidence,
            ))
            groups.append(EntityGroup(
                role=role,
                person=entity.text,
                confidence=round(confidence, 3),
                zone=entity.zone,
                source_blocks=list(entity.source_blocks),
                evidence=["ner-entity"],
            ))
        return assignments, groups

    @staticmethod
    def merge_assignments(base: Iterable[RoleAssignment], extra: Iterable[RoleAssignment]) -> list[RoleAssignment]:
        output = list(base)
        seen = {
            (item.role, FieldNormalizer.comparison_key("person", item.value))
            for item in output if item.value
        }
        for item in extra:
            key = (item.role, FieldNormalizer.comparison_key("person", item.value))
            if item.value and key not in seen:
                seen.add(key)
                output.append(item)
        return output

    @staticmethod
    def merge_groups(base: Iterable[EntityGroup], extra: Iterable[EntityGroup]) -> list[EntityGroup]:
        output = list(base)
        seen = {
            (item.role, FieldNormalizer.comparison_key("person", item.person))
            for item in output if item.person
        }
        for item in extra:
            key = (item.role, FieldNormalizer.comparison_key("person", item.person))
            if item.person and key not in seen:
                seen.add(key)
                output.append(item)
        return output



def _normalized_entity_text(value: str) -> str:
    value = str(value or "").lower().replace("ё", "е")
    value = re.sub(r"[^0-9a-zа-я]+", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip()


def entities_compatible(left: NerEntity, right: NerEntity) -> bool:
    """Return a tolerant same-label match for calibration/support.

    Exact span equality is too strict for OCR documents: a transformer may emit
    ``Иванов Иван Иванович`` while deterministic rules emit ``Иванов И.И.``;
    organization spans may differ by a legal-form prefix.  Compatibility is
    deliberately conservative and is used only as supporting evidence.
    """
    if left.label != right.label:
        return False
    a = _normalized_entity_text(left.text)
    b = _normalized_entity_text(right.text)
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) >= 5 and (a in b or b in a):
        return True
    a_tokens = a.split()
    b_tokens = b.split()
    if left.label == NerLabel.PERSON.value:
        return bool(
            len(a_tokens) >= 2 and len(b_tokens) >= 2
            and len(a_tokens[0]) >= 4 and a_tokens[0] == b_tokens[0]
        )
    shared = set(a_tokens) & set(b_tokens)
    union = set(a_tokens) | set(b_tokens)
    return bool(union and len(shared) >= 2 and len(shared) / len(union) >= 0.5)


def select_trusted_ml_entities(
    primary: Iterable[NerEntity],
    baseline: Iterable[NerEntity],
    *,
    allow_standalone: bool = False,
) -> list[NerEntity]:
    """Keep only ML entities safe enough to influence registry fields.

    By default v1.20.6 runs the news-domain ONNX model in role-aware *confirmation-only*
    mode: an ML span may affect registry fields only when the deterministic
    archive-oriented baseline independently supports a compatible entity.

    ``allow_standalone=True`` keeps the earlier experimental behaviour where a
    very-high-confidence entity in a role-bearing page zone may also be trusted.
    Raw ML output is always retained in diagnostics, so we can keep measuring the
    model without letting domain-shift noise degrade normal parsing.
    """
    baseline_items = list(baseline)
    trusted: list[NerEntity] = []

    def production_support(entity: NerEntity, item: NerEntity) -> bool:
        if not entities_compatible(entity, item):
            return False
        if entity.label == NerLabel.LOCATION.value:
            # LOCATION is useful for diagnostics, but no registry field consumes
            # it yet.  Keeping it out of the production hybrid set avoids
            # amplifying a noisy news-domain label for no user-visible benefit.
            return False
        if entity.label == NerLabel.PERSON.value:
            role_zones = {
                PageZone.ADDRESSEE.value,
                PageZone.SIGNATURE.value,
                PageZone.EXECUTOR_FOOTER.value,
            }
            return (
                entity.confidence >= 0.75
                and entity.zone in role_zones
                and item.zone == entity.zone
            )
        if entity.label == NerLabel.ORGANIZATION.value:
            sender_zones = {PageZone.HEADER.value, PageZone.INCOMING.value}
            return (
                entity.confidence >= 0.78
                and entity.zone in sender_zones
                and item.zone in sender_zones
            )
        return False

    for entity in primary:
        supported = any(production_support(entity, item) for item in baseline_items)
        reason = "baseline_support_same_role_zone" if supported else ""
        if not supported and allow_standalone:
            if entity.label == NerLabel.PERSON.value:
                supported = entity.confidence >= 0.93 and entity.zone in {
                    PageZone.ADDRESSEE.value,
                    PageZone.SIGNATURE.value,
                    PageZone.EXECUTOR_FOOTER.value,
                }
                reason = "high_confidence_role_zone" if supported else ""
            elif entity.label == NerLabel.ORGANIZATION.value:
                supported = entity.confidence >= 0.94 and entity.zone in {
                    PageZone.HEADER.value,
                    PageZone.INCOMING.value,
                }
                reason = "high_confidence_header_zone" if supported else ""
            elif entity.label == NerLabel.LOCATION.value:
                supported = entity.confidence >= 0.90
                reason = "high_confidence_location" if supported else ""
        if not supported:
            continue
        entity.metadata = dict(entity.metadata)
        entity.metadata["trusted_for_hybrid"] = True
        entity.metadata["trust_reason"] = reason
        entity.metadata["trust_policy"] = (
            "experimental-standalone" if allow_standalone else "confirmation-only"
        )
        trusted.append(entity)
    return trusted

def _entity_signature(entity: NerEntity) -> tuple[str, str]:
    value = re.sub(r"\s+", " ", str(entity.text or "").lower().replace("ё", "е")).strip()
    return entity.label, value


def compare_entity_sets(
    primary: Iterable[NerEntity],
    baseline: Iterable[NerEntity],
    *,
    primary_name: str = "primary",
    baseline_name: str = "heuristic-ru-v1",
) -> dict[str, Any]:
    """Return compact, serialisable disagreement diagnostics for two engines.

    This is intentionally *not* presented as an accuracy metric: neither engine
    is ground truth.  It is a calibration signal that shows where ML and the
    deterministic baseline agree or diverge on the same OCR text.
    """

    primary_items = list(primary)
    baseline_items = list(baseline)
    primary_map = {_entity_signature(item): item for item in primary_items if item.text}
    baseline_map = {_entity_signature(item): item for item in baseline_items if item.text}
    p_keys = set(primary_map)
    b_keys = set(baseline_map)
    overlap = p_keys & b_keys
    union = p_keys | b_keys
    compatible_pairs: list[tuple[NerEntity, NerEntity]] = []
    used_baseline: set[int] = set()
    for primary_item in primary_items:
        for baseline_index, baseline_item in enumerate(baseline_items):
            if baseline_index in used_baseline:
                continue
            if entities_compatible(primary_item, baseline_item):
                compatible_pairs.append((primary_item, baseline_item))
                used_baseline.add(baseline_index)
                break

    labels = sorted({label for label, _text in union})
    by_label: dict[str, dict[str, Any]] = {}
    for label in labels:
        p_label = {key for key in p_keys if key[0] == label}
        b_label = {key for key in b_keys if key[0] == label}
        shared = p_label & b_label
        label_union = p_label | b_label
        by_label[label] = {
            "primary": len(p_label),
            "baseline": len(b_label),
            "overlap": len(shared),
            "agreement_ratio": round(len(shared) / len(label_union), 3) if label_union else 1.0,
        }

    def preview(keys: set[tuple[str, str]], mapping: dict[tuple[str, str], NerEntity]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for key in sorted(keys)[:8]:
            item = mapping[key]
            output.append({
                "label": item.label,
                "text": item.text,
                "confidence": round(float(item.confidence), 3),
                "zone": item.zone,
            })
        return output

    return {
        "primary_engine": primary_name,
        "baseline_engine": baseline_name,
        "primary_count": len(p_keys),
        "baseline_count": len(b_keys),
        "overlap": len(overlap),
        "union": len(union),
        "agreement_ratio": round(len(overlap) / len(union), 3) if union else 1.0,
        "compatible_overlap": len(compatible_pairs),
        "compatible_agreement_ratio": round(
            (2 * len(compatible_pairs)) / (len(primary_items) + len(baseline_items)), 3
        ) if (primary_items or baseline_items) else 1.0,
        "by_label": by_label,
        "primary_only_count": len(p_keys - b_keys),
        "baseline_only_count": len(b_keys - p_keys),
        "primary_only": preview(p_keys - b_keys, primary_map),
        "baseline_only": preview(b_keys - p_keys, baseline_map),
    }


def serialize_entities(entities: Iterable[NerEntity]) -> list[dict[str, Any]]:
    return [asdict(entity) for entity in entities]
