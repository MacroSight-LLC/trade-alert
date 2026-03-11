"""Finnhub + ROT sentiment normalizer (SSOT §7).

Transforms sentiment scores into ``sentiment_bull`` / ``sentiment_bear`` signals.
Respects SpamShield filtering: skips symbols flagged as spam.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any, Literal, cast

sys.path.insert(0, ".")
from models import Signal, Snapshot
from normalizers import clamp as _clamp

_log = logging.getLogger(__name__)

# ROT signal → (score, type, confidence) mapping (SSOT §7)

_ROT_MAP: dict[str, tuple[float, str, float]] = {
    "strong_bullish": (2.5, "sentiment_bull", 0.85),
    "bullish": (1.5, "sentiment_bull", 0.70),
    "bearish": (-1.5, "sentiment_bear", 0.70),
    "strong_bearish": (-2.5, "sentiment_bear", 0.85),
}


def normalize(raw_results: dict[str, Any], *, timeframe: str) -> list[Snapshot]:
    """Convert Finnhub + ROT sentiment output into Snapshots.

    Args:
        raw_results: Dict keyed by symbol. Each value contains:
            - finnhub_score (float): -1.0..+1.0 aggregate sentiment
            - rot_signal (str, optional): strong_bullish|bullish|neutral|
              bearish|strong_bearish
            - spam_filtered (bool): if True, skip symbol entirely
        timeframe: Candle timeframe, e.g. "15m".

    Returns:
        List of Snapshots.
    """
    snapshots: list[Snapshot] = []
    now = datetime.now(timezone.utc).isoformat()

    for symbol, data in raw_results.items():
        if data.get("spam_filtered"):
            _log.warning("Skipping %s — flagged as spam by SpamShield", symbol)
            continue

        signals: list[Signal] = []

        # Finnhub sentiment (SSOT §7)
        fh_score: float | None = data.get("finnhub_score")
        if fh_score is not None:
            score = _clamp(fh_score * 2.0, -2.0, 2.0)
            confidence = min(abs(fh_score) * 1.5, 1.0)

            signals.append(
                Signal(
                    source="finnhub",
                    type="sentiment_bull" if score > 0 else "sentiment_bear",
                    score=score,
                    confidence=confidence,
                    reason=f"Finnhub aggregate sentiment {fh_score:+.2f}",
                    raw=data,
                )
            )

        # ROT social signal (SSOT §7)
        rot_signal: str | None = data.get("rot_signal")
        if rot_signal and rot_signal in _ROT_MAP:
            rot_score, rot_type, rot_conf = _ROT_MAP[rot_signal]
            signals.append(
                Signal(
                    source="rot",
                    type=cast(
                        Literal[
                            "technical_trend",
                            "volume_spike",
                            "sentiment_bull",
                            "sentiment_bear",
                            "order_imbalance_long",
                            "order_imbalance_short",
                            "macro_risk_off",
                        ],
                        rot_type,
                    ),
                    score=rot_score,
                    confidence=rot_conf,
                    reason=f"ROT social signal: {rot_signal}",
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
            "finnhub_score": 0.7,
            "rot_signal": "strong_bullish",
            "spam_filtered": False,
        },
        "TSLA": {
            "finnhub_score": -0.4,
            "rot_signal": "bearish",
            "spam_filtered": False,
        },
        "SPAM": {
            "finnhub_score": 0.9,
            "rot_signal": "strong_bullish",
            "spam_filtered": True,
        },
    }
    import os

    if os.environ.get("MOCK_DATA"):
        results = normalize(mock, timeframe="15m")
        for r in results:
            print(r.model_dump())
        print(f"Sentiment normalizer: {len(results)} snapshots ✅")
