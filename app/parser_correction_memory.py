from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from collections import Counter
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.models import Request

logger = logging.getLogger(__name__)

LEARNABLE_FIELDS = (
    "from_department",
    "from_position",
    "from_person",
    "to_person",
)


def _key(value: str) -> str:
    value = (value or "").strip().lower().replace("ё", "е")
    value = re.sub(r"(?<=[а-я])0(?=[а-я])", "о", value)
    value = re.sub(r"[^а-яa-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _digits(value: str) -> tuple[str, ...]:
    return tuple(
        re.findall(
            r"(?<![А-ЯЁа-яёA-Za-z0-9])(\d+)(?![А-ЯЁа-яёA-Za-z0-9])",
            value or "",
        )
    )


def _field_context(field: str, context: dict[str, Any] | None) -> dict[str, str]:
    """Extract small, stable parser context without persisting document text."""
    context = context or {}
    output: dict[str, str] = {}
    document_type = str(context.get("document_type", "") or "").strip()
    if document_type:
        output["document_type"] = document_type

    field_sources = context.get("field_sources")
    if isinstance(field_sources, dict):
        source = str(field_sources.get(field, "") or "").strip()
        if source:
            output["source"] = source

    field_regions = context.get("field_regions")
    if isinstance(field_regions, dict):
        regions = field_regions.get(field)
        if isinstance(regions, list):
            for region in regions:
                if isinstance(region, dict):
                    zone = str(region.get("parser_zone", "") or "").strip()
                    if zone:
                        output["zone"] = zone
                        break

    return output


def _context_key(context: dict[str, str]) -> str:
    return "|".join(f"{key}={context[key]}" for key in sorted(context) if context.get(key))


class ParserCorrectionMemory:
    """Portable memory of user-confirmed parser corrections.

    v2 keeps the safe source->target rules from earlier versions and adds a
    compact context fingerprint (document type / page zone / extractor source).
    Raw OCR text is never stored. Exact corrections still work after one user
    confirmation; fuzzy reuse remains conservative and requires repeated
    confirmation, with context used only as a ranking signal.
    """

    VERSION = 2
    MAX_RULES_PER_FIELD = 400
    MAX_CONTEXTS_PER_RULE = 12

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data = self._load()

    @classmethod
    def for_data_dir(cls, data_dir: str | Path) -> "ParserCorrectionMemory":
        return cls(Path(data_dir) / "parser_corrections.json")

    def _empty(self) -> dict:
        return {
            "version": self.VERSION,
            "fields": {field: {"rules": []} for field in LEARNABLE_FIELDS},
        }

    def _normalize_rule(self, rule: dict[str, Any]) -> dict[str, Any]:
        normalized = {
            "source": str(rule.get("source", "") or ""),
            "source_key": str(rule.get("source_key", "") or ""),
            "target": str(rule.get("target", "") or ""),
            "count": max(1, int(rule.get("count", 1) or 1)),
            "contexts": [],
        }
        contexts = rule.get("contexts", [])
        if isinstance(contexts, list):
            for item in contexts:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key", "") or "").strip()
                if not key:
                    continue
                normalized["contexts"].append({
                    "key": key,
                    "count": max(1, int(item.get("count", 1) or 1)),
                    "document_type": str(item.get("document_type", "") or ""),
                    "zone": str(item.get("zone", "") or ""),
                    "source": str(item.get("source", "") or ""),
                })
        return normalized

    def _load(self) -> dict:
        if not self.path.is_file():
            return self._empty()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("Не удалось прочитать память исправлений parser: %s", error)
            return self._empty()
        if not isinstance(loaded, dict):
            return self._empty()

        version = int(loaded.get("version", 0) or 0)
        if version not in {1, self.VERSION}:
            return self._empty()

        migrated = self._empty()
        old_fields = loaded.get("fields", {})
        if isinstance(old_fields, dict):
            for field in LEARNABLE_FIELDS:
                field_data = old_fields.get(field, {})
                rules = field_data.get("rules", []) if isinstance(field_data, dict) else []
                if isinstance(rules, list):
                    migrated["fields"][field]["rules"] = [
                        self._normalize_rule(rule) for rule in rules if isinstance(rule, dict)
                    ]
        if version == 1:
            logger.info("Память исправлений parser v1 загружена в совместимом формате v2")
        return migrated

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(
            prefix="parser_corrections_",
            suffix=".tmp",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _rules(self, field: str) -> list[dict]:
        if field not in LEARNABLE_FIELDS:
            return []
        return self._data["fields"][field]["rules"]

    def _record_context(self, rule: dict[str, Any], field: str, context: dict[str, Any] | None) -> None:
        compact = _field_context(field, context)
        key = _context_key(compact)
        if not key:
            return
        contexts = rule.setdefault("contexts", [])
        for item in contexts:
            if str(item.get("key", "")) == key:
                item["count"] = max(1, int(item.get("count", 0))) + 1
                return
        contexts.append({
            "key": key,
            "count": 1,
            "document_type": compact.get("document_type", ""),
            "zone": compact.get("zone", ""),
            "source": compact.get("source", ""),
        })
        contexts.sort(key=lambda item: int(item.get("count", 0)), reverse=True)
        del contexts[self.MAX_CONTEXTS_PER_RULE :]

    def learn(
        self,
        before: Request,
        after: Request,
        context: dict[str, Any] | None = None,
    ) -> int:
        """Remember explicit edits and return how many correction rules changed."""
        changed = 0
        for field in LEARNABLE_FIELDS:
            source = str(getattr(before, field, "") or "").strip()
            target = str(getattr(after, field, "") or "").strip()
            source_key = _key(source)
            target_key = _key(target)
            if not source_key or not target_key or source_key == target_key:
                continue
            if _digits(source) != _digits(target):
                continue

            rules = self._rules(field)
            matched = None
            for rule in rules:
                if (
                    str(rule.get("source_key", "")) == source_key
                    and _key(str(rule.get("target", ""))) == target_key
                ):
                    matched = rule
                    break
            if matched is None:
                matched = {
                    "source": source,
                    "source_key": source_key,
                    "target": target,
                    "count": 1,
                    "contexts": [],
                }
                rules.append(matched)
            else:
                matched["source"] = source
                matched["target"] = target
                matched["count"] = max(1, int(matched.get("count", 0))) + 1
            self._record_context(matched, field, context)

            if len(rules) > self.MAX_RULES_PER_FIELD:
                rules.sort(key=lambda item: int(item.get("count", 0)), reverse=True)
                del rules[self.MAX_RULES_PER_FIELD :]
            changed += 1

        if changed:
            try:
                self._save()
            except OSError:
                logger.exception("Не удалось сохранить память исправлений parser")
        return changed

    def canonical_values(self, field: str) -> list[str]:
        if field not in LEARNABLE_FIELDS:
            return []
        counts: Counter[str] = Counter()
        display: dict[str, str] = {}
        for rule in self._rules(field):
            target = str(rule.get("target", "") or "").strip()
            target_key = _key(target)
            if not target_key:
                continue
            counts[target_key] += max(1, int(rule.get("count", 1)))
            display[target_key] = target
        return [display[key] for key, _ in counts.most_common()]

    def _canonical_counts(self, field: str) -> Counter[str]:
        counts: Counter[str] = Counter()
        for rule in self._rules(field):
            target = str(rule.get("target", "") or "").strip()
            target_key = _key(target)
            if target_key:
                counts[target_key] += max(1, int(rule.get("count", 1)))
        return counts

    def _context_support(self, field: str, target_key: str, context: dict[str, Any] | None) -> int:
        wanted = _field_context(field, context)
        if not wanted:
            return 0
        score = 0
        for rule in self._rules(field):
            if _key(str(rule.get("target", "") or "")) != target_key:
                continue
            for item in rule.get("contexts", []) or []:
                if not isinstance(item, dict):
                    continue
                local = 0
                if wanted.get("document_type") and item.get("document_type") == wanted["document_type"]:
                    local += 2
                if wanted.get("zone") and item.get("zone") == wanted["zone"]:
                    local += 2
                if wanted.get("source") and item.get("source") == wanted["source"]:
                    local += 1
                if local:
                    score += local * max(1, int(item.get("count", 1) or 1))
        return score


    def cache_key(self) -> str:
        """Stable short fingerprint used to invalidate parser cache after learning."""
        payload = json.dumps(self._data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def suggest_correction(
        self,
        field: str,
        value: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Return a provenance-rich correction candidate without mutating text.

        Exact user-confirmed source->target rules are allowed after one
        confirmation. Fuzzy canonical reuse keeps the existing conservative
        requirement of at least two confirmations.
        """
        source = str(value or "").strip()
        source_key = _key(source)
        if field not in LEARNABLE_FIELDS or not source_key:
            return None

        exact = [
            rule for rule in self._rules(field)
            if str(rule.get("source_key", "")) == source_key
        ]
        if exact:
            exact.sort(
                key=lambda item: (
                    self._context_support(field, _key(str(item.get("target", ""))), context),
                    int(item.get("count", 0)),
                ),
                reverse=True,
            )
            rule = exact[0]
            target = str(rule.get("target", source) or source).strip()
            target_key = _key(target)
            if not target_key or target_key == source_key:
                return None
            confirmations = max(1, int(rule.get("count", 1) or 1))
            context_support = self._context_support(field, target_key, context)
            confidence = min(0.99, 0.94 + min(0.03, 0.01 * (confirmations - 1)) + min(0.02, 0.002 * context_support))
            return {
                "target": target,
                "confidence": round(confidence, 3),
                "exact": True,
                "confirmations": confirmations,
                "context_support": context_support,
            }

        counts = self._canonical_counts(field)
        display = {_key(item): item for item in self.canonical_values(field)}
        best: dict[str, Any] | None = None
        source_digits = _digits(source)
        for target_key, confirmations in counts.items():
            if confirmations < 2 or not target_key or source_digits != _digits(display[target_key]):
                continue
            ratio_len = min(len(source_key), len(target_key)) / max(len(source_key), len(target_key))
            if ratio_len < 0.72:
                continue
            similarity = SequenceMatcher(None, source_key, target_key).ratio()
            if similarity < 0.94:
                continue
            context_support = self._context_support(field, target_key, context)
            score = similarity + min(0.02, context_support * 0.001)
            if best is None or score > float(best["score"]):
                best = {
                    "score": score,
                    "target": display[target_key],
                    "similarity": similarity,
                    "confirmations": confirmations,
                    "context_support": context_support,
                }
        if best is None:
            return None
        confidence = min(0.95, 0.88 + 0.05 * float(best["similarity"]) + min(0.02, 0.002 * int(best["context_support"])))
        return {
            "target": str(best["target"]),
            "confidence": round(confidence, 3),
            "exact": False,
            "confirmations": int(best["confirmations"]),
            "context_support": int(best["context_support"]),
        }

    def correct_value(
        self,
        field: str,
        value: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        source = str(value or "").strip()
        source_key = _key(source)
        if field not in LEARNABLE_FIELDS or not source_key:
            return source

        exact = [
            rule
            for rule in self._rules(field)
            if str(rule.get("source_key", "")) == source_key
        ]
        if exact:
            exact.sort(
                key=lambda item: (
                    self._context_support(field, _key(str(item.get("target", ""))), context),
                    int(item.get("count", 0)),
                ),
                reverse=True,
            )
            return str(exact[0].get("target", source) or source).strip()

        counts = self._canonical_counts(field)
        display = {_key(item): item for item in self.canonical_values(field)}
        best_score = 0.94
        best_context = -1
        best = source
        source_digits = _digits(source)
        for target_key, count in counts.items():
            if count < 2 or not target_key or source_digits != _digits(display[target_key]):
                continue
            ratio_len = min(len(source_key), len(target_key)) / max(len(source_key), len(target_key))
            if ratio_len < 0.72:
                continue
            score = SequenceMatcher(None, source_key, target_key).ratio()
            context_support = self._context_support(field, target_key, context)
            if score > best_score or (abs(score - best_score) < 0.005 and context_support > best_context):
                best_score = score
                best_context = context_support
                best = display[target_key]
        return best

    def apply(self, request: Request, context: dict[str, Any] | None = None) -> Request:
        corrected = replace(request)
        for field in LEARNABLE_FIELDS:
            value = getattr(corrected, field, "")
            if value:
                setattr(corrected, field, self.correct_value(field, value, context=context))
        return corrected

    def merge_suggestions(
        self,
        suggestions: dict[str, list[str]] | None,
    ) -> dict[str, list[str]]:
        merged = {key: list(values) for key, values in (suggestions or {}).items()}
        for field in LEARNABLE_FIELDS:
            seen: set[str] = set()
            values: list[str] = []
            for value in [*self.canonical_values(field), *merged.get(field, [])]:
                value = str(value or "").strip()
                key = _key(value)
                if not value or not key or key in seen:
                    continue
                seen.add(key)
                values.append(value)
            merged[field] = values
        return merged
