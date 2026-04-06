"""Short interest normalizer (SSOT §7).

Transforms short interest data into ``short_interest`` signals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, cast

from models import Signal, Snapshot
from normalizers import clamp, interpolate, safe_float

# Continuous SI% scoring breakpoints (fraction of float)
_SI_BREAKPOINTS: list[tuple[float, float, float]] = [
    (0.10, 1.0, 0.60),
    (0.15, 1.8, 0.72),
    (0.25, 2.5, 0.85),
    (0.40, 3.0, 0.95),
]


def normalize(raw_results: dict[str, Any], *, timeframe: str) -> list[Snapshot]:
    """Convert short interest data into Snapshots.

    Args:
        raw_results: Dict keyed by symbol. Each value may contain:
            - si_pct_float (float|None): Short interest as % of float (0.10 = 10%)
            - short_ratio (float|None): Days to cover
            - shares_short (int|None): Total shares short
        timeframe: Candle timeframe, e.g. "15m".

    Returns:
        List of Snapshots for symbols with notable short interest.
    """
    snapshots: list[Snapshot] = []
    now = datetime.now(timezone.utc).isoformat()

    for symbol, data in raw_results.items():
        signals: list[Signal] = []

        si_pct = data.get("si_pct_float")
        if si_pct is None:
            continue
        si_pct = safe_float(si_pct)

        short_ratio_val = safe_float(data.get("short_ratio"), default=0.0)

        # SI% scoring — continuous interpolation
        reason_parts: list[str] = []
        interp = interpolate(si_pct, _SI_BREAKPOINTS)

        if interp is not None:
            raw_score, conf = interp
            if si_pct >= 0.25:
                label = "very high"
            elif si_pct >= 0.15:
                label = "elevated"
            else:
                label = "notable"
            reason_parts.append(f"SI {si_pct:.0%} of float ({label})")
        else:
            raw_score = None
            conf = None

        if raw_score is not None:
            # Short ratio > 5 days = hard to cover → boost
            if short_ratio_val > 5.0:
                raw_score = clamp(raw_score + 0.5, 0.0, 3.0)
                reason_parts.append(f"days-to-cover {short_ratio_val:.1f}")

            shares_short = data.get("shares_short")
            if shares_short:
                reason_parts.append(f"{shares_short:,} shares short")

            signals.append(
                Signal(
                    source="yfinance",
                    type="short_interest",
                    score=raw_score,
                    confidence=conf,
                    reason="; ".join(reason_parts),
                    raw=data,
                )
            )

        if signals:
            snapshots.append(
                Snapshot(
                    symbol=symbol,
                    timeframe=cast(Literal["5m", "15m", "1h", "4h", "1D"], timeframe),
                    timestamp=now,
                    signals=signals,
                )
            )

    return snapshots
