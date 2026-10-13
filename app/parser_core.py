from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Iterable


class PageZone(str, Enum):
    HEADER = "header"
    INCOMING = "incoming"
    ADDRESSEE = "addressee"
    BODY = "body"
    SIGNATURE = "signature"
    EXECUTOR_FOOTER = "executor_footer"
    UNKNOWN = "unknown"


class DocumentType(str, Enum):
    REQUEST = "request"
    RESPONSE = "response"
    LETTER = "letter"
    CERTIFICATE = "certificate"
    STATEMENT = "statement"
    MEMO = "memo"
    RESOLUTION = "resolution"
    OTHER = "other"


@dataclass(slots=True)
class DocumentTypeResult:
    value: str = DocumentType.OTHER.value
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FieldCandidate:
    field: str
    value: str
    normalized: str = ""
    confidence: float = 0.0
    source: str = ""
    zone: str = PageZone.UNKNOWN.value
    page_index: int | None = None
    source_blocks: list[dict[str, Any]] = field(default_factory=list)
    extractors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.value = str(self.value or "").strip()
        if not self.normalized:
            self.normalized = FieldNormalizer.normalize(self.field, self.value)
        self.confidence = max(0.0, min(1.0, float(self.confidence or 0.0)))
        if not self.extractors and self.source:
            self.extractors = [self.source]


@dataclass(slots=True)
class FieldResult:
    field: str
    value: str = ""
    normalized: str = ""
    confidence: float = 0.0
    alternatives: list[FieldCandidate] = field(default_factory=list)
    source_blocks: list[dict[str, Any]] = field(default_factory=list)
    extractors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # v1.20.2 calibration metadata.  ``decision`` is intentionally advisory:
    # existing form behaviour stays compatible while diagnostics can distinguish
    # a safe autofill from a value that deserves operator review.
    decision: str = "empty"
    margin: float = 0.0
    evidence: list[str] = field(default_factory=list)


class FieldNormalizer:
    """Conservative, field-specific normalization for Parser Core.

    Normalization is used for comparing candidates and diagnostics. It never
    overwrites the original OCR text and deliberately avoids aggressive spelling
    corrections that could turn one person/organisation into another.
    """

    _DATE_DIGIT_MAP = str.maketrans(
        {"O": "0", "o": "0", "О": "0", "о": "0", "I": "1", "i": "1", "l": "1", "|": "1", "З": "3", "з": "3", "Б": "6", "б": "6"}
    )

    @classmethod
    def normalize(cls, field_name: str, value: str) -> str:
        value = cls._spaces(value)
        if not value:
            return ""
        if field_name in {"received_date", "due_date", "completed_date", "date"}:
            return cls.date(value) or value
        if field_name in {"from_person", "to_person", "person"}:
            return cls.person(value)
        if field_name in {"from_department", "organization", "organisation"}:
            return cls.organization(value)
        if field_name in {"from_position", "position"}:
            return cls.position(value)
        return value

    @staticmethod
    def _spaces(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n")

    @classmethod
    def date(cls, value: str) -> str:
        source = cls._spaces(value).translate(cls._DATE_DIGIT_MAP)
        match = re.search(r"(?<!\d)(\d{1,2})\s*[./,\-]\s*(\d{1,2})\s*[./,\-]\s*(\d{2,4})(?!\d)", source)
        if not match:
            match = re.search(r"(?<!\d)(\d{1,2})\s+(\d{1,2})\s+(\d{2,4})(?!\d)", source)
        if not match:
            return ""
        try:
            day = int(match.group(1))
            month = int(match.group(2))
            year_text = match.group(3)
            year = int("20" + year_text if len(year_text) == 2 else year_text)
            date(year, month, day)
        except ValueError:
            return ""
        return f"{day:02d}.{month:02d}.{year:04d}"

    @classmethod
    def person(cls, value: str) -> str:
        value = cls._spaces(value).strip(" .,:;—–-")
        value = re.sub(r"\s*\.\s*", ".", value)
        value = re.sub(r"(?<=[А-ЯЁ])\.(?=[А-ЯЁ])", ". ", value)
        return cls._spaces(value)

    @classmethod
    def organization(cls, value: str) -> str:
        value = cls._spaces(value).strip(" .,:;—–-")
        value = re.sub(r"\s*№\s*", " №", value)
        return cls._spaces(value)

    @classmethod
    def position(cls, value: str) -> str:
        return cls._spaces(value).strip(" .,:;—–-")

    @classmethod
    def comparison_key(cls, field_name: str, value: str) -> str:
        normalized = cls.normalize(field_name, value).lower().replace("ё", "е")
        normalized = re.sub(r"[^а-яa-z0-9]+", " ", normalized)
        return re.sub(r"\s+", " ", normalized).strip()


class LayoutAnalyzer:
    """Assign OCR blocks to broad semantic page zones.

    This is intentionally geometry-only. Semantic extractors remain separate so
    a block being in the upper-right corner does not automatically make it a
    recipient, and a footer FIO does not automatically become a signer.
    """

    @staticmethod
    def classify_block(block: dict[str, Any]) -> str:
        try:
            x1 = float(block.get("x1", 0.0) or 0.0)
            y1 = float(block.get("y1", 0.0) or 0.0)
            width = float(block.get("page_width", 0.0) or 0.0)
            height = float(block.get("page_height", 0.0) or 0.0)
        except (TypeError, ValueError):
            return PageZone.UNKNOWN.value
        if width <= 0.0 or height <= 0.0:
            return PageZone.UNKNOWN.value

        xr = max(0.0, min(1.0, x1 / width))
        yr = max(0.0, min(1.0, y1 / height))
        if yr >= 0.84:
            return PageZone.EXECUTOR_FOOTER.value
        if yr >= 0.62:
            return PageZone.SIGNATURE.value
        if yr <= 0.18:
            return PageZone.HEADER.value
        if yr <= 0.46 and xr >= 0.48:
            return PageZone.ADDRESSEE.value
        if yr <= 0.38 and xr <= 0.58:
            return PageZone.INCOMING.value
        return PageZone.BODY.value

    @classmethod
    def annotate_blocks(cls, blocks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for index, block in enumerate(blocks or []):
            copied = dict(block)
            copied.setdefault("block_index", index)
            copied["parser_zone"] = cls.classify_block(copied)
            output.append(copied)
        return output

    @classmethod
    def zone_counts(cls, blocks: Iterable[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for block in cls.annotate_blocks(blocks):
            zone = str(block.get("parser_zone", PageZone.UNKNOWN.value))
            counts[zone] = counts.get(zone, 0) + 1
        return counts


class DocumentTypeClassifier:
    """Small rule classifier used before NER is introduced.

    It is diagnostic/contextual rather than authoritative. The resolver can use
    this later for role scoring, while current field extraction remains stable.
    """

    _RULES: tuple[tuple[DocumentType, tuple[tuple[str, float], ...]], ...] = (
        (DocumentType.RESPONSE, (
            (r"\bв\s+ответ\s+на\b", 4.0),
            (r"\bна\s+ваш(?:е|у)?\s+(?:письмо|запрос|обращение)\b", 3.0),
            (r"\bсообщаем\b", 1.0),
        )),
        (DocumentType.REQUEST, (
            (r"\bпрошу\b|\bпросим\b|\bпросит\b", 3.5),
            (r"\bзапрос\b", 2.5),
            (r"\bпредостав(?:ить|ьте)\s+(?:сведения|информацию|документы)\b", 3.0),
            (r"\bнаправ(?:ить|ьте)\s+(?:сведения|информацию|копию|документы)\b", 2.0),
        )),
        (DocumentType.CERTIFICATE, ((r"\bсправка\b", 4.0),)),
        (DocumentType.STATEMENT, ((r"\bзаявление\b", 4.0),)),
        (DocumentType.MEMO, (
            (r"\bслужебная\s+записка\b", 5.0),
            (r"\bдокладная\s+записка\b", 5.0),
        )),
        (DocumentType.RESOLUTION, (
            (r"\bрезолюция\b", 4.0),
            (r"\bк\s+исполнению\b", 2.5),
            (r"\bпоручаю\b", 2.5),
        )),
        (DocumentType.LETTER, (
            (r"\bисх\.?\s*№", 1.5),
            (r"\bуважаем(?:ый|ая|ые)\b", 1.5),
            (r"\bписьмо\b", 2.0),
        )),
    )

    @classmethod
    def classify(cls, text: str) -> DocumentTypeResult:
        source = re.sub(r"\s+", " ", str(text or "")).lower().replace("ё", "е")
        if not source:
            return DocumentTypeResult()

        scored: list[tuple[float, DocumentType, list[str]]] = []
        for doc_type, rules in cls._RULES:
            score = 0.0
            reasons: list[str] = []
            for pattern, weight in rules:
                if re.search(pattern, source, re.IGNORECASE):
                    score += weight
                    reasons.append(pattern)
            if score > 0:
                scored.append((score, doc_type, reasons))

        if not scored:
            return DocumentTypeResult(DocumentType.OTHER.value, 0.35, [])
        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best_type, reasons = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        margin = max(0.0, best_score - runner_up)
        confidence = min(0.95, 0.52 + best_score * 0.055 + margin * 0.025)
        return DocumentTypeResult(best_type.value, round(confidence, 3), reasons[:4])


class RoleType(str, Enum):
    SENDER = "sender"
    RECIPIENT = "recipient"
    SIGNER = "signer"
    SUBJECT_PERSON = "subject_person"
    EXECUTOR = "executor"
    MENTIONED_PERSON = "mentioned_person"


@dataclass(slots=True)
class EntityGroup:
    role: str
    person: str = ""
    organization: str = ""
    position: str = ""
    confidence: float = 0.0
    zone: str = PageZone.UNKNOWN.value
    source_blocks: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RoleAssignment:
    role: str
    value: str = ""
    confidence: float = 0.0
    zone: str = PageZone.UNKNOWN.value
    organization: str = ""
    position: str = ""
    source_blocks: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


class RoleResolver:
    """Resolve semantic person roles from already selected parser fields.

    This layer intentionally does not invent new people. It binds the current
    deterministic parser output to semantic roles using page zone, labels and
    document type. NER will later add more person candidates to the same API.
    """

    _EXECUTOR_RE = re.compile(
        r"(?:исполнитель|исп\.?|исполн\.?)[\s:—–-]*(?P<name>[А-ЯЁ][а-яё-]+\s+[А-ЯЁ]\.?(?:\s*[А-ЯЁ]\.?){0,1}|[А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+)?)",
        re.IGNORECASE,
    )

    @staticmethod
    def _region_zone(regions: list[dict[str, Any]] | None, fallback: str) -> str:
        for region in regions or []:
            zone = str(region.get("parser_zone", "") or "")
            if zone:
                return zone
        return fallback

    @classmethod
    def resolve(
        cls,
        selected: dict[str, str],
        *,
        field_regions: dict[str, list[dict[str, Any]]] | None = None,
        blocks: Iterable[dict[str, Any]] = (),
        document_type: str = DocumentType.OTHER.value,
    ) -> tuple[list[RoleAssignment], list[EntityGroup]]:
        field_regions = field_regions or {}
        assignments: list[RoleAssignment] = []
        groups: list[EntityGroup] = []

        sender = str(selected.get("from_person", "") or "").strip()
        department = str(selected.get("from_department", "") or "").strip()
        position = str(selected.get("from_position", "") or "").strip()
        recipient = str(selected.get("to_person", "") or "").strip()

        if sender:
            zone = cls._region_zone(field_regions.get("from_person"), PageZone.SIGNATURE.value)
            sender_conf = 0.90 if zone == PageZone.SIGNATURE.value else 0.82
            assignments.append(RoleAssignment(
                role=RoleType.SENDER.value, value=sender, confidence=sender_conf,
                zone=zone, organization=department, position=position,
                source_blocks=list(field_regions.get("from_person", [])),
                evidence=["from_person", f"zone:{zone}", f"document_type:{document_type}"],
            ))
            assignments.append(RoleAssignment(
                role=RoleType.SIGNER.value, value=sender,
                confidence=0.94 if zone == PageZone.SIGNATURE.value else 0.76,
                zone=zone, organization=department, position=position,
                source_blocks=list(field_regions.get("from_person", [])),
                evidence=["from_person", "signature-role"],
            ))
            groups.append(EntityGroup(
                role=RoleType.SENDER.value, person=sender, organization=department,
                position=position, confidence=sender_conf, zone=zone,
                source_blocks=list(field_regions.get("from_person", [])),
                evidence=["department+position+person"],
            ))

        if recipient:
            zone = cls._region_zone(field_regions.get("to_person"), PageZone.ADDRESSEE.value)
            assignments.append(RoleAssignment(
                role=RoleType.RECIPIENT.value, value=recipient,
                confidence=0.93 if zone == PageZone.ADDRESSEE.value else 0.80,
                zone=zone, source_blocks=list(field_regions.get("to_person", [])),
                evidence=["to_person", f"zone:{zone}"],
            ))
            groups.append(EntityGroup(
                role=RoleType.RECIPIENT.value, person=recipient,
                confidence=0.93 if zone == PageZone.ADDRESSEE.value else 0.80,
                zone=zone, source_blocks=list(field_regions.get("to_person", [])),
                evidence=["addressee-field"],
            ))

        for block in LayoutAnalyzer.annotate_blocks(blocks):
            if block.get("parser_zone") != PageZone.EXECUTOR_FOOTER.value:
                continue
            text = str(block.get("text", "") or "").strip()
            match = cls._EXECUTOR_RE.search(text)
            if not match:
                continue
            value = re.sub(r"\s+", " ", match.group("name")).strip(" ,:;—–-")
            if not value:
                continue
            assignments.append(RoleAssignment(
                role=RoleType.EXECUTOR.value, value=value, confidence=0.91,
                zone=PageZone.EXECUTOR_FOOTER.value, source_blocks=[block],
                evidence=["executor-marker", "footer-zone"],
            ))
            groups.append(EntityGroup(
                role=RoleType.EXECUTOR.value, person=value, confidence=0.91,
                zone=PageZone.EXECUTOR_FOOTER.value, source_blocks=[block],
                evidence=["executor-marker"],
            ))
            break

        return assignments, groups


class CandidateResolver:
    """Calibrate and combine candidates without losing provenance.

    v1.20.2 adds three safeguards on top of the original resolver:

    * field-specific page-zone priors (a signer belongs near a signature, an
      addressee near the addressee zone, etc.);
    * independent-extractor agreement bonuses instead of trusting a single NER
      span just because its raw model probability is high;
    * an advisory decision class: ``autofill``, ``review`` or ``suggestion``.

    The selected form value is still controlled by :class:`DocumentParser`, so
    calibration can be benchmarked before it is allowed to replace established
    label/filename/layout results.
    """

    SOURCE_BONUS = {
        "label/layout": 0.05,
        "label": 0.04,
        "correction_memory": 0.04,
        "filename": 0.025,
        "layout/heuristic": 0.0,
        "heuristic": 0.0,
        "ner": 0.015,
        "dictionary": 0.01,
    }

    # Positive priors are deliberately small.  They resolve close calls but do
    # not allow geometry alone to overpower a labelled/filename value.
    PREFERRED_ZONES: dict[str, dict[str, float]] = {
        "received_date": {
            PageZone.INCOMING.value: 0.04,
            PageZone.HEADER.value: 0.02,
        },
        "from_department": {
            PageZone.HEADER.value: 0.04,
            PageZone.INCOMING.value: 0.025,
        },
        "from_position": {
            PageZone.SIGNATURE.value: 0.045,
        },
        "from_person": {
            PageZone.SIGNATURE.value: 0.055,
            PageZone.EXECUTOR_FOOTER.value: -0.08,
            PageZone.ADDRESSEE.value: -0.12,
        },
        "to_person": {
            PageZone.ADDRESSEE.value: 0.055,
            PageZone.SIGNATURE.value: -0.12,
            PageZone.EXECUTOR_FOOTER.value: -0.10,
        },
        "request_subject": {
            PageZone.BODY.value: 0.025,
        },
    }

    # Advisory thresholds.  NER-only autofill remains stricter in
    # ``DocumentParser``; these values drive diagnostics/review queues.
    DECISION_THRESHOLDS: dict[str, tuple[float, float, float]] = {
        "received_date": (0.84, 0.66, 0.50),
        "from_department": (0.86, 0.68, 0.52),
        "from_position": (0.86, 0.68, 0.52),
        "from_person": (0.88, 0.70, 0.55),
        "to_person": (0.88, 0.70, 0.55),
        "request_subject": (0.82, 0.64, 0.48),
    }

    NER_AUTOFILL_THRESHOLDS: dict[str, float] = {
        "from_department": 0.88,
        "from_person": 0.90,
        "to_person": 0.90,
    }

    @classmethod
    def _zone_adjustment(cls, field_name: str, candidate: FieldCandidate) -> float:
        return float(cls.PREFERRED_ZONES.get(field_name, {}).get(candidate.zone, 0.0))

    @classmethod
    def _candidate_pre_score(cls, field_name: str, candidate: FieldCandidate) -> float:
        return max(0.0, min(0.99, candidate.confidence + cls._zone_adjustment(field_name, candidate)))

    @staticmethod
    def _support_tokens(items: Iterable[FieldCandidate]) -> set[str]:
        tokens: set[str] = set()
        for item in items:
            if item.extractors:
                tokens.update(str(value) for value in item.extractors if value)
            elif item.source:
                tokens.add(item.source)
            engine = str(item.metadata.get("ner_engine", "") or "")
            if engine:
                tokens.add(engine)
        return tokens

    @classmethod
    def _ranked(cls, field_name: str, candidates: Iterable[FieldCandidate]) -> list[FieldCandidate]:
        groups: dict[str, list[FieldCandidate]] = {}
        for candidate in candidates:
            if not candidate.value:
                continue
            key = FieldNormalizer.comparison_key(field_name, candidate.value)
            if not key:
                continue
            groups.setdefault(key, []).append(candidate)

        merged: list[FieldCandidate] = []
        for items in groups.values():
            items.sort(key=lambda item: cls._candidate_pre_score(field_name, item), reverse=True)
            primary = items[0]
            support_tokens = cls._support_tokens(items)
            # Multiple independent extractors agreeing on the same normalized
            # value are more useful than duplicate spans from one engine.
            support_bonus = min(0.06, 0.02 * max(0, len(support_tokens) - 1))
            source_bonus = max(cls.SOURCE_BONUS.get(item.source, 0.0) for item in items)
            zone_adjustment = cls._zone_adjustment(field_name, primary)
            confidence = min(0.99, max(0.0, primary.confidence + support_bonus + source_bonus + zone_adjustment))
            extractors: list[str] = []
            warnings: list[str] = []
            source_blocks: list[dict[str, Any]] = []
            seen_blocks: set[str] = set()
            for item in items:
                for name in item.extractors or ([item.source] if item.source else []):
                    if name and name not in extractors:
                        extractors.append(name)
                for warning in item.warnings:
                    if warning and warning not in warnings:
                        warnings.append(warning)
                for block in item.source_blocks:
                    marker = repr(block)
                    if marker not in seen_blocks:
                        seen_blocks.add(marker)
                        source_blocks.append(block)
            if zone_adjustment < 0:
                warnings.append(f"zone_mismatch:{primary.zone}")
            merged.append(FieldCandidate(
                field=field_name,
                value=primary.value,
                normalized=primary.normalized,
                confidence=round(confidence, 3),
                source=primary.source,
                zone=primary.zone,
                page_index=primary.page_index,
                source_blocks=source_blocks,
                extractors=extractors,
                warnings=warnings,
                metadata={
                    **dict(primary.metadata),
                    "support_count": len(items),
                    "support_sources": sorted(support_tokens),
                    "raw_confidence": round(primary.confidence, 3),
                    "zone_adjustment": round(zone_adjustment, 3),
                    "source_bonus": round(source_bonus, 3),
                    "support_bonus": round(support_bonus, 3),
                    "calibrated_score": round(confidence, 3),
                },
            ))
        merged.sort(key=lambda item: item.confidence, reverse=True)
        return merged

    @staticmethod
    def _department_values_compatible(left: str, right: str) -> bool:
        """Treat nested organisation names as supporting evidence, not rivals.

        OCR/layout extractors often return the full formal sender name while NER
        returns a shorter contained span (for example ``ГУ МВД России`` inside a
        longer territorial department name).  Those should not create an
        artificial close-alternative review signal.
        """
        left_key = FieldNormalizer.comparison_key("from_department", left)
        right_key = FieldNormalizer.comparison_key("from_department", right)
        if not left_key or not right_key:
            return False
        if left_key == right_key:
            return True
        left_tokens = {token for token in left_key.split() if len(token) >= 2}
        right_tokens = {token for token in right_key.split() if len(token) >= 2}
        if not left_tokens or not right_tokens:
            return False
        shared = left_tokens & right_tokens
        coverage = len(shared) / max(1, min(len(left_tokens), len(right_tokens)))
        contained = left_key in right_key or right_key in left_key
        return coverage >= 0.80 and (contained or len(shared) >= 3)

    @classmethod
    def _same_decision_value(cls, field_name: str, left: str, right: str) -> bool:
        left_key = FieldNormalizer.comparison_key(field_name, left)
        right_key = FieldNormalizer.comparison_key(field_name, right)
        if bool(left_key) and left_key == right_key:
            return True
        if field_name == "from_department":
            return cls._department_values_compatible(left, right)
        return False

    @classmethod
    def decision_for(cls, field_name: str, confidence: float, margin: float, *, has_value: bool = True) -> str:
        if not has_value:
            return "empty"
        autofill, review, suggestion = cls.DECISION_THRESHOLDS.get(field_name, (0.86, 0.68, 0.52))
        # Close competing alternatives always deserve review even when the top
        # score alone would have crossed the autofill threshold.
        if confidence >= autofill and margin >= 0.07:
            return "autofill"
        if confidence >= review:
            return "review"
        if confidence >= suggestion:
            return "suggestion"
        return "low_confidence"

    @classmethod
    def ner_autofill_threshold(cls, field_name: str) -> float:
        return cls.NER_AUTOFILL_THRESHOLDS.get(field_name, 0.92)

    @staticmethod
    def _evidence(candidate: FieldCandidate | None) -> list[str]:
        if candidate is None:
            return []
        output: list[str] = []
        if candidate.source:
            output.append(f"source:{candidate.source}")
        if candidate.zone and candidate.zone != PageZone.UNKNOWN.value:
            output.append(f"zone:{candidate.zone}")
        support = candidate.metadata.get("support_sources") or []
        if support:
            output.append("agreement:" + ",".join(str(item) for item in support))
        zone_adjustment = candidate.metadata.get("zone_adjustment")
        if zone_adjustment:
            output.append(f"zone_adjustment:{float(zone_adjustment):+.3f}")
        return output

    @classmethod
    def rank(cls, field_name: str, candidates: Iterable[FieldCandidate]) -> list[FieldCandidate]:
        """Return calibrated candidates in production order."""
        return cls._ranked(field_name, candidates)

    @classmethod
    def values_compatible(cls, field_name: str, left: str, right: str) -> bool:
        return cls._same_decision_value(field_name, left, right)

    @classmethod
    def resolve(cls, field_name: str, candidates: Iterable[FieldCandidate]) -> FieldResult:
        ranked = cls._ranked(field_name, candidates)
        if not ranked:
            return FieldResult(field=field_name)
        winner = ranked[0]
        competing = [
            item for item in ranked[1:]
            if not cls._same_decision_value(field_name, winner.value, item.value)
        ]
        runner_score = competing[0].confidence if competing else 0.0
        margin = round(max(0.0, winner.confidence - runner_score), 3)
        decision = cls.decision_for(field_name, winner.confidence, margin, has_value=True)
        warnings = list(winner.warnings)
        if len(ranked) > 1 and margin < 0.07:
            warnings.append("close_alternative")
        return FieldResult(
            field=field_name,
            value=winner.value,
            normalized=winner.normalized,
            confidence=winner.confidence,
            alternatives=ranked[1:4],
            source_blocks=winner.source_blocks,
            extractors=winner.extractors,
            warnings=warnings,
            decision=decision,
            margin=margin,
            evidence=cls._evidence(winner),
        )

    @classmethod
    def result_for_selected(
        cls,
        field_name: str,
        candidates: Iterable[FieldCandidate],
        *,
        selected_value: str,
        selected_confidence: float,
        selected_source: str,
        source_blocks: list[dict[str, Any]] | None = None,
    ) -> FieldResult:
        ranked = cls._ranked(field_name, candidates)
        selected_key = FieldNormalizer.comparison_key(field_name, selected_value)
        selected_candidate = next((
            item for item in ranked
            if selected_key and FieldNormalizer.comparison_key(field_name, item.value) == selected_key
        ), None)
        # Sender organisations are frequently represented at different levels
        # of specificity (full legal name vs a shorter nested NER span).  When
        # the form already selected the deterministic/layout value, let a
        # compatible richer candidate contribute confidence instead of treating
        # it as unrelated evidence.
        if selected_candidate is None and field_name == "from_department" and selected_value:
            selected_candidate = next((
                item for item in ranked
                if cls._department_values_compatible(selected_value, item.value)
            ), None)

        alternatives = [
            item for item in ranked
            if not cls._same_decision_value(field_name, selected_value, item.value)
        ][:3]
        extractors: list[str] = []
        if selected_candidate is not None:
            extractors.extend(selected_candidate.extractors)
        if selected_source and selected_source not in extractors:
            extractors.insert(0, selected_source)

        calibrated = max(0.0, min(1.0, float(selected_confidence or 0.0)))
        if selected_candidate is not None:
            calibrated = max(calibrated, selected_candidate.confidence)
        # In confirmation-only mode NER is advisory evidence.  A conflicting
        # NER span must not by itself downgrade a deterministic label/layout/
        # filename value to review.  We still expose such spans in
        # ``alternatives`` for diagnostics.  Genuine non-NER alternatives keep
        # their old veto power.
        decision_alternatives = list(alternatives)
        if selected_source and selected_source != "ner" and field_name in {
            "from_department", "from_person", "to_person"
        }:
            decision_alternatives = [item for item in alternatives if item.source != "ner"]

        runner_score = decision_alternatives[0].confidence if decision_alternatives else 0.0
        margin = round(max(0.0, calibrated - runner_score), 3) if selected_value else 0.0
        decision = cls.decision_for(field_name, calibrated, margin, has_value=bool(selected_value))
        warnings = list(selected_candidate.warnings if selected_candidate else [])
        if selected_value and decision_alternatives and margin < 0.07:
            warnings.append("close_alternative")
            if decision == "autofill":
                decision = "review"
        elif selected_value and alternatives and not decision_alternatives:
            warnings.append("advisory_ner_alternative")
        evidence = cls._evidence(selected_candidate)
        if selected_source and f"source:{selected_source}" not in evidence:
            evidence.insert(0, f"source:{selected_source}")
        return FieldResult(
            field=field_name,
            value=str(selected_value or ""),
            normalized=FieldNormalizer.normalize(field_name, selected_value),
            confidence=round(calibrated, 3),
            alternatives=alternatives,
            source_blocks=list(source_blocks or []),
            extractors=extractors,
            warnings=warnings,
            decision=decision,
            margin=margin,
            evidence=evidence,
        )

