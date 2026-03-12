"""TradingView technical analysis normalizer (SSOT §7).

Transforms raw TradingView MCP results into ``technical_trend`` Signals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, cast

from models import Signal, Snapshot
from normalizers import safe_float


def normalize(raw_results: dict[str, Any], *, timeframe: str) -> list[Snapshot]:
    """Convert TradingView MCP output into Snapshots with technical_trend signals.

    Args:
        raw_results: Dict keyed by symbol. Each value contains:
            - rating (float | None): -3..+3 directional strength
            - patterns (list[str]): e.g. ["trend_change", "bb_squeeze"]
            - indicators (dict): rsi, bb_width, bb_squeeze (bool)
        timeframe: Candle timeframe, e.g. "15m".

    Returns:
        List of Snapshots, one per valid symbol.
    """
    snapshots: list[Snapshot] = []
    now = datetime.now(timezone.utc).isoformat()

    for symbol, data in raw_results.items():
        rating = data.get("rating")
        indicators: dict[str, Any] = data.get("indicators", {})

        # Fallback: derive rating from BB position when RSI-based rating is absent
        if rating is None:
            bb_width = indicators.get("bb_width")
            bb_squeeze = indicators.get("bb_squeeze", False)
            if bb_width is not None or bb_squeeze:
                # bb_width here is |bb_position - 0.5| * 2, range [0, 1]
                # Map: 0 (at lower band) → -1.5, 0.5 (mid) → 0, 1 (at upper) → +1.5
                raw_bb = indicators.get("bb_width", 0.0)
                # Reconstruct bb_position: width = |pos - 0.5| * 2
                # We can't recover direction from width alone; use squeeze as neutral signal
                rating = 0.0  # neutral fallback from BB-only
            else:
                continue

        rating = safe_float(rating)
        if rating == 0.0 and data.get("rating") not in (0, 0.0, None):
            continue

        patterns: list[str] = data.get("patterns", [])

        # Build reason from patterns and indicators (SSOT §7)
        reasons: list[str] = []
        if indicators.get("bb_squeeze"):
            reasons.append("BB squeeze detected")
        if "trend_change" in patterns:
            reasons.append("trend change pattern")
        if not reasons:
            reasons.append(f"TA rating {rating:+.1f}")

        score = max(-3.0, min(3.0, float(rating)))
        confidence = min(abs(rating) / 3.0, 1.0)

        signal = Signal(
            source="tradingview",
            type="technical_trend",
            score=score,
            confidence=confidence,
            reason="; ".join(reasons),
            raw=data,
        )
        snapshots.append(
            Snapshot(
                symbol=symbol,
                timeframe=cast(Literal["5m", "15m", "1h", "4h", "1D"], timeframe),
                timestamp=now,
                signals=[signal],
            )
        )

    return snapshots


if __name__ == "__main__":
    sample = {
        "AAPL": {
            "rating": 2.1,
            "patterns": ["trend_change"],
            "indicators": {"rsi": 62, "bb_width": 0.04, "bb_squeeze": True},
        },
    }
    results = normalize(sample, timeframe="15m")
    for r in results:
        print(r.model_dump())
    print(f"TA normalizer: {len(results)} snapshots ✅")
