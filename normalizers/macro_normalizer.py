"""FRED bundle macro-regime normalizer (SSOT §7).

Transforms VIX, yield-curve, and risk-on/off data into
``macro_risk_off`` signals on a single ``__GLOBAL_MACRO__`` snapshot.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Literal, cast

from models import Signal, Snapshot

VIX_EXTREME_THRESHOLD: float = float(os.getenv("VIX_EXTREME_THRESHOLD", "35.0"))
VIX_ELEVATED_THRESHOLD: float = float(os.getenv("VIX_ELEVATED_THRESHOLD", "25.0"))


def _safe_float(value: float | None) -> float | None:
    """Return value if finite, else None."""
    if value is None:
        return None
    import math
    if not isinstance(value, (int, float)) or math.isnan(value) or math.isinf(value):
        return None
    return float(value)


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

    # VIX thresholds (SSOT §7)
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
            signals.append(
                Signal(
                    source="fred",
                    type="macro_risk_off",
                    score=2.0,
                    confidence=0.85,
                    reason=f"VIX elevated at {vix:.1f}",
                    raw=raw_results,
                )
            )

    # Yield curve inversion (SSOT §7)
    if curve_slope is not None and curve_slope < -50:
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
    mock = {
        "vix": 28.5,
        "yield_curve_slope": -75.0,
        "fed_funds_rate": 4.5,
        "risk_on": False,
    }
    import os

    if os.environ.get("MOCK_DATA"):
        results = normalize(mock, timeframe="15m")
        for r in results:
            print(r.model_dump())
        print(f"Macro normalizer: {len(results)} snapshots ✅")
