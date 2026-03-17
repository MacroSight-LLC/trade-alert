"""Finnhub + ROT sentiment normalizer (SSOT §7).

Transforms sentiment scores into ``sentiment_bull`` / ``sentiment_bear`` signals.
Respects SpamShield filtering: skips symbols flagged as spam.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal, cast

from models import Signal, Snapshot
from normalizers import clamp as _clamp
from normalizers import safe_float

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
            fh_score = safe_float(fh_score)
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
                            "options_flow",
                            "insider_activity",
                            "relative_strength",
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

        # ROT options flow → options_flow signal (SSOT §7)
        rot_flow_items: list[dict] = data.get("rot_options_flow", [])
        for flow_item in rot_flow_items:
            premium = flow_item.get("premium", 0)
            contracts = flow_item.get("contracts", 0)
            sweep_type = flow_item.get("sweep_type", "")
            strike = flow_item.get("strike", "")

            # Score by magnitude: small sweep → 1.0, medium → 2.0, large → 2.5
            if contracts >= 500 or premium >= 1_000_000:
                flow_score = 2.5
                flow_conf = 0.85
            elif contracts >= 200 or premium >= 500_000:
                flow_score = 2.0
                flow_conf = 0.75
            elif contracts >= 50 or premium >= 100_000:
                flow_score = 1.0
                flow_conf = 0.60
            else:
                continue

            # Determine direction from sweep type (call = bullish, put = bearish)
            if "put" in sweep_type.lower():
                flow_score = -flow_score

            signals.append(
                Signal(
                    source="rot",
                    type="options_flow",
                    score=flow_score,
                    confidence=flow_conf,
                    reason=f"Options sweep: {sweep_type} {strike} {contracts} contracts ${premium:,.0f}",
                    raw=flow_item,
                )
            )
            break  # One options_flow signal per symbol

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
    sample = {
        "AAPL": {
            "finnhub_score": 0.7,
            "rot_signal": "strong_bullish",
            "spam_filtered": False,
        },
    }
    results = normalize(sample, timeframe="15m")
    for r in results:
        print(r.model_dump())
    print(f"Sentiment normalizer: {len(results)} snapshots ✅")
