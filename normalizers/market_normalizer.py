"""Market universe normalizer (SSOT §8).

Transforms price-change and insider-activity signals into scored Snapshots.
Used by collector-market to produce supplementary universe-quality signals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, cast

from models import Signal, Snapshot


def normalize(raw_results: dict[str, Any], *, timeframe: str) -> list[Snapshot]:
    """Convert market screening data into Snapshots.

    Args:
        raw_results: Dict keyed by symbol. Each value may contain:
            - price_change_24h (float): 24h price change percentage
            - insider_activity (str): "buying", "selling", or "none"
        timeframe: Candle timeframe, e.g. "15m".

    Returns:
        List of Snapshots for symbols with actionable signals.
    """
    snapshots: list[Snapshot] = []
    now = datetime.now(timezone.utc).isoformat()

    for symbol, data in raw_results.items():
        signals: list[Signal] = []

        change: float | None = data.get("price_change_24h")
        if change is not None:
            if change >= 10.0:
                signals.append(
                    Signal(
                        source="trading",
                        type="technical_trend",
                        score=2.5,
                        confidence=0.8,
                        reason=f"24h change {change:+.1f}%",
                        raw=data,
                    )
                )
            elif change >= 5.0:
                signals.append(
                    Signal(
                        source="trading",
                        type="technical_trend",
                        score=1.5,
                        confidence=0.7,
                        reason=f"24h change {change:+.1f}%",
                        raw=data,
                    )
                )
            elif change <= -10.0:
                signals.append(
                    Signal(
                        source="trading",
                        type="technical_trend",
                        score=-2.5,
                        confidence=0.8,
                        reason=f"24h change {change:+.1f}%",
                        raw=data,
                    )
                )
            elif change <= -5.0:
                signals.append(
                    Signal(
                        source="trading",
                        type="technical_trend",
                        score=-1.5,
                        confidence=0.7,
                        reason=f"24h change {change:+.1f}%",
                        raw=data,
                    )
                )

        insider: str | None = data.get("insider_activity")
        if insider == "buying":
            signals.append(
                Signal(
                    source="trading",
                    type="sentiment_bull",
                    score=1.5,
                    confidence=0.75,
                    reason="Insider buying activity",
                    raw=data,
                )
            )
        elif insider == "selling":
            signals.append(
                Signal(
                    source="trading",
                    type="sentiment_bear",
                    score=-1.5,
                    confidence=0.75,
                    reason="Insider selling activity",
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
