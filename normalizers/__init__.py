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
