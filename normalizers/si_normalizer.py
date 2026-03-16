"""Short interest normalizer (SSOT §7).

Transforms short interest data into ``short_interest`` signals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, cast

from models import Signal, Snapshot
from normalizers import clamp, normalize_score, safe_float


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

        # SI% thresholds — score is always positive (short interest = potential energy)
        raw_score: float | None = None
        conf: float | None = None
        reason_parts: list[str] = []

        if si_pct >= 0.25:
            raw_score = 2.5
            conf = 0.85
            reason_parts.append(f"SI {si_pct:.0%} of float (very high)")
        elif si_pct >= 0.15:
            raw_score = 1.5
            conf = 0.70
            reason_parts.append(f"SI {si_pct:.0%} of float (elevated)")
        elif si_pct >= 0.10:
            raw_score = 0.8
            conf = 0.55
            reason_parts.append(f"SI {si_pct:.0%} of float (notable)")

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
                    score=normalize_score(raw_score, 0.0, 3.0),
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
