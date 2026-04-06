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


def interpolate(
    value: float,
    breakpoints: list[tuple[float, float, float]],
) -> tuple[float, float] | None:
    """Linearly interpolate score and confidence from tier breakpoints.

    Each breakpoint is ``(threshold, score, confidence)``.  Breakpoints
    must be ordered ascending by threshold.  For *value* between two
    breakpoints the output is linearly blended.  Below the lowest
    breakpoint returns ``None`` (no signal).

    Args:
        value: Raw input metric (e.g. abs price change, SI%, volume mult).
        breakpoints: Ascending list of ``(threshold, score, confidence)``
            tuples defining the piecewise-linear mapping.

    Returns:
        ``(score, confidence)`` tuple or ``None`` if *value* is below the
        lowest breakpoint.
    """
    if not breakpoints or value < breakpoints[0][0]:
        return None

    # At or above the highest breakpoint → return its values
    if value >= breakpoints[-1][0]:
        return breakpoints[-1][1], breakpoints[-1][2]

    # Find the surrounding pair and interpolate
    for i in range(len(breakpoints) - 1):
        lo_thresh, lo_score, lo_conf = breakpoints[i]
        hi_thresh, hi_score, hi_conf = breakpoints[i + 1]
        if lo_thresh <= value < hi_thresh:
            t = (value - lo_thresh) / (hi_thresh - lo_thresh)
            return (
                lo_score + t * (hi_score - lo_score),
                lo_conf + t * (hi_conf - lo_conf),
            )

    # Fallback (shouldn't reach here)
    return breakpoints[-1][1], breakpoints[-1][2]


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
