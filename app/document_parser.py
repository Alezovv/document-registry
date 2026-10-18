from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app.models import Request
from app.hybrid_parser import HybridChoice, HybridFieldResolver
from app.parser_correction_memory import ParserCorrectionMemory
from app.ner import (
    HeuristicRussianNerEngine, NerCandidateAdapter, NerEngine, NerRoleAdapter,
    compare_entity_sets, select_trusted_ml_entities, serialize_entities,
)
from app.ner_runtime import current_engine_cache_key, get_default_ner_engine
from app.parser_core import (
    CandidateResolver, DocumentTypeClassifier, FieldCandidate, LayoutAnalyzer, PageZone, RoleResolver,
)
from app.document_parser_parts.constants import (
    FIELD_CONFIDENCE_FLOOR,
    FIELD_NAMES,
)
from app.document_parser_parts.fields import FieldExtractionMixin
from app.document_parser_parts.filename import FilenameParsingMixin
from app.document_parser_parts.layout import LayoutAnalysisMixin
from app.document_parser_parts.normalization import NormalizationMixin
from app.document_parser_parts.subject import SubjectExtractionMixin

logger = logging.getLogger(__name__)

PARSER_VERSION = "1.22.1"


def get_parser_cache_version(memory_key: str | None = None) -> str:
    """Include the active NER backend in parser cache compatibility.

    Installing/removing the external ML model must force a re-parse from the OCR
    cache; otherwise a document parsed by the heuristic engine would keep its
    old result after ML NER becomes available.
    """
    base = f"{PARSER_VERSION}|{current_engine_cache_key()}"
    return f"{base}|memory:{memory_key}" if memory_key else base


@dataclass(slots=True)
class ParseDiagnostics:
    field_confidence: dict[str, float] = field(default_factory=dict)
    field_sources: dict[str, str] = field(default_factory=dict)
    document_number: str = ""
    document_number_confidence: float = 0.0
    field_candidate_scores: dict[str, list[float]] = field(default_factory=dict)
    field_regions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    document_type: str = "other"
    document_type_confidence: float = 0.0
    document_type_reasons: list[str] = field(default_factory=list)
    layout_zone_counts: dict[str, int] = field(default_factory=dict)
    field_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    role_assignments: list[dict[str, Any]] = field(default_factory=list)
    entity_groups: list[dict[str, Any]] = field(default_factory=list)
    ner_engine: str = ""
    ner_entity_counts: dict[str, int] = field(default_factory=dict)
    ner_hybrid_entity_counts: dict[str, int] = field(default_factory=dict)
    ner_trusted_entity_counts: dict[str, int] = field(default_factory=dict)
    ner_entities: list[dict[str, Any]] = field(default_factory=list)
    ner_metrics: dict[str, Any] = field(default_factory=dict)
    ner_comparison: dict[str, Any] = field(default_factory=dict)
    field_decisions: dict[str, str] = field(default_factory=dict)
    review_fields: list[str] = field(default_factory=list)
    hybrid_changed_fields: list[str] = field(default_factory=list)
    hybrid_actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    hybrid_candidate_counts: dict[str, int] = field(default_factory=dict)
    correction_memory_key: str = ""


class DocumentParserError(RuntimeError):
    pass


class SufficiencyResult(tuple):
    """Backwards-compatible tuple (is_sufficient, reason) that also exposes .suggested_regions, .missing_reasons, and .preparsed_fields."""

    def __new__(
        cls,
        is_sufficient: bool,
        reason: str,
        suggested_regions: list[dict[str, Any]] | None = None,
        missing_reasons: list[str] | None = None,
        preparsed_fields: dict[str, Any] | None = None,
    ):
        return super().__new__(cls, (is_sufficient, reason))

    def __init__(
        self,
        is_sufficient: bool,
        reason: str,
        suggested_regions: list[dict[str, Any]] | None = None,
        missing_reasons: list[str] | None = None,
        preparsed_fields: dict[str, Any] | None = None,
    ):
        self.is_sufficient = is_sufficient
        self.reason = reason
        self.suggested_regions = suggested_regions or []
        self.missing_reasons = missing_reasons or []
        self.preparsed_fields = preparsed_fields or {}


class DocumentParser(
    FilenameParsingMixin,
    NormalizationMixin,
    SubjectExtractionMixin,
    LayoutAnalysisMixin,
    FieldExtractionMixin,
):
    """Deterministic local document parser.

    Public compatibility facade. Implementation details live in
    ``app.document_parser_parts`` to keep extraction concerns isolated while
    preserving the existing ``DocumentParser`` API.
    """

    def __init__(
        self,
        ner_engine: NerEngine | None = None,
        correction_memory: ParserCorrectionMemory | None = None,
        hybrid_enabled: bool = True,
    ) -> None:
        self.last_diagnostics = ParseDiagnostics()
        self._subject_candidate_scores: list[float] = []
        self.ner_engine = ner_engine or get_default_ner_engine()
        self.correction_memory = correction_memory
        self.hybrid_enabled = bool(hybrid_enabled)

    @classmethod
    def suggest_retry_regions(
        cls,
        text: str,
        blocks: list[dict[str, Any]] | None,
        missing_reasons: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Identify minimal high-resolution scan zones targeting missing/ambiguous fields."""
        import re

        blocks_list = blocks or []
        page_w = max((float(b.get("page_width", 0) or 0) for b in blocks_list), default=1000.0)
        page_h = max((float(b.get("page_height", 0) or 0) for b in blocks_list), default=1400.0)
        reasons = set(missing_reasons or [])

        regions: list[dict[str, Any]] = []

        needs_upper = (
            any(
                r in reasons
                for r in (
                    "missing_or_unreliable_date",
                    "missing_department",
                    "missing_document_number",
                )
            )
            or not reasons
        )
        needs_lower = (
            any(
                r in reasons
                for r in (
                    "missing_signer",
                    "missing_position",
                    "role_confusion_signer_and_target",
                )
            )
            or not reasons
        )

        # 1. Upper region (Header, Incoming Stamp, Document Number, Date)
        if needs_upper:
            anchor_stamp = None
            for b in blocks_list:
                by1 = float(b.get("y1", 0))
                if by1 > page_h * 0.45:
                    continue
                bt = str(b.get("text", "")).strip()
                if re.search(r"(?i)\b(?:вх\.?|входящ\w*|исх\.?|исходящ\w*|№|номер)\b", bt):
                    anchor_stamp = b
                    break

            if anchor_stamp:
                ax1 = float(anchor_stamp.get("x1", 0))
                ay1 = float(anchor_stamp.get("y1", 0))
                ax2 = float(anchor_stamp.get("x2", ax1))
                ay2 = float(anchor_stamp.get("y2", ay1))
                if ax1 < page_w * 0.5:
                    nx0 = 0.0
                    nx1 = min(1.0, max(0.68, (ax2 + 250.0) / page_w))
                else:
                    nx0 = max(0.0, min(0.35, (ax1 - 250.0) / page_w))
                    nx1 = 1.0
                ny0 = max(0.0, (ay1 - 80.0) / page_h)
                ny1 = min(0.48, (ay2 + 250.0) / page_h)
                regions.append({
                    "field": "incoming_stamp",
                    "norm_rect": (nx0, ny0, nx1, ny1),
                    "reason": "anchor_stamp",
                })
            else:
                regions.append({
                    "field": "header_and_stamp",
                    "norm_rect": (0.0, 0.0, 1.0, 0.42),
                    "reason": "structural_fallback_upper",
                })

        # 2. Lower region (Signer, Position)
        if needs_lower:
            anchor_sig = None
            for b in blocks_list:
                by1 = float(b.get("y1", 0))
                if by1 < page_h * 0.45:
                    continue
                bt = str(b.get("text", "")).strip()
                if re.search(
                    r"(?i)\b(?:судья|начальник|главн\w+\s+врач|инспектор|директор|майор|капитан|подполковник|лейтенант|уполномоченный|следователь|дознаватель|подпись|м\.п\.)\b",
                    bt,
                ):
                    anchor_sig = b
                    break

            if anchor_sig:
                ay1 = float(anchor_sig.get("y1", 0))
                ny0 = max(0.45, (ay1 - 90.0) / page_h)
                ny1 = 1.0
                regions.append({
                    "field": "signature",
                    "norm_rect": (0.0, ny0, 1.0, ny1),
                    "reason": "anchor_signature",
                })
            else:
                regions.append({
                    "field": "signature",
                    "norm_rect": (0.0, 0.58, 1.0, 1.0),
                    "reason": "structural_fallback_lower",
                })

        return regions

    @classmethod
    def is_extraction_sufficient(
        cls,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> SufficiencyResult:
        """Lightweight pre-check to decide if OCR pass 1 is already reliable enough
        to skip expensive 200 DPI retries and rotation retries.

        Evaluates all core fields without loading heavy ML/NER models:
        - received_date: from incoming stamp or header date (valid date format, not mid-paragraph event date).
        - from_department: department/sender is present and >= 4 chars.
        - doc_number: document number is present.
        - from_position: position/title is present (or clear signature marker).
        - from_person: signer FIO is present in signature/header.
        - to_person: target person (if present) must NOT be confused with the signer or executor.
        - request_subject: subject is present and meaningful (>= 10 chars).
        """
        if not text or len(text.strip()) < 50:
            suggested = cls.suggest_retry_regions("", blocks, ["text_too_short"])
            return SufficiencyResult(False, "text_too_short", suggested, ["text_too_short"])

        blocks_list = blocks or []
        zones = cls._layout_zones(text, blocks_list)
        header_text = zones.get("header", "")
        incoming_text = zones.get("incoming", "")
        body_text = zones.get("body", "")
        signature_text = zones.get("signature", "") or zones.get("footer", "")
        content_text = zones.get("content", "") or body_text

        missing_reasons: list[str] = []

        # 1. Date: must come from incoming stamp or header
        date_val = (
            cls._extract_incoming_date(incoming_text or text, blocks_list)
            or cls._extract_received_date(header_text, text)
        )
        if not date_val:
            missing_reasons.append("missing_or_unreliable_date")

        # 2. Sender / Department:
        dept_val = cls._extract_department(header_text, text)
        if not dept_val or len(dept_val.strip()) < 4:
            missing_reasons.append("missing_department")

        # 3. Document Number:
        num_lines: list[str] = []
        _seen_num_lines: set[str] = set()
        for _l in (header_text + "\n" + "\n".join(text.splitlines()[:35])).splitlines():
            _s = _l.strip()
            if _s and _s not in _seen_num_lines:
                _seen_num_lines.add(_s)
                num_lines.append(_s)
        num_header = "\n".join(num_lines[:35])
        doc_num, doc_conf = cls._extract_document_number(num_header, blocks_list)
        if not doc_num or doc_conf < 0.40:
            missing_reasons.append("missing_document_number")

        # 4. Signer:
        signer_val = cls._extract_signer(signature_text or text, full_text=text)
        if not signer_val:
            missing_reasons.append("missing_signer")

        # 5. Position:
        pos_val = cls._extract_position(signature_text, body_text, header_text)
        if not pos_val:
            from app.ocr import OCR

            if not OCR._has_signer_on_page(signature_text or text):
                missing_reasons.append("missing_position")

        # 6. Subject:
        subj_val, subj_scores = cls._extract_subject_ranked(content_text)
        if not subj_val:
            subj_val, subj_scores = cls._extract_subject_ranked(text)
        if not subj_val or len(subj_val.strip()) < 10:
            missing_reasons.append("missing_subject")

        # 7. Target person vs Signer (no role confusion):
        to_person_val = cls._extract_to_person(content_text) or cls._extract_to_person(body_text)

        clean_text = text.strip()
        text_sha = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()

        preparsed = {
            "_text_len": len(text),
            "_text_hash": text_sha,
            "received_date": date_val or "",
            "from_department": dept_val or "",
            "doc_number": doc_num or "",
            "doc_number_confidence": doc_conf or 0.0,
            "from_person": signer_val or "",
            "from_position": pos_val or "",
            "request_subject": subj_val or "",
            "request_subject_scores": list(subj_scores) if subj_scores else [],
            "to_person": to_person_val or "",
        }

        if missing_reasons:
            suggested = cls.suggest_retry_regions(text, blocks_list, missing_reasons)
            return SufficiencyResult(False, missing_reasons[0], suggested, missing_reasons, preparsed)

        if to_person_val and signer_val:
            norm_target = cls._normalize_target_name(to_person_val)
            norm_signer = cls._normalize_person_name(signer_val)
            t_parts = norm_target.split()
            s_parts = norm_signer.split()
            if t_parts and s_parts and t_parts[0] == s_parts[0]:
                if len(t_parts) > 1 and len(s_parts) > 1 and t_parts[1][0] == s_parts[1][0]:
                    suggested = cls.suggest_retry_regions(
                        text, blocks_list, ["role_confusion_signer_and_target"]
                    )
                    return SufficiencyResult(
                        False, "role_confusion_signer_and_target", suggested, ["role_confusion_signer_and_target"], preparsed
                    )
                if len(t_parts) == 1 or len(s_parts) == 1:
                    suggested = cls.suggest_retry_regions(
                        text, blocks_list, ["role_confusion_signer_and_target"]
                    )
                    return SufficiencyResult(
                        False, "role_confusion_signer_and_target", suggested, ["role_confusion_signer_and_target"], preparsed
                    )

        return SufficiencyResult(True, "all_core_fields_reliable", [], [], preparsed)

    def parse(
        self,
        ocr_data: str | dict[str, Any],
        file_name: str = "",
    ) -> Request:
        raw_preparsed = None
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
            raw_preparsed = ocr_data.get("_preparsed")
        else:
            text = str(ocr_data)
            blocks = []

        text = self._prepare_ocr_text(text)
        if not text and not file_name:
            raise DocumentParserError("Текст после OCR пуст и имя файла не передано.")

        preparsed: dict[str, Any] = {}
        if isinstance(raw_preparsed, dict):
            clean_body = re.sub(r"^---\s*Страница\s+\d+\s*---\s*", "", text).strip()
            expected_hash = raw_preparsed.get("_text_hash")
            current_sha = hashlib.sha256(clean_body.encode("utf-8")).hexdigest()
            is_valid = (
                expected_hash == current_sha
                or (isinstance(expected_hash, int) and expected_hash == hash(clean_body[:200] + clean_body[-200:]))
            )
            if expected_hash is not None and is_valid:
                preparsed = raw_preparsed

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

        subj = preparsed.get("request_subject")
        if subj:
            if preparsed.get("request_subject_scores"):
                self._subject_candidate_scores = list(preparsed["request_subject_scores"])
        else:
            subj = (
                self._extract_subject_with_scores(content_text)
                or self._extract_subject_with_scores(text)
            )

        heuristic = {
            "received_date": (
                preparsed.get("received_date")
                or (
                    self._extract_incoming_date(incoming_text or text, blocks)
                    or self._extract_received_date(header_text, text)
                )
            ),
            "from_department": (
                preparsed.get("from_department")
                or self._extract_department(header_text, text)
            ),
            "from_position": (
                preparsed.get("from_position")
                or self._extract_position(signature_text, body_text, header_text)
            ),
            "from_person": (
                preparsed.get("from_person")
                or self._extract_signer(signature_text or text, full_text=text)
            ),
            # Addressee zone is useful for labelled/form letters, but the semantic
            # target extractor still requires person-context before accepting a FIO.
            "to_person": (
                preparsed.get("to_person")
                or (
                    self._extract_to_person(content_text)
                    or self._extract_to_person(body_text)
                    or self._extract_to_person(addressee_text)
                    or self._extract_to_person(text)
                )
            ),
            "request_subject": subj,
        }

        heuristic_confidence = {
            field_name: self._heuristic_confidence(
                field_name, heuristic.get(field_name, ""), blocks, zones
            )
            for field_name in FIELD_NAMES
        }
        result: dict[str, str] = {}
        confidence: dict[str, float] = {}
        sources: dict[str, str] = {}
        for field_name in FIELD_NAMES:
            if field_name in explicit and explicit[field_name]:
                val = explicit[field_name]
                result[field_name] = val
                base_conf = 0.97 if field_name in spatial else 0.95
                if blocks:
                    val_key = self._label_key(val)
                    matched_scores = [
                        float(b["confidence"])
                        for b in blocks
                        if b.get("confidence") is not None
                        and val_key
                        and (val_key in self._label_key(str(b.get("text", ""))) or self._label_key(str(b.get("text", ""))) in val_key)
                    ]
                    if matched_scores:
                        avg_ocr = sum(matched_scores) / len(matched_scores)
                        if avg_ocr < 0.78:
                            base_conf = max(0.58, round(0.55 + avg_ocr * 0.32, 3))
                if self._looks_low_quality_value(field_name, val):
                    base_conf = min(base_conf, 0.65)
                confidence[field_name] = base_conf
                sources[field_name] = "label/layout" if field_name in spatial else "label"
            else:
                value = heuristic.get(field_name, "")
                result[field_name] = value
                confidence[field_name] = heuristic_confidence[field_name]
                sources[field_name] = "layout/heuristic" if value else "none"

        file_meta: dict[str, str] = {}
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

        document_type = DocumentTypeClassifier.classify(text)
        ner_target_fields = ("from_department", "from_person", "to_person")
        all_ner_fields_strong = all(
            bool(result.get(f)) and confidence.get(f, 0.0) >= 0.88
            for f in ner_target_fields
        )
        if all_ner_fields_strong and getattr(self.ner_engine, "name", "") == "onnx-with-heuristic-fallback":
            ner_entities = HeuristicRussianNerEngine().extract(text, blocks)
            ner_metrics = {
                "engine": self.ner_engine.name,
                "active_engine": HeuristicRussianNerEngine.name,
                "loaded": False,
                "skipped_heavy_ner": True,
                "entities": len(ner_entities),
            }
            active_ner_engine = HeuristicRussianNerEngine.name
        else:
            ner_entities = self.ner_engine.extract(text, blocks)
            ner_metrics = dict(getattr(self.ner_engine, "last_metrics", {}) or {})
            active_ner_engine = str(ner_metrics.get("active_engine") or self.ner_engine.name)

        # ML NER is compared against the cheap deterministic baseline on the same
        # OCR blocks.  The baseline is a shadow signal only when ML is active;
        # explicit NER-off mode stays truly off.  Agreement becomes an independent
        # support signal for CandidateResolver, while disagreements remain visible
        # in diagnostics for later tuning on real archive documents.
        shadow_entities = []
        trusted_ml_entities = []
        hybrid_ner_entities = list(ner_entities)
        if active_ner_engine not in {HeuristicRussianNerEngine.name, "disabled"}:
            shadow_entities = HeuristicRussianNerEngine().extract(text, blocks)
            # Production policy is confirmation-only: the news-domain model is
            # evidence, not an independent source of sender/addressee values.
            trusted_ml_entities = select_trusted_ml_entities(
                ner_entities, shadow_entities, allow_standalone=False
            )
            # Only trusted ML spans are allowed to compete for registry fields.
            # Raw model output stays in diagnostics so we can tune the model
            # without letting domain noise degrade an otherwise good parse.
            hybrid_ner_entities = list(shadow_entities) + list(trusted_ml_entities)
            ner_comparison = compare_entity_sets(
                ner_entities, shadow_entities,
                primary_name=active_ner_engine,
                baseline_name=HeuristicRussianNerEngine.name,
            )
            ner_comparison["trusted_ml_count"] = len(trusted_ml_entities)
            ner_comparison["trusted_ml_policy"] = "role-aware-confirmation-only"
        elif active_ner_engine == HeuristicRussianNerEngine.name:
            ner_comparison = compare_entity_sets(
                ner_entities, ner_entities,
                primary_name=active_ner_engine,
                baseline_name=HeuristicRussianNerEngine.name,
            )
        else:
            ner_comparison = {
                "primary_engine": active_ner_engine,
                "baseline_engine": HeuristicRussianNerEngine.name,
                "disabled": True,
                "agreement_ratio": None,
                "compatible_agreement_ratio": None,
            }

        ner_candidates = NerCandidateAdapter.to_field_candidates(hybrid_ner_entities)

        if preparsed.get("doc_number"):
            document_number = preparsed["doc_number"]
            document_number_confidence = float(preparsed.get("doc_number_confidence", 0.0) or 0.0)
        else:
            num_lines: list[str] = []
            _seen_num_lines: set[str] = set()
            for _l in (header_text + "\n" + "\n".join(text.splitlines()[:35])).splitlines():
                _s = _l.strip()
                if _s and _s not in _seen_num_lines:
                    _seen_num_lines.add(_s)
                    num_lines.append(_s)
            num_header = "\n".join(num_lines[:35])
            document_number, document_number_confidence = self._extract_document_number(
                num_header, blocks
            )
        layout_zone_counts = LayoutAnalyzer.zone_counts(blocks)
        field_results: dict[str, dict[str, Any]] = {}
        default_zones = {
            "received_date": PageZone.INCOMING.value,
            "from_department": PageZone.HEADER.value,
            "from_position": PageZone.SIGNATURE.value,
            "from_person": PageZone.SIGNATURE.value,
            "to_person": PageZone.ADDRESSEE.value,
            "request_subject": PageZone.BODY.value,
        }
        all_candidates: dict[str, list[FieldCandidate]] = {}
        hybrid_actions: dict[str, dict[str, Any]] = {}
        hybrid_changed_fields: list[str] = []

        memory_context = {
            "document_type": document_type.value,
            "field_sources": dict(sources),
            "field_regions": {
                name: [{"parser_zone": default_zones.get(name, PageZone.UNKNOWN.value)}]
                for name in FIELD_NAMES
            },
        }

        for field_name in FIELD_NAMES:
            candidates: list[FieldCandidate] = []
            if explicit.get(field_name):
                explicit_source = "label/layout" if field_name in spatial else "label"
                candidates.append(FieldCandidate(
                    field=field_name, value=explicit[field_name],
                    confidence=confidence.get(field_name, 0.97 if field_name in spatial else 0.95),
                    source=explicit_source, zone=default_zones.get(field_name, PageZone.UNKNOWN.value),
                ))
            if heuristic.get(field_name):
                candidates.append(FieldCandidate(
                    field=field_name, value=heuristic[field_name],
                    confidence=heuristic_confidence.get(field_name, 0.0),
                    source="layout/heuristic", zone=default_zones.get(field_name, PageZone.UNKNOWN.value),
                ))
            if file_meta.get(field_name):
                candidates.append(FieldCandidate(
                    field=field_name, value=file_meta[field_name], confidence=0.88,
                    source="filename", zone=PageZone.UNKNOWN.value,
                ))
            candidates.extend(ner_candidates.get(field_name, []))
            candidates = HybridFieldResolver.add_correction_memory_candidates(
                field_name, candidates, self.correction_memory, context=memory_context
            )
            all_candidates[field_name] = candidates

            if self.hybrid_enabled:
                choice = HybridFieldResolver.choose(
                    field_name, candidates,
                    baseline_value=result.get(field_name, ""),
                    baseline_confidence=confidence.get(field_name, 0.0),
                    baseline_source=sources.get(field_name, "none"),
                )
            else:
                # Benchmark/debug mode: keep the deterministic pre-v1.21 value
                # while still building the same candidate/NER diagnostics. This
                # enables apples-to-apples A/B evaluation against user-reviewed
                # database values without changing production behaviour.
                choice = HybridChoice(
                    field=field_name,
                    value=str(result.get(field_name, "") or ""),
                    confidence=float(confidence.get(field_name, 0.0) or 0.0),
                    source=sources.get(field_name, "none"),
                    changed=False,
                    reason="hybrid_disabled",
                    candidate_count=len(candidates),
                    memory_candidate_count=sum(
                        1 for item in candidates if item.source == "correction_memory"
                    ),
                )
            if choice.value != result.get(field_name, ""):
                result[field_name] = choice.value
                confidence[field_name] = choice.confidence
                sources[field_name] = choice.source
            else:
                confidence[field_name] = max(confidence.get(field_name, 0.0), choice.confidence)
            if choice.changed:
                hybrid_changed_fields.append(field_name)
            hybrid_actions[field_name] = asdict(choice)

        result = self._final_validate(result)
        for field_name in FIELD_NAMES:
            if not result.get(field_name):
                confidence[field_name] = 0.0
                if sources.get(field_name) not in {"none", "rejected"}:
                    sources[field_name] = "rejected"
            elif blocks:
                val_key = self._label_key(result[field_name])
                matched_ocr = [
                    float(b["confidence"])
                    for b in blocks
                    if b.get("confidence") is not None
                    and val_key
                    and (val_key in self._label_key(str(b.get("text", ""))) or self._label_key(str(b.get("text", ""))) in val_key)
                ]
                if matched_ocr:
                    avg_ocr = sum(matched_ocr) / len(matched_ocr)
                    if avg_ocr < 0.78:
                        confidence[field_name] = min(confidence[field_name], round(0.55 + avg_ocr * 0.35, 3))

        # Re-locate regions after the hybrid result has become the actual form value.
        field_regions = self._locate_field_regions(result, blocks, sources)

        for field_name in FIELD_NAMES:
            field_result = CandidateResolver.result_for_selected(
                field_name, all_candidates.get(field_name, []),
                selected_value=result.get(field_name, ""),
                selected_confidence=confidence.get(field_name, 0.0),
                selected_source=sources.get(field_name, "none"),
                source_blocks=field_regions.get(field_name, []),
            )
            action = hybrid_actions.get(field_name, {})
            reason = str(action.get("reason", "") or "")
            if reason:
                field_result.evidence.append(f"hybrid:{reason}")
            if bool(action.get("changed")):
                field_result.warnings.append("hybrid_changed_value")
            field_results[field_name] = asdict(field_result)

        field_decisions = {
            name: str(payload.get("decision", "empty"))
            for name, payload in field_results.items()
        }
        review_fields = [
            name for name, decision in field_decisions.items()
            if decision in {"review", "suggestion", "low_confidence"}
            and bool(result.get(name))
        ]

        role_assignments, entity_groups = RoleResolver.resolve(
            result,
            field_regions=field_regions,
            blocks=blocks,
            document_type=document_type.value,
        )
        ner_roles, ner_groups = NerRoleAdapter.resolve(
            hybrid_ner_entities, document_type=document_type.value,
        )
        role_assignments = NerRoleAdapter.merge_assignments(role_assignments, ner_roles)
        entity_groups = NerRoleAdapter.merge_groups(entity_groups, ner_groups)

        self.last_diagnostics = ParseDiagnostics(
            field_confidence={name: round(confidence.get(name, 0.0), 3) for name in FIELD_NAMES},
            field_sources=sources,
            document_number=document_number,
            document_number_confidence=round(document_number_confidence, 3),
            field_candidate_scores={
                "request_subject": [round(score, 3) for score in self._subject_candidate_scores[:5]]
            },
            field_regions=field_regions,
            document_type=document_type.value,
            document_type_confidence=document_type.confidence,
            document_type_reasons=document_type.reasons,
            layout_zone_counts=layout_zone_counts,
            field_results=field_results,
            role_assignments=[asdict(item) for item in role_assignments],
            entity_groups=[asdict(item) for item in entity_groups],
            ner_engine=active_ner_engine,
            ner_entity_counts=NerCandidateAdapter.entity_counts(ner_entities),
            ner_hybrid_entity_counts=NerCandidateAdapter.entity_counts(hybrid_ner_entities),
            ner_trusted_entity_counts=NerCandidateAdapter.entity_counts(trusted_ml_entities),
            ner_entities=serialize_entities(ner_entities),
            ner_metrics=ner_metrics,
            ner_comparison=ner_comparison,
            field_decisions=field_decisions,
            review_fields=review_fields,
            hybrid_changed_fields=hybrid_changed_fields,
            hybrid_actions=hybrid_actions,
            hybrid_candidate_counts={name: len(items) for name, items in all_candidates.items()},
            correction_memory_key=self.correction_memory.cache_key() if self.correction_memory else "none",
        )
        logger.debug("Распознано: %s; confidence=%s", result, self.last_diagnostics.field_confidence)
        return self._to_request(result)
