"""Snapshot parsing and deterministic sources_agree reconciliation."""

from __future__ import annotations

import json
import os
from typing import Any

_TYPE_FAMILY: dict[str, str] = {
    "technical_trend": "trend",
    "relative_strength": "trend",
    "price_forecast": "trend",
    "volume_spike": "volume",
    "sentiment_bull": "sentiment",
    "sentiment_bear": "sentiment",
    "options_flow": "flow",
    "insider_activity": "events",
    "catalyst_event": "events",
    "macro_risk_off": "macro",
    "short_interest": "positioning",
}

# Minimum mean family score to count a family as directionally aligned.
_SA_FAMILY_MIN_SCORE: float = float(os.environ.get("SA_FAMILY_MIN_SCORE", "0.25"))
_SA_INCLUDE_MACRO_CONTEXT: bool = os.environ.get("SA_INCLUDE_MACRO_CONTEXT", "1") == "1"
_SA_MACRO_CONTEXT_SCORE: float = float(os.environ.get("SA_MACRO_CONTEXT_SCORE", "0.50"))
_SA_FORECAST_CONFIRM_BONUS_ENABLED: bool = os.environ.get("SA_FORECAST_CONFIRM_BONUS_ENABLED", "1") == "1"
_SA_FORECAST_BONUS_THRESHOLD: float = float(os.environ.get("SA_FORECAST_BONUS_THRESHOLD", "0.80"))


def _parse_snapshots(snapshots_json: str) -> list[dict[str, Any]]:
    """Parse snapshot JSON once for reuse by downstream helpers.

    Args:
        snapshots_json: JSON string of Snapshot dicts.

    Returns:
        Parsed list of snapshot dicts, or empty list on error.
    """
    try:
        parsed = json.loads(snapshots_json)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _build_snap_type_index(snaps: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Build per-symbol signal-type index from parsed snapshots.

    Args:
        snaps: List of parsed Snapshot dicts.

    Returns:
        Mapping of symbol → set of distinct signal type strings.
    """
    snap_types: dict[str, set[str]] = {}
    for s in snaps:
        sym = s.get("symbol", "")
        for sig in s.get("signals", []):
            snap_types.setdefault(sym, set()).add(sig.get("type", ""))
    return snap_types


def _signal_directional_score(sig_type: str, score: float) -> float:
    if sig_type == "sentiment_bear":
        # sentiment_bear scores are always positive; negate so they count for SHORT.
        return -abs(score)
    if sig_type == "macro_risk_off":
        # macro_risk_off uses SIGNED scores: positive = risk-off (bearish),
        # negative = risk-on (bullish).  Negate score (not abs) to preserve
        # this convention: risk-on (-1.0) → +1.0 (counts for LONG);
        # risk-off (+2.5) → -2.5 (counts for SHORT).  Using -abs() was a bug
        # that treated all macro signals as bearish regardless of regime.
        return -score
    return score


def _build_symbol_family_scores(snaps: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    family_accum: dict[str, dict[str, list[float]]] = {}
    for snap in snaps:
        symbol = str(snap.get("symbol", ""))
        if not symbol:
            continue
        for sig in snap.get("signals", []):
            if not isinstance(sig, dict):
                continue
            sig_type = str(sig.get("type", ""))
            family = _TYPE_FAMILY.get(sig_type)
            if family is None:
                continue
            try:
                raw_score = float(sig.get("score", 0.0))
            except (TypeError, ValueError):
                continue
            score = _signal_directional_score(sig_type, raw_score)
            family_accum.setdefault(symbol, {}).setdefault(family, []).append(score)

    family_means: dict[str, dict[str, float]] = {}
    for symbol, fam_map in family_accum.items():
        family_means[symbol] = {
            family: (sum(scores) / len(scores)) for family, scores in fam_map.items() if scores
        }
    return family_means


def _aligned_family_count(family_scores: dict[str, float], direction: str) -> int:
    if direction == "LONG":
        return sum(1 for score in family_scores.values() if score >= _SA_FAMILY_MIN_SCORE)
    if direction == "SHORT":
        return sum(1 for score in family_scores.values() if score <= -_SA_FAMILY_MIN_SCORE)
    long_count = sum(1 for score in family_scores.values() if score >= _SA_FAMILY_MIN_SCORE)
    short_count = sum(1 for score in family_scores.values() if score <= -_SA_FAMILY_MIN_SCORE)
    return max(long_count, short_count)
