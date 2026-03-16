"""Normalizer utilities shared across all signal normalizers."""

from __future__ import annotations

import math


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp *value* to [lo, hi]."""
    return max(lo, min(hi, value))


def safe_float(value: float | None, default: float = 0.0) -> float:
    """Return *value* if finite, otherwise *default*."""
    if value is None or not isinstance(value, (int, float)):
        return default
    if math.isnan(value) or math.isinf(value):
        return default
    return float(value)


def normalize_score(score: float, lo: float, hi: float) -> float:
    """Map *score* from [lo, hi] range to [-1.0, +1.0].

    For asymmetric ranges (e.g. [1, 3] with no negatives), the
    output is shifted so mid maps to 0.  For symmetric ranges
    (e.g. [-3, +3]), this is a simple linear rescale.

    Args:
        score: Raw score value.
        lo: Minimum of the source range.
        hi: Maximum of the source range.

    Returns:
        Score mapped to [-1.0, +1.0].
    """
    span = hi - lo
    if span == 0:
        return 0.0
    return clamp(2.0 * (score - lo) / span - 1.0, -1.0, 1.0)
