from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

# Tokens that immediately follow "Р\xa0" (or "Р " when NBSP was normalized to space)
# in double UTF-8 -> CP1251 mojibake for Russian letters А..Я, Ё, а..п.
_2X_AFTER_R_NBSP: tuple[str, ...] = (
    "вЂ\ufffd",
    "вЂ\x98",
    "вЂ\u02dc",
    "вЂ?",
    "вЂ™",
    "вЂњ",
    "вЂќ",
    "вЂў",
    "вЂ“",
    "вЂ”",
    "в„ў",
    "вЂє",
    "в„–",
    "вЂ",
    "пїЅ",
    "С’",
    "Рѓ",
    "С™",
    "Сљ",
    "Сњ",
    "С›",
    "Сџ",
    "В\xa0",
    "В ",
    "В\ufffd",
    "В\x98",
    "В\u02dc",
    "В?",
    "РЋ",
    "Сћ",
    "Р€",
    "В¤",
    "Тђ",
    "В¦",
    "В§",
    "РЃ",
    "В©",
    "Р„",
    "В«",
    "В¬",
    "В\xad",
    "В®",
    "Р‡",
    "В°",
    "В±",
    "Р†",
    "С–",
    "Т‘",
    "Вµ",
    "В¶",
    "В·",
    "С‘",
    "С”",
    "В»",
    "С\ufffd",
    "С\x98",
    "С\u02dc",
    "С?",
    "С",
    "Р…",
    "С•",
    "С—",
)

_2X_R_SPACE_RE = re.compile(
    "Р (?=" + "|".join(re.escape(tok) for tok in _2X_AFTER_R_NBSP) + ")"
)
_2X_TS_SPACE_RE = re.compile(r"РЎвЂ (?=[РЎ\s.,!?:;\"'»)\]-]|$)")
_2X_CAPITAL_R_SPACE_RE = re.compile(r"Р[\xa0 ]В (?=[РЎ\s.,!?:;\"'»)\]-]|$)")
_1X_CAPITAL_R_SPACE_RE = re.compile(
    r"Р (?=[ЃЂѓєѕіїјљњћќўџА-Яа-яЁё\x98\ufffd\u02dc])"
)

# Characteristic bigrams of UTF-8 decoded as CP1251, Latin-1/CP1252, or CP866
_MOJIBAKE_CP1251_RE = re.compile(
    r"(?:Р[\xa0\x98\ufffd\u02dcЃЂѓєѕіїјљњћќўџ‘’“”•–—™›°±Ііґµ¶·ё№є»јЅѕї¬\xad®¯¤¦§©«]|"
    r"С[\x98\ufffd\u02dcЃЂѓєѕіїјљњћќўџ‘’“”•–—™›‚„…†‡€‰‹ŒŽ\xa0]|"
    r"Р\xa0|РЎ|вЂ|РІР‚|пїЅ)"
)
_MOJIBAKE_LATIN1_RE = re.compile(r"(?:Ð[\x80-\xbf]|Ñ[\x80-\xbf]|Ã[\x80-\xbf])")
_MOJIBAKE_CP866_RE = re.compile(r"(?:╨[\x80-\xff\u2500-\u257f]|╤[\x80-\xff\u2500-\u257f])")


def _mojibake_score(text: str) -> int:
    if not text:
        return 0
    score = (
        len(_MOJIBAKE_CP1251_RE.findall(text)) * 2
        + len(_MOJIBAKE_LATIN1_RE.findall(text)) * 2
        + len(_MOJIBAKE_CP866_RE.findall(text)) * 2
    )
    score += len(_2X_R_SPACE_RE.findall(text)) * 3
    return score


def _restore_nbsp_for_cp1251(text: str) -> str:
    """Restore NBSP (\xa0) where editors or tools normalized it to regular space in 1x/2x mojibake."""
    text = _2X_TS_SPACE_RE.sub("РЎвЂ\xa0", text)
    text = _2X_CAPITAL_R_SPACE_RE.sub("Р\xa0В\xa0", text)
    text = _2X_R_SPACE_RE.sub("Р\xa0", text)
    text = _1X_CAPITAL_R_SPACE_RE.sub("Р\xa0", text)
    return text


def _repair_missing_0x98_in_utf8_bytes(raw: bytes) -> bytes:
    """Restore dropped 0x98 continuation bytes in a CP1251-decoded UTF-8 byte stream.

    In Windows-1251, 0x98 is the only unassigned byte in the UTF-8 continuation
    range 0x80..0xBF. When a tool decodes UTF-8 as CP1251 with errors='ignore'
    (or strips replacement characters), any UTF-8 lead sequence (0xC2, 0xD0, 0xD1,
    or 0xE2 0x80) not followed by 0x80..0xBF is deterministically missing 0x98.
    """
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        b = raw[i]
        if b in (0xC2, 0xD0, 0xD1):
            out.append(b)
            if i + 1 < n and 0x80 <= raw[i + 1] <= 0xBF:
                out.append(raw[i + 1])
                i += 2
            else:
                out.append(0x98)
                i += 1
        elif b == 0xE2 and i + 1 < n and raw[i + 1] == 0x80:
            out.append(0xE2)
            out.append(0x80)
            if i + 2 < n and 0x80 <= raw[i + 2] <= 0xBF:
                out.append(raw[i + 2])
                i += 3
            else:
                out.append(0x98)
                i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def _encode_cp1251_tolerant(text: str, *, repair_dropped_0x98: bool = False) -> bytes:
    """Encode a string back to raw bytes assuming it was decoded as Windows-1251."""
    prepared = _restore_nbsp_for_cp1251(text)
    prepared = re.sub(r"(?<=вЂ)\?", "\x98", prepared)
    prepared = re.sub(r"(?<=[РСВ])\?(?=[РЎВ\s.,!?:;\"'»)\]-]|$)", "\x98", prepared)

    out = bytearray()
    for ch in prepared:
        if ch in ("\x98", "\ufffd", "\u02dc"):
            out.append(0x98)
        else:
            try:
                out.extend(ch.encode("cp1251"))
            except UnicodeEncodeError:
                if ord(ch) < 256:
                    out.append(ord(ch))
                else:
                    raise
    raw = bytes(out)
    if repair_dropped_0x98:
        return _repair_missing_0x98_in_utf8_bytes(raw)
    return raw


def _unroll_single_segment(text: str) -> str:
    if not text or _mojibake_score(text) == 0:
        return text

    current = text
    for _ in range(3):
        base_score = _mojibake_score(current)
        if base_score == 0:
            break

        improved = False
        # 1. Try CP1251 -> UTF-8 (first without dropped-0x98 synthesis, then with it)
        for repair_dropped in (False, True):
            try:
                raw_bytes = _encode_cp1251_tolerant(current, repair_dropped_0x98=repair_dropped)
                candidate = raw_bytes.decode("utf-8")
                if _mojibake_score(candidate) < base_score:
                    current = candidate
                    improved = True
                    break
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
        if improved:
            continue

        # 2. Try Latin-1 / CP1252 / CP866 -> UTF-8
        for enc in ("latin1", "cp1252", "cp866"):
            try:
                candidate = current.encode(enc).decode("utf-8")
                if _mojibake_score(candidate) < base_score:
                    current = candidate
                    improved = True
                    break
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue

        if not improved:
            break

    return current


def repair_cyrillic_mojibake(text: str | None) -> str:
    """Detect and losslessly repair single, double, or triple Cyrillic mojibake.

    Safe to call on any string: clean Russian and English strings have a mojibake
    score of 0 and are returned untouched in O(n) time.
    """
    if not text:
        return ""
    value = str(text)
    if _mojibake_score(value) == 0:
        return value

    whole = _unroll_single_segment(value)
    if _mojibake_score(whole) == 0:
        return whole

    lines = value.splitlines(keepends=True)
    if len(lines) > 1:
        repaired_lines = [_unroll_single_segment(line) for line in lines]
        combined = "".join(repaired_lines)
        if _mojibake_score(combined) <= _mojibake_score(whole):
            return combined

    return whole


def decode_text_with_fallback(data: bytes) -> str:
    """Decode raw bytes using UTF-8 (with BOM), falling back to CP1251/CP866, and repair mojibake."""
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "cp866"):
        try:
            decoded = data.decode(encoding)
            return repair_cyrillic_mojibake(decoded)
        except UnicodeDecodeError:
            continue
    return repair_cyrillic_mojibake(data.decode("utf-8", errors="replace"))


def repair_json_strings(obj: Any) -> Any:
    """Recursively repair Cyrillic mojibake in all string values of a parsed JSON structure."""
    if isinstance(obj, str):
        return repair_cyrillic_mojibake(obj)
    if isinstance(obj, list):
        return [repair_json_strings(item) for item in obj]
    if isinstance(obj, dict):
        return {
            (repair_cyrillic_mojibake(k) if isinstance(k, str) else k): repair_json_strings(v)
            for k, v in obj.items()
        }
    return obj


def loads_json_with_fallback(source: bytes | str | Path) -> Any:
    """Parse JSON from bytes, string, or Path with encoding fallback and automatic mojibake repair."""
    if isinstance(source, Path):
        raw_bytes = source.read_bytes()
        text = decode_text_with_fallback(raw_bytes)
    elif isinstance(source, (bytes, bytearray)):
        text = decode_text_with_fallback(bytes(source))
    else:
        text = repair_cyrillic_mojibake(str(source))
        if text.startswith("\ufeff"):
            text = text.lstrip("\ufeff")

    parsed = json.loads(text)
    return repair_json_strings(parsed)