from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections import Counter
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

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


class ParserCorrectionMemory:
    """Small portable memory of user-confirmed parser corrections.

    The file lives in the application's ``data`` directory and therefore moves
    together with a portable installation.  It deliberately learns only stable
    identity-like fields. ``request_subject`` is excluded because a subject is
    document-specific and reusing it across unrelated letters would be unsafe.

    Exact source -> target corrections are safe to reuse after one explicit user
    correction. Fuzzy reuse is more conservative: a canonical target must have
    been confirmed at least twice before it may repair a new OCR variant.
    """

    VERSION = 1
    MAX_RULES_PER_FIELD = 400

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

    def _load(self) -> dict:
        if not self.path.is_file():
            return self._empty()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("Не удалось прочитать память исправлений parser: %s", error)
            return self._empty()
        if not isinstance(loaded, dict) or loaded.get("version") != self.VERSION:
            return self._empty()
        fields = loaded.setdefault("fields", {})
        for field in LEARNABLE_FIELDS:
            field_data = fields.setdefault(field, {"rules": []})
            if not isinstance(field_data, dict):
                fields[field] = {"rules": []}
                continue
            if not isinstance(field_data.get("rules"), list):
                field_data["rules"] = []
        return loaded

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

    def learn(self, before: Request, after: Request) -> int:
        """Remember explicit edits and return how many correction rules changed."""
        changed = 0
        for field in LEARNABLE_FIELDS:
            source = str(getattr(before, field, "") or "").strip()
            target = str(getattr(after, field, "") or "").strip()
            source_key = _key(source)
            target_key = _key(target)
            if not source_key or not target_key or source_key == target_key:
                continue
            # Do not learn a correction that changes structural numbers. This
            # protects department numbers and similar identifiers.
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
                rules.append(
                    {
                        "source": source,
                        "source_key": source_key,
                        "target": target,
                        "count": 1,
                    }
                )
            else:
                matched["source"] = source
                matched["target"] = target
                matched["count"] = max(1, int(matched.get("count", 0))) + 1

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

    def correct_value(self, field: str, value: str) -> str:
        source = str(value or "").strip()
        source_key = _key(source)
        if field not in LEARNABLE_FIELDS or not source_key:
            return source

        # Exact user-confirmed OCR mistake: reuse after the first correction.
        exact = [
            rule
            for rule in self._rules(field)
            if str(rule.get("source_key", "")) == source_key
        ]
        if exact:
            exact.sort(key=lambda item: int(item.get("count", 0)), reverse=True)
            return str(exact[0].get("target", source) or source).strip()

        # New OCR spelling: only trusted canonical values confirmed at least two
        # times may participate in fuzzy repair.
        counts = self._canonical_counts(field)
        display = { _key(value): value for value in self.canonical_values(field) }
        best_score = 0.94
        best = source
        source_digits = _digits(source)
        for target_key, count in counts.items():
            if count < 2 or not target_key or source_digits != _digits(display[target_key]):
                continue
            ratio_len = min(len(source_key), len(target_key)) / max(len(source_key), len(target_key))
            if ratio_len < 0.72:
                continue
            score = SequenceMatcher(None, source_key, target_key).ratio()
            if score > best_score:
                best_score = score
                best = display[target_key]
        return best

    def apply(self, request: Request) -> Request:
        corrected = replace(request)
        for field in LEARNABLE_FIELDS:
            value = getattr(corrected, field, "")
            if value:
                setattr(corrected, field, self.correct_value(field, value))
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
