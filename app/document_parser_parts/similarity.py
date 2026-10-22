"""Cheap, correctness-preserving bounds for fuzzy string comparisons."""
from __future__ import annotations

from difflib import SequenceMatcher


def ratio_upper_bound(a: str, b: str) -> float:
    """Return an upper bound for ``SequenceMatcher.ratio()`` without matching.

    The ratio cannot exceed twice the length of the shorter string divided by
    the combined length. This bound is intentionally conservative: it can skip
    comparisons that provably cannot reach a threshold, but never rejects a
    pair that could reach it.
    """
    len_a = len(a)
    len_b = len(b)
    total = len_a + len_b
    if total == 0:
        return 1.0
    return (2.0 * min(len_a, len_b)) / total


def ratio_if_above(a: str, b: str, floor: float) -> float:
    """Compute SequenceMatcher ratio only if it can strictly beat ``floor``."""
    if ratio_upper_bound(a, b) <= floor:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()
