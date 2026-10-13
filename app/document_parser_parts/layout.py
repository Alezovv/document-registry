from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from app.document_parser_parts.constants import FIELD_NAMES, ORGANISATION_TERMS, POSITION_TERMS


class LayoutAnalysisMixin:
    """OCR block geometry, zones, field regions and confidence helpers."""

    @classmethod
    def _reconstruct_layout_text(cls, blocks: list[dict[str, Any]]) -> str:
        """Rebuild logical OCR lines from block geometry.

        PaddleOCR can return neighbouring fragments as separate blocks.  Joining
        fragments that share a baseline gives the parser much stronger context
        without performing another OCR pass.
        """
        prepared: list[dict[str, Any]] = []
        for block in blocks or []:
            text = re.sub(r"[ \t]+", " ", str(block.get("text", "")).strip())
            if not text:
                continue
            try:
                page = int(block.get("page_index", 0) or 0)
                x1 = float(block.get("x1", 0) or 0)
                x2 = float(block.get("x2", x1) or x1)
                y1 = float(block.get("y1", 0) or 0)
                y2 = float(block.get("y2", y1) or y1)
                center_y = float(block.get("center_y", (y1 + y2) / 2.0) or 0)
                height = max(1.0, float(block.get("height", y2 - y1 or 1.0) or 1.0))
            except (TypeError, ValueError):
                continue
            prepared.append({
                "text": text, "page": page, "x1": x1, "x2": x2,
                "center_y": center_y, "height": height,
            })
        if not prepared:
            return ""

        output: list[str] = []
        pages = sorted({int(item["page"]) for item in prepared})
        for page in pages:
            page_blocks = sorted(
                (item for item in prepared if int(item["page"]) == page),
                key=lambda item: (float(item["center_y"]), float(item["x1"])),
            )
            lines: list[dict[str, Any]] = []
            for block in page_blocks:
                best_line = None
                best_distance = None
                for line in lines:
                    tolerance = max(float(line["height"]), float(block["height"])) * 0.72
                    distance = abs(float(line["center_y"]) - float(block["center_y"]))
                    if distance <= tolerance and (best_distance is None or distance < best_distance):
                        best_line = line
                        best_distance = distance
                if best_line is None:
                    lines.append({
                        "center_y": float(block["center_y"]),
                        "height": float(block["height"]),
                        "blocks": [block],
                    })
                else:
                    best_line["blocks"].append(block)
                    centers = [float(item["center_y"]) for item in best_line["blocks"]]
                    best_line["center_y"] = sum(centers) / len(centers)
                    best_line["height"] = max(float(item["height"]) for item in best_line["blocks"])

            for line in sorted(lines, key=lambda item: float(item["center_y"])):
                line_blocks = sorted(line["blocks"], key=lambda item: float(item["x1"]))
                line_text = " ".join(str(item["text"]) for item in line_blocks)
                line_text = re.sub(r"\s+", " ", line_text).strip()
                if line_text:
                    output.append(line_text)
        return "\n".join(output)

    @classmethod
    def _layout_zones(cls, text: str, blocks: list[dict[str, Any]]) -> dict[str, str]:
        """Build conservative page zones from OCR coordinates.

        Coordinates are used only when a usable geometry is present. Otherwise the
        legacy line-based split remains the fallback, which preserves v1.13 behavior.
        """
        header, body, footer = cls._split_zones(text, blocks)
        fallback_lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        signature_start = max(0, len(fallback_lines) - max(6, int(len(fallback_lines) * 0.45)))
        zones = {
            "header": header,
            "body": body,
            "footer": footer,
            "signature": "\n".join(fallback_lines[signature_start:]),
            "incoming": "",
            "addressee": "",
            "content": body,
        }
        if not blocks:
            return zones

        usable: list[tuple[dict[str, Any], float, float, float, float, float, float]] = []
        for block in blocks:
            try:
                x1 = float(block.get("x1", 0))
                y1 = float(block.get("y1", 0))
                x2 = float(block.get("x2", x1))
                y2 = float(block.get("y2", y1))
                page_w = float(block.get("page_width") or 0)
                page_h = float(block.get("page_height") or 0)
            except (TypeError, ValueError):
                continue
            if page_w <= 0:
                page_w = max((float(b.get("x2", 0) or 0) for b in blocks), default=0.0)
            if page_h <= 0:
                page_h = max((float(b.get("y2", 0) or 0) for b in blocks), default=0.0)
            if page_w <= 0 or page_h <= 0:
                continue
            usable.append((block, x1, y1, x2, y2, page_w, page_h))

        if not usable:
            return zones

        first_page_index = min(int(item[0].get("page_index", 0) or 0) for item in usable)
        last_page_index = max(int(item[0].get("page_index", 0) or 0) for item in usable)

        def collect(
            predicate, *, first_page_only: bool = False, last_page_only: bool = False
        ) -> str:
            selected = []
            for item in usable:
                block, x1, y1, x2, y2, page_w, page_h = item
                block_page = int(block.get("page_index", 0) or 0)
                if first_page_only and block_page != first_page_index:
                    continue
                if last_page_only and block_page != last_page_index:
                    continue
                if predicate(x1, y1, x2, y2, page_w, page_h):
                    selected.append(block)
            selected.sort(
                key=lambda b: (
                    int(b.get("page_index", 0) or 0),
                    float(b.get("y1", 0) or 0),
                    float(b.get("x1", 0) or 0),
                )
            )
            return cls._reconstruct_layout_text(selected)

        # Incoming stamps are normally in the upper-left quadrant. Keep the zone
        # deliberately wider because handwritten date blocks may sit beside "Вх.".
        zones["incoming"] = collect(
            lambda x1, y1, x2, y2, w, h: y1 <= h * 0.38 and x1 <= w * 0.58,
            first_page_only=True,
        )
        # Addressee blocks are typically upper-right. This text is not accepted as
        # to_person by itself; semantic context is still required downstream.
        zones["addressee"] = collect(
            lambda x1, y1, x2, y2, w, h: y1 <= h * 0.48 and x1 >= w * 0.42,
            first_page_only=True,
        )
        # Main semantic content is wider than the historical body zone.  Some
        # official templates start the request just below a large letterhead, at
        # ~20% of page height, and others continue almost to the signature block.
        zones["content"] = collect(
            lambda x1, y1, x2, y2, w, h: h * 0.18 <= y1 <= h * 0.84,
        ) or body
        zones["signature"] = collect(
            lambda x1, y1, x2, y2, w, h: y1 >= h * 0.56,
            last_page_only=True,
        ) or zones["signature"]
        return zones

    @classmethod
    def _locate_field_regions(
        cls,
        result: dict[str, str],
        blocks: list[dict[str, Any]],
        sources: dict[str, str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Map final parser fields back to normalized PDF regions.

        This is deliberately a post-processing step: extraction rules remain
        deterministic, while the UI receives enough provenance to highlight
        the text that contributed to a field. Filename-only fallbacks have no
        PDF region and are therefore omitted.
        """
        output: dict[str, list[dict[str, Any]]] = {}
        if not blocks:
            return output

        prepared = []
        for block in blocks:
            text = str(block.get("text", "")).strip()
            key = cls._label_key(text)
            try:
                x1 = float(block.get("x1", 0.0))
                y1 = float(block.get("y1", 0.0))
                x2 = float(block.get("x2", x1))
                y2 = float(block.get("y2", y1))
                width = float(block.get("page_width", 0.0) or 0.0)
                height = float(block.get("page_height", 0.0) or 0.0)
                page_index = int(block.get("page_index", 0) or 0)
            except (TypeError, ValueError):
                continue
            if not key or width <= 0 or height <= 0:
                continue
            prepared.append((block, key, x1, y1, x2, y2, width, height, page_index))

        for field_name in FIELD_NAMES:
            value = (result.get(field_name) or "").strip()
            if not value or sources.get(field_name) in {"filename", "rejected", "none"}:
                continue
            value_key = cls._label_key(value)
            value_tokens = {t for t in value_key.split() if len(t) >= 3}
            ranked = []
            for item in prepared:
                block, block_key, *_rest = item
                block_tokens = {t for t in block_key.split() if len(t) >= 3}
                if not block_tokens:
                    continue
                if field_name == "received_date":
                    score = 1.0 if value in cls._all_dates(str(block.get("text", ""))) else 0.0
                else:
                    overlap = len(value_tokens & block_tokens) / max(1, min(len(value_tokens), len(block_tokens)))
                    similarity = SequenceMatcher(None, value_key, block_key).ratio()
                    contained = 1.0 if value_key in block_key or block_key in value_key else 0.0
                    score = max(contained, overlap * 0.9, similarity * 0.82)
                threshold = 0.42 if field_name == "request_subject" else 0.62
                if score >= threshold:
                    ranked.append((score, item))

            ranked.sort(key=lambda pair: (-pair[0], pair[1][8], pair[1][3], pair[1][2]))
            max_regions = 10 if field_name == "request_subject" else 3
            selected = []
            seen = set()
            for score, item in ranked[:max_regions * 2]:
                _block, _key, x1, y1, x2, y2, width, height, page_index = item
                rect = (
                    max(0.0, min(1.0, x1 / width)),
                    max(0.0, min(1.0, y1 / height)),
                    max(0.0, min(1.0, x2 / width)),
                    max(0.0, min(1.0, y2 / height)),
                )
                signature = (page_index,) + tuple(round(v, 5) for v in rect)
                if signature in seen:
                    continue
                seen.add(signature)
                selected.append({
                    "page_index": page_index,
                    "rect_norm": [round(v, 6) for v in rect],
                    "score": round(float(score), 3),
                })
                if len(selected) >= max_regions:
                    break
            if selected:
                output[field_name] = selected
        return output

    @classmethod
    def _heuristic_confidence(
        cls,
        field_name: str,
        value: str,
        blocks: list[dict[str, Any]],
        zones: dict[str, str],
    ) -> float:
        if not value:
            return 0.0
        base = {
            "received_date": 0.82,
            "from_department": 0.76,
            "from_position": 0.72,
            "from_person": 0.76,
            "to_person": 0.74,
            "request_subject": 0.72,
        }.get(field_name, 0.65)

        if field_name == "received_date" and value in cls._all_dates(zones.get("incoming", "")):
            base += 0.10
        if field_name == "from_department" and cls._contains_dictionary_term(value, ORGANISATION_TERMS):
            # Archive sender departments usually carry stable agency/medical/
            # administration markers.  On the calibration corpus these values
            # were systematically landing just below the autofill boundary even
            # when OCR confidence was high, so give this independent semantic
            # evidence a slightly stronger (still bounded) contribution.
            base += 0.09
        if field_name == "from_position" and cls._contains_dictionary_term(value, POSITION_TERMS):
            base += 0.08
        if field_name in {"from_person", "to_person"} and cls._looks_like_person_value(value):
            base += 0.07
        if field_name == "request_subject" and len(value) >= 24:
            base += 0.05

        # OCR confidence nudges the score but never dominates semantic/layout rules.
        block_scores = []
        value_key = cls._label_key(value)
        for block in blocks or []:
            text_key = cls._label_key(str(block.get("text", "")))
            if not text_key or not value_key:
                continue
            if value_key in text_key or text_key in value_key:
                try:
                    score = block.get("confidence")
                    if score is not None:
                        block_scores.append(float(score))
                except (TypeError, ValueError):
                    pass
        if block_scores:
            avg = sum(block_scores) / len(block_scores)
            base += (avg - 0.70) * 0.12
        return max(0.0, min(0.94, base))

    @classmethod
    def _extract_document_number(
        cls, header: str, blocks: list[dict[str, Any]]
    ) -> tuple[str, float]:
        patterns = (
            r"(?i)(?:исх\.?\s*)?№\s*([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9/\-]{1,30})",
            r"(?i)\bN[oо]\.?\s*([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9/\-]{1,30})",
        )
        for pattern in patterns:
            match = re.search(pattern, header or "")
            if match:
                value = match.group(1).strip(" .,:;—–-")
                if value:
                    return value, 0.86
        # A split "№" + value is common in OCR forms. Use spatial proximity only.
        for block in blocks or []:
            if not re.search(r"(?i)(?:^|\s)(?:№|N[oо])(?:\s|$)", str(block.get("text", ""))):
                continue
            try:
                lx2 = float(block.get("x2", 0))
                lcy = float(block.get("center_y", 0))
                lh = max(1.0, float(block.get("height", 1)))
            except (TypeError, ValueError):
                continue
            candidates = []
            for candidate in blocks:
                if candidate is block:
                    continue
                text = str(candidate.get("text", "")).strip()
                if not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9/\-]{1,30}", text):
                    continue
                try:
                    cx1 = float(candidate.get("x1", 0))
                    ccy = float(candidate.get("center_y", 0))
                except (TypeError, ValueError):
                    continue
                if cx1 >= lx2 - 2 and abs(ccy - lcy) <= max(14.0, lh * 1.4):
                    candidates.append((cx1 - lx2 + abs(ccy - lcy), text))
            if candidates:
                return min(candidates)[1], 0.82
        return "", 0.0

    @classmethod
    def _all_dates(cls, text: str) -> set[str]:
        return set(cls._all_normalized_dates(text or ""))

    @classmethod
    def _contains_dictionary_term(cls, value: str, terms: tuple[str, ...]) -> bool:
        key = cls._label_key(value)
        return any(cls._label_key(term) in key for term in terms)

    @classmethod
    def _fuzzy_dictionary_term(
        cls, text: str, terms: tuple[str, ...], *, threshold: float = 0.84
    ) -> str:
        words = re.findall(r"[А-ЯЁа-яёA-Za-z]{3,}", text or "")
        if not words:
            return ""
        normalized_words = [cls._label_key(word) for word in words]
        best = (0.0, "")
        for term in terms:
            term_key = cls._label_key(term)
            term_words = term_key.split()
            width = max(1, len(term_words))
            for index in range(0, max(1, len(normalized_words) - width + 1)):
                phrase = " ".join(normalized_words[index:index + width])
                score = SequenceMatcher(None, phrase, term_key).ratio()
                if score > best[0]:
                    best = (score, term)
        return best[1] if best[0] >= threshold else ""

    @staticmethod
    def _split_zones(text: str, blocks: list[dict[str, Any]]) -> tuple[str, str, str]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        total = len(lines)
        if total == 0:
            return "", "", ""

        if blocks:
            try:
                page_indices = [int(b.get("page_index", 0) or 0) for b in blocks]
                first_page = min(page_indices) if page_indices else 0
                last_page = max(page_indices) if page_indices else first_page
            except (TypeError, ValueError):
                first_page = last_page = 0
            first_blocks = [b for b in blocks if int(b.get("page_index", 0) or 0) == first_page]
            last_blocks = [b for b in blocks if int(b.get("page_index", 0) or 0) == last_page]
            max_y = max((float(b.get("y2", 0) or 0) for b in first_blocks), default=0.0)
            last_max_y = max((float(b.get("y2", 0) or 0) for b in last_blocks), default=max_y)
            if max_y > 0:
                header_blocks = [b for b in first_blocks if float(
                    b.get("y1", 0) or 0) < (max_y * 0.35)]
                footer_blocks = [b for b in last_blocks if float(
                    b.get("y1", 0) or 0) > (last_max_y * 0.70)]
                body_blocks = []
                for b in blocks:
                    page_height = float(b.get("page_height", 0) or 0)
                    if page_height <= 0:
                        page_height = max_y
                    y1 = float(b.get("y1", 0) or 0)
                    if (page_height * 0.28) <= y1 <= (page_height * 0.78):
                        body_blocks.append(b)

                sort_key = lambda x: (int(x.get("page_index", 0) or 0), float(x.get("y1", 0) or 0), float(x.get("x1", 0) or 0))
                h_text = "\n".join(str(b.get("text", "")) for b in sorted(header_blocks, key=sort_key))
                b_text = "\n".join(str(b.get("text", "")) for b in sorted(body_blocks, key=sort_key))
                f_text = "\n".join(str(b.get("text", "")) for b in sorted(footer_blocks, key=sort_key))
                return h_text, b_text, f_text

        h_end = max(1, int(total * 0.35))
        f_start = max(h_end, int(total * 0.70))
        return "\n".join(lines[:h_end]), "\n".join(lines[h_end:f_start]), "\n".join(lines[f_start:])
