"""Polygon flow + crypto orderbook normalizer (SSOT §7).

Transforms volume multiples into ``volume_spike`` signals and
bid/ask imbalances into ``order_imbalance_long`` / ``order_imbalance_short``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any, Literal, cast

sys.path.insert(0, ".")
from models import Signal, Snapshot


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp *value* to [lo, hi]."""
    return max(lo, min(hi, value))


def normalize(raw_results: dict[str, Any], *, timeframe: str) -> list[Snapshot]:
    """Convert flow/orderbook MCP output into Snapshots.

    Args:
        raw_results: Dict keyed by symbol. Each value contains:
            - volume_multiple (float): current_volume / avg_20d_volume
            - imbalance (float, optional): bid-ask imbalance -1.0..+1.0
            - unusual_options (list[str], optional)
        timeframe: Candle timeframe, e.g. "15m".

    Returns:
        List of Snapshots, one per symbol with 1-2 signals.
    """
    snapshots: list[Snapshot] = []
    now = datetime.now(timezone.utc).isoformat()

    for symbol, data in raw_results.items():
        signals: list[Signal] = []
        vol_mult: float | None = data.get("volume_multiple")

        # Volume spike scoring (SSOT §7)
        if vol_mult is not None and vol_mult >= 1.5:
            if vol_mult >= 5.0:
                vol_score = 3.0
            elif vol_mult >= 3.0:
                vol_score = 2.5
            else:
                vol_score = 1.0

            unusual: list[str] = data.get("unusual_options", [])
            reason_parts = [f"volume {vol_mult:.1f}x avg"]
            if unusual:
                reason_parts.append(f"unusual options: {', '.join(unusual)}")

            signals.append(
                Signal(
                    source="polygon",
                    type="volume_spike",
                    score=vol_score,
                    confidence=min(vol_mult / 5.0, 1.0),
                    reason="; ".join(reason_parts),
                    raw=data,
                )
            )

        # Order-book imbalance scoring (SSOT §7 — crypto only)
        imbalance: float | None = data.get("imbalance")
        if imbalance is not None and imbalance != 0.0:
            if imbalance > 0:
                imb_score = _clamp(imbalance * 3.0, 0.0, 3.0)
                signals.append(
                    Signal(
                        source="crypto-orderbook",
                        type="order_imbalance_long",
                        score=imb_score,
                        confidence=min(abs(imbalance), 1.0),
                        reason=f"bid/ask imbalance {imbalance:+.2f}",
                        raw=data,
                    )
                )
            else:
                imb_score = _clamp(imbalance * 3.0, -3.0, 0.0)
                signals.append(
                    Signal(
                        source="crypto-orderbook",
                        type="order_imbalance_short",
                        score=imb_score,
                        confidence=min(abs(imbalance), 1.0),
                        reason=f"bid/ask imbalance {imbalance:+.2f}",
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
            "volume_multiple": 3.5,
            "unusual_options": ["$190c sweep"],
        },
        "BTC-USD": {
            "volume_multiple": 1.2,
            "imbalance": 0.65,
        },
        "ETH-USD": {
            "volume_multiple": 6.0,
            "imbalance": -0.4,
        },
    }
    import os

    if os.environ.get("MOCK_DATA"):
        results = normalize(mock, timeframe="15m")
        for r in results:
            print(r.model_dump())
        print(f"Flow normalizer: {len(results)} snapshots ✅")
