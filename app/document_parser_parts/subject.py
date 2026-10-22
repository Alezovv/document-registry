from __future__ import annotations

import re
from difflib import SequenceMatcher
from app.document_parser_parts.similarity import ratio_upper_bound

from app.document_parser_parts.constants import (
    SUBJECT_ACTION_VERBS,
    SUBJECT_CONTEXT_ANCHORS,
    SUBJECT_MAX_CHARS,
    SUBJECT_MAX_LINES,
    SUBJECT_OBJECT_TERMS,
    SUBJECT_REQUEST_ANCHORS,
    SUBJECT_STOP_MARKERS,
)


class SubjectExtractionMixin:
    """Deterministic request-subject extraction and ranking."""

    def _extract_subject_with_scores(self, text: str) -> str:
        value, scores = self._extract_subject_ranked(text)
        if scores:
            self._subject_candidate_scores.extend(scores)
            self._subject_candidate_scores = sorted(
                self._subject_candidate_scores, reverse=True
            )[:8]
        return value

    @classmethod
    def _extract_subject(cls, text: str) -> str:
        """Compatibility wrapper returning only the best subject candidate."""
        return cls._extract_subject_ranked(text)[0]

    @classmethod
    def _extract_subject_ranked(cls, text: str) -> tuple[str, list[float]]:
        """Return the best semantic subject and candidate scores.

        The parser deliberately stays deterministic and lightweight.  Instead of
        feeding the whole OCR text to another model, it ranks short local text
        windows using request verbs, business-language anchors, layout-safe noise
        filters and object words.  This gives ``request_subject`` a useful fallback
        for letters that do not literally contain ``Прошу предоставить``.
        """
        if not text:
            return "", []

        raw_lines = []
        for raw in text.splitlines():
            line = re.sub(r"\s+", " ", raw or "").strip()
            if not line or re.match(r"^---\s*Страница\s+\d+\s*---$", line, re.IGNORECASE):
                continue
            raw_lines.append(line)
        if not raw_lines:
            return "", []

        candidates: list[tuple[float, str]] = []

        def add(raw_value: str, base: float) -> None:
            value = cls._clean_subject(raw_value)
            if not value or cls._subject_is_noise(value):
                return
            score = base + cls._subject_quality_score(value)
            # De-duplicate near-identical windows while keeping the stronger one.
            key = cls._label_key(value)
            for index, (old_score, old_value) in enumerate(candidates):
                old_key = cls._label_key(old_value)
                if key == old_key or (
                    min(len(key), len(old_key)) >= 18
                    and ratio_upper_bound(key, old_key) >= 0.93
                    and SequenceMatcher(None, key, old_key).ratio() >= 0.93
                ):
                    if score > old_score:
                        candidates[index] = (score, value)
                    return
            candidates.append((score, value))

        request_pattern = re.compile(
            r"(?i)\b(?:прошу|просим|просит|предлагаю|ходатайствую)\b"
        )
        action_pattern = re.compile(
            r"(?i)\b(?:" + "|".join(map(re.escape, SUBJECT_ACTION_VERBS)) + r")\w*\b"
        )
        context_pattern = re.compile(
            r"(?i)\b(?:" + "|".join(map(re.escape, SUBJECT_CONTEXT_ANCHORS)) + r")\b"
        )

        def is_boundary(line: str) -> bool:
            normalized = cls._label_key(line)
            if any(normalized.startswith(cls._label_key(marker)) for marker in SUBJECT_STOP_MARKERS):
                return True
            return bool(re.match(
                r"(?i)^(?:исх\.?|вх\.?|№|дата\b|тел\.?|телефон|исп\.?|"
                r"исполнитель|e-mail|email|подпись\b)",
                line,
            ))

        def collect_lines(start: int) -> str:
            parts: list[str] = []
            total = 0
            for offset, candidate_line in enumerate(raw_lines[start:start + SUBJECT_MAX_LINES]):
                if offset and is_boundary(candidate_line):
                    break
                if offset and cls._looks_like_person_value(candidate_line):
                    # A lone FIO after a complete sentence is normally a signature.
                    joined = " ".join(parts).rstrip()
                    if joined.endswith((".", "!", "?")):
                        break
                projected = total + len(candidate_line) + (1 if parts else 0)
                if projected > SUBJECT_MAX_CHARS:
                    remaining = SUBJECT_MAX_CHARS - total - (1 if parts else 0)
                    if remaining > 20:
                        parts.append(candidate_line[:remaining].rstrip())
                    break
                parts.append(candidate_line)
                total = projected
            return " ".join(parts)

        # A semantic thought is commonly split over many OCR lines. Collect the
        # paragraph from the anchor until a signature/contact boundary instead
        # of hard-cutting it to two or three lines.
        for index, line in enumerate(raw_lines):
            paragraph = collect_lines(index)
            two_line = " ".join(raw_lines[index:index + 2])

            if request_pattern.search(line):
                add(paragraph, 104.0)
                continue
            if action_pattern.search(line):
                add(paragraph, 94.0)
            if context_pattern.search(line):
                add(paragraph, 84.0)

            # A useful generic fallback for informational letters with no explicit
            # request verb (e.g. incident reports or explanatory responses).
            if 18 <= len(line) <= SUBJECT_MAX_CHARS and not cls._subject_is_noise(line):
                add(two_line if len(two_line) <= SUBJECT_MAX_CHARS else line, 44.0)

        # Some PDFs expose a whole paragraph as one native-text block. Sentence
        # groups rescue those documents while still preserving several connected
        # sentences when the subject is more than one sentence long.
        flat = re.sub(r"\s+", " ", " ".join(raw_lines)).strip()
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", flat) if part.strip()]
        for index, sentence in enumerate(sentences):
            if len(sentence) < 18:
                continue
            group = " ".join(sentences[index:index + 3])[:SUBJECT_MAX_CHARS].rstrip()
            if request_pattern.search(sentence):
                add(group, 102.0)
            elif action_pattern.search(sentence):
                add(group, 92.0)
            elif context_pattern.search(sentence):
                add(group, 82.0)

        if not candidates:
            return "", []
        candidates.sort(key=lambda item: (item[0], -abs(len(item[1]) - 120)), reverse=True)
        return candidates[0][1], [item[0] for item in candidates[:8]]

    @classmethod
    def _subject_quality_score(cls, value: str) -> float:
        low = cls._label_key(value)
        score = 0.0
        length = len(value)
        if 28 <= length <= 260:
            score += 12.0
        elif 18 <= length <= 700:
            score += 8.0
        elif length <= SUBJECT_MAX_CHARS:
            score += 4.0
        else:
            score -= 12.0

        if any(anchor in low for anchor in SUBJECT_REQUEST_ANCHORS):
            score += 8.0
        if any(re.search(rf"(?i)\b{re.escape(verb)}\w*\b", value) for verb in SUBJECT_ACTION_VERBS):
            score += 10.0
        if any(term in low for term in SUBJECT_OBJECT_TERMS):
            score += 8.0
        if re.search(r"(?i)\b(?:в отношении|по факту|по вопросу|в связи с|для проведения)\b", value):
            score += 6.0
        if re.search(r"https?://|www\.|\S+@\S+", value, re.IGNORECASE):
            score -= 20.0
        return score

    @classmethod
    def _subject_is_noise(cls, value: str) -> bool:
        value = re.sub(r"\s+", " ", (value or "")).strip(" .,:;—–-")
        if len(value) < 12 or sum(ch.isalpha() for ch in value) < 8:
            return True
        low = cls._label_key(value)
        if not low:
            return True
        if re.match(
            r"(?i)^(?:исх\.?|вх\.?|№|дата\b|тел\.?|телефон|исп\.?|"
            r"исполнитель|e-mail|email|адрес\b|кому\b|от кого\b|"
            r"начальник\b|заместитель\b|директор\b|главный врач\b|"
            r"судья\b|мировой судья\b|подпись\b|уважаем\w*)",
            value,
        ):
            return True
        if cls._looks_like_person_value(value):
            return True
        # Pure letterheads must not become the subject, but organisation names
        # inside a real sentence are allowed.
        if cls._looks_like_organisation(value) and not (
            any(re.search(rf"(?i)\b{re.escape(verb)}\w*\b", value) for verb in SUBJECT_ACTION_VERBS)
            or any(anchor in low for anchor in SUBJECT_CONTEXT_ANCHORS)
        ):
            words = re.findall(r"[А-ЯЁа-яёA-Za-z]+", value)
            if len(words) <= 12:
                return True
        if re.fullmatch(r"[\d\s№./\-–—:()]+", value):
            return True
        return False
