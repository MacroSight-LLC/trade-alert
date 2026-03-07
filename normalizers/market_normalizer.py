"""CoinGecko + trading-mcp market universe normalizer (SSOT §7).

Transforms price-change and insider-activity data into signals.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any, Literal, cast

sys.path.insert(0, ".")
from models import Signal, Snapshot


def normalize(raw_results: dict[str, Any], *, timeframe: str) -> list[Snapshot]:
    """Convert market universe data into Snapshots.

    Args:
        raw_results: Dict keyed by symbol. Each value contains:
            - price_change_24h (float): percent change
            - market_cap_rank (int, optional)
            - insider_activity (str, optional): "buying"|"selling"|"none"
        timeframe: Candle timeframe, e.g. "15m".

    Returns:
        List of Snapshots.
    """
    snapshots: list[Snapshot] = []
    now = datetime.now(timezone.utc).isoformat()

    for symbol, data in raw_results.items():
        signals: list[Signal] = []
        pct: float | None = data.get("price_change_24h")

        # Price-change scoring tiers
        if pct is not None:
            if pct > 10.0:
                score, conf = 2.5, 0.75
            elif pct > 5.0:
                score, conf = 1.5, 0.6
            elif pct < -10.0:
                score, conf = -2.5, 0.75
            elif pct < -5.0:
                score, conf = -1.5, 0.6
            else:
                score, conf = 0.0, 0.0  # below threshold

            if score != 0.0:
                signals.append(
                    Signal(
                        source="market-universe",
                        type="technical_trend",
                        score=score,
                        confidence=conf,
                        reason=f"24h price change {pct:+.1f}%",
                        raw=data,
                    )
                )

        # Insider activity
        insider: str | None = data.get("insider_activity")
        if insider == "buying":
            signals.append(
                Signal(
                    source="trading-mcp",
                    type="sentiment_bull",
                    score=1.0,
                    confidence=0.9,
                    reason="insider buying detected",
                    raw=data,
                )
            )
        elif insider == "selling":
            signals.append(
                Signal(
                    source="trading-mcp",
                    type="sentiment_bear",
                    score=-1.0,
                    confidence=0.9,
                    reason="insider selling detected",
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


if __name__ == "__main__":
    mock = {
        "AAPL": {
            "price_change_24h": 6.5,
            "market_cap_rank": 1,
            "insider_activity": "buying",
        },
        "TSLA": {
            "price_change_24h": -12.0,
            "market_cap_rank": 8,
            "insider_activity": "selling",
        },
        "MSFT": {
            "price_change_24h": 1.2,
            "insider_activity": "none",
        },
    }
    import os

    if os.environ.get("MOCK_DATA"):
        results = normalize(mock, timeframe="15m")
        for r in results:
            print(r.model_dump())
        print(f"Market normalizer: {len(results)} snapshots ✅")
