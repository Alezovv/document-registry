from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, TYPE_CHECKING

from app.parser_core import CandidateResolver, FieldCandidate, FieldNormalizer

if TYPE_CHECKING:
    from app.parser_correction_memory import ParserCorrectionMemory


@dataclass(slots=True)
class HybridChoice:
    field: str
    value: str
    confidence: float
    source: str
    changed: bool = False
    reason: str = "baseline"
    winner_value: str = ""
    winner_confidence: float = 0.0
    winner_source: str = ""
    candidate_count: int = 0
    memory_candidate_count: int = 0


class HybridFieldResolver:
    """Conservative production fusion for parser candidates.

    v1.21 moves candidate fusion from diagnostics into the actual parser result,
    while preserving hard safety rules:

    * explicit labelled values stay protected unless a strong, previously
      confirmed correction-memory rule says otherwise;
    * NER can fill an empty field only when the candidate has independent
      support and crosses the field-specific NER threshold;
    * deterministic alternatives may replace a weak baseline only when the
      calibrated winner is clearly stronger;
    * user correction history is represented as a normal candidate with
      provenance instead of being applied as an opaque post-processing step.
    """

    PROTECTED_SOURCES = {"label/layout", "label"}
    DETERMINISTIC_SOURCES = {
        "label/layout", "label", "layout/heuristic", "filename", "correction_memory"
    }
    MIN_SWITCH_GAIN = 0.06
    MEMORY_OVERRIDE_MIN = 0.94
    # Proxy-gold A/B on the real archive showed that hybrid subject switches
    # harmed reviewed values (0 improvements / 4 regressions). Keep the
    # deterministic subject extractor authoritative; NER and other alternatives
    # remain visible in diagnostics but cannot rewrite a populated subject.
    BASELINE_LOCKED_FIELDS = {"request_subject"}

    @classmethod
    def add_correction_memory_candidates(
        cls,
        field_name: str,
        candidates: Iterable[FieldCandidate],
        memory: "ParserCorrectionMemory | None",
        context: dict[str, Any] | None = None,
    ) -> list[FieldCandidate]:
        materialized = list(candidates)
        if memory is None:
            return materialized

        additions: list[FieldCandidate] = []
        seen: set[tuple[str, str]] = set()
        for candidate in materialized:
            suggestion = memory.suggest_correction(field_name, candidate.value, context=context)
            if not suggestion:
                continue
            target = str(suggestion.get("target", "") or "").strip()
            if not target:
                continue
            source_key = FieldNormalizer.comparison_key(field_name, candidate.value)
            target_key = FieldNormalizer.comparison_key(field_name, target)
            if not target_key or source_key == target_key:
                continue
            marker = (target_key, candidate.zone)
            if marker in seen:
                continue
            seen.add(marker)
            confidence = float(suggestion.get("confidence", 0.0) or 0.0)
            additions.append(FieldCandidate(
                field=field_name,
                value=target,
                confidence=confidence,
                source="correction_memory",
                zone=candidate.zone,
                page_index=candidate.page_index,
                source_blocks=list(candidate.source_blocks),
                extractors=["correction_memory", *candidate.extractors],
                warnings=list(candidate.warnings),
                metadata={
                    "memory_exact": bool(suggestion.get("exact", False)),
                    "memory_confirmations": int(suggestion.get("confirmations", 0) or 0),
                    "memory_context_support": int(suggestion.get("context_support", 0) or 0),
                    "memory_source": candidate.source,
                },
            ))
        return [*materialized, *additions]

    @staticmethod
    def _independent_support(candidate: FieldCandidate) -> int:
        support = candidate.metadata.get("support_sources") or []
        return len({str(item) for item in support if item})

    @classmethod
    def _safe_empty_fill(cls, field_name: str, winner: FieldCandidate) -> bool:
        if winner.source != "ner":
            return winner.confidence >= CandidateResolver.DECISION_THRESHOLDS.get(
                field_name, (0.86, 0.68, 0.52)
            )[0]
        return (
            winner.confidence >= CandidateResolver.ner_autofill_threshold(field_name)
            and cls._independent_support(winner) >= 2
        )

    @classmethod
    def choose(
        cls,
        field_name: str,
        candidates: Iterable[FieldCandidate],
        *,
        baseline_value: str,
        baseline_confidence: float,
        baseline_source: str,
    ) -> HybridChoice:
        ranked = CandidateResolver.rank(field_name, candidates)
        memory_count = sum(1 for item in ranked if item.source == "correction_memory")
        if not ranked:
            return HybridChoice(
                field=field_name,
                value=str(baseline_value or ""),
                confidence=float(baseline_confidence or 0.0),
                source=baseline_source or "none",
                candidate_count=0,
                memory_candidate_count=0,
            )

        winner = ranked[0]
        baseline_value = str(baseline_value or "").strip()
        baseline_confidence = float(baseline_confidence or 0.0)
        baseline_source = baseline_source or "none"

        base = HybridChoice(
            field=field_name,
            value=baseline_value,
            confidence=baseline_confidence,
            source=baseline_source,
            winner_value=winner.value,
            winner_confidence=winner.confidence,
            winner_source=winner.source,
            candidate_count=len(ranked),
            memory_candidate_count=memory_count,
        )

        if not baseline_value:
            if field_name in cls.BASELINE_LOCKED_FIELDS:
                # Do not resurrect a subject that the deterministic parser
                # intentionally rejected as low quality. This keeps the proven
                # v1.21 baseline semantics for request_subject.
                base.reason = "field_locked_empty"
                return base
            if cls._safe_empty_fill(field_name, winner):
                base.value = winner.value
                base.confidence = winner.confidence
                base.source = winner.source
                base.changed = True
                base.reason = "hybrid_fill_empty"
            else:
                base.reason = "empty_not_confident_enough"
            return base

        if CandidateResolver.values_compatible(field_name, baseline_value, winner.value):
            base.confidence = max(baseline_confidence, winner.confidence)
            base.reason = "hybrid_confirmed_baseline"
            return base

        if winner.source == "correction_memory":
            exact = bool(winner.metadata.get("memory_exact", False))
            if exact and winner.confidence >= cls.MEMORY_OVERRIDE_MIN:
                base.value = winner.value
                base.confidence = winner.confidence
                base.source = "correction_memory"
                base.changed = True
                base.reason = "confirmed_correction_memory"
            else:
                base.reason = "memory_not_strong_enough"
            return base

        if baseline_source in cls.PROTECTED_SOURCES:
            base.reason = "protected_label"
            return base

        if field_name in cls.BASELINE_LOCKED_FIELDS:
            base.reason = "field_locked_to_baseline"
            return base

        # ML is advisory for an already populated deterministic field.
        if winner.source == "ner":
            base.reason = "advisory_ner_only"
            return base

        resolved = CandidateResolver.resolve(field_name, ranked)
        if (
            winner.source in cls.DETERMINISTIC_SOURCES
            and resolved.decision == "autofill"
            and winner.confidence >= baseline_confidence + cls.MIN_SWITCH_GAIN
        ):
            base.value = winner.value
            base.confidence = winner.confidence
            base.source = winner.source
            base.changed = True
            base.reason = "stronger_deterministic_candidate"
            return base

        base.reason = "baseline_retained"
        return base
