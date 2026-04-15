"""FRED bundle macro-regime normalizer (SSOT §7).

Transforms VIX, yield-curve, and risk-on/off data into
``macro_risk_off`` signals on a single ``__GLOBAL_MACRO__`` snapshot.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any, Literal, cast

from models import Signal, Snapshot

VIX_EXTREME_THRESHOLD: float = float(os.getenv("VIX_EXTREME_THRESHOLD", "35.0"))
VIX_ELEVATED_THRESHOLD: float = float(os.getenv("VIX_ELEVATED_THRESHOLD", "25.0"))
CURVE_INVERSION_THRESHOLD: float = float(os.getenv("CURVE_INVERSION_THRESHOLD", "-50.0"))

from normalizers import safe_float  # noqa: E402


def _safe_float(value: Any) -> float | None:
    """Return *value* as float if finite, else ``None``."""
    if value is None:
        return None
    result = safe_float(value, default=float("nan"))
    return None if math.isnan(result) else result


def normalize(raw_results: dict[str, Any], *, timeframe: str) -> list[Snapshot]:
    """Convert FRED bundle output into a global macro Snapshot.

    Args:
        raw_results: Single dict (NOT keyed by symbol) containing:
            - vix (float): VIX level
            - yield_curve_slope (float): 10Y-2Y spread in bps
            - fed_funds_rate (float)
            - risk_on (bool): pre-computed flag from FRED MCP
        timeframe: Candle timeframe, e.g. "15m".

    Returns:
        List with one ``__GLOBAL_MACRO__`` Snapshot if any risk-off signals
        triggered, otherwise empty list.
    """
    signals: list[Signal] = []
    vix: float | None = _safe_float(raw_results.get("vix"))
    curve_slope: float | None = _safe_float(raw_results.get("yield_curve_slope"))
    risk_on: bool | None = raw_results.get("risk_on")

    # VIX scoring (SSOT §7) — continuous instead of binary thresholds
    if vix is not None:
        if vix > VIX_EXTREME_THRESHOLD:
            signals.append(
                Signal(
                    source="fred",
                    type="macro_risk_off",
                    score=3.0,
                    confidence=0.95,
                    reason=f"VIX extreme at {vix:.1f}",
                    raw=raw_results,
                )
            )
        elif vix > VIX_ELEVATED_THRESHOLD:
            # Interpolate between elevated and extreme
            t = (vix - VIX_ELEVATED_THRESHOLD) / (VIX_EXTREME_THRESHOLD - VIX_ELEVATED_THRESHOLD)
            score = 2.0 + t * 1.0  # 2.0 → 3.0
            conf = 0.85 + t * 0.10  # 0.85 → 0.95
            signals.append(
                Signal(
                    source="fred",
                    type="macro_risk_off",
                    score=round(score, 2),
                    confidence=round(conf, 2),
                    reason=f"VIX elevated at {vix:.1f}",
                    raw=raw_results,
                )
            )
        elif vix <= 15.0:
            # Calm VIX = risk-on environment; emit negative risk_off
            # so decision engine can see "macro is supportive"
            signals.append(
                Signal(
                    source="fred",
                    type="macro_risk_off",
                    score=-1.0,
                    confidence=0.70,
                    reason=f"VIX calm at {vix:.1f} (risk-on)",
                    raw=raw_results,
                )
            )
        elif vix <= 20.0:
            # VIX 15-20: mildly elevated but still risk-on; score negative
            # so _signal_directional_score(-score) returns +0.65 for LONG.
            t = (vix - 15.0) / 5.0  # 0.0 at VIX=15 → 1.0 at VIX=20
            score = -1.0 + t * 0.35  # -1.0 → -0.65
            signals.append(
                Signal(
                    source="fred",
                    type="macro_risk_off",
                    score=round(score, 2),
                    confidence=0.60,
                    reason=f"VIX moderate at {vix:.1f} (mild risk-on)",
                    raw=raw_results,
                )
            )
        elif vix <= 25.0:
            # VIX 20-25: transitional zone — market not clearly risk-on or
            # risk-off.  Emit a small negative score so the family registers
            # as present with low conviction; won't dominate directional count.
            t = (vix - 20.0) / 5.0  # 0.0 at VIX=20 → 1.0 at VIX=25
            score = -0.65 + t * 0.65  # -0.65 → 0.0 (approaches threshold)
            signals.append(
                Signal(
                    source="fred",
                    type="macro_risk_off",
                    score=round(score, 2),
                    confidence=0.50,
                    reason=f"VIX transitional at {vix:.1f} (near-neutral)",
                    raw=raw_results,
                )
            )

    # Yield curve inversion (SSOT §7)
    if curve_slope is not None and curve_slope < CURVE_INVERSION_THRESHOLD:
        signals.append(
            Signal(
                source="fred",
                type="macro_risk_off",
                score=1.5,
                confidence=0.8,
                reason=f"Yield curve inverted: {curve_slope:.0f}bps",
                raw=raw_results,
            )
        )

    # Pre-computed risk-on flag
    if risk_on is False:
        signals.append(
            Signal(
                source="fred",
                type="macro_risk_off",
                score=1.0,
                confidence=0.7,
                reason="FRED risk-on flag is False",
                raw=raw_results,
            )
        )

    # When no risk signals triggered, skip the snapshot entirely.
    # The decision engine and merger already handle "no macro data"
    # gracefully (macro staleness guard + default risk_on=True).
    # Emitting zero-score/zero-confidence signals wastes Redis space
    # and muddies the signal-type count used by SA and diversity scoring.
    if not signals:
        return []

    now = datetime.now(timezone.utc).isoformat()
    return [
        Snapshot(
            symbol="__GLOBAL_MACRO__",
            timeframe=cast(Literal["5m", "15m", "1h", "4h", "1D"], timeframe),
            timestamp=now,
            signals=signals,
        )
    ]


if __name__ == "__main__":
    sample = {
        "vix": 28.5,
        "yield_curve_slope": -75.0,
        "fed_funds_rate": 4.5,
        "risk_on": False,
    }
    results = normalize(sample, timeframe="15m")
    for r in results:
        print(r.model_dump())
    print(f"Macro normalizer: {len(results)} snapshots ✅")
