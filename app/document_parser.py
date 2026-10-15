from __future__ import annotations

import logging
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
                result[field_name] = explicit[field_name]
                confidence[field_name] = 0.97 if field_name in spatial else 0.95
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

        document_number, document_number_confidence = self._extract_document_number(
            header_text + "\n" + "\n".join(text.splitlines()[:12]), blocks
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
                    confidence=0.97 if field_name in spatial else 0.95,
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
