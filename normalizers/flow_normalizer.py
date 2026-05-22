"""Flow normalizer (SSOT §7).

Transforms volume multiples (Polygon), options chain data (yfinance),
and intraday volume acceleration (Alpaca) into flow signals.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast

from models import Signal, Snapshot
from normalizers import interpolate, safe_float

# Continuous volume-spike scoring breakpoints (multiples of avg)
_VOL_BREAKPOINTS: list[tuple[float, float, float]] = [
    (1.5, 1.0, 0.30),
    (3.0, 2.0, 0.60),
    (5.0, 3.0, 1.00),
]


def normalize(raw_results: dict[str, Any], *, timeframe: str) -> list[Snapshot]:
    """Convert flow MCP output into Snapshots.

    Args:
        raw_results: Dict keyed by symbol. Each value contains:
            - volume_multiple (float): current_volume / avg_20d_volume
            - unusual_options (list[str], optional)
        timeframe: Candle timeframe, e.g. "15m".

    Returns:
        List of Snapshots, one per symbol with volume_spike signals.
    """
    snapshots: list[Snapshot] = []
    now = datetime.now(UTC).isoformat()

    for symbol, data in raw_results.items():
        signals: list[Signal] = []
        vol_mult: float | None = data.get("volume_multiple")

        # Volume spike scoring — continuous interpolation (SSOT §7)
        if vol_mult is not None:
            vol_mult = safe_float(vol_mult)
            result = interpolate(vol_mult, _VOL_BREAKPOINTS)
            if result is not None:
                vol_score, vol_conf = result

                unusual: list[str] = data.get("unusual_options", [])
                reason_parts = [f"volume {vol_mult:.1f}x avg"]
                if unusual:
                    reason_parts.append(f"unusual options: {', '.join(unusual)}")

                signals.append(
                    Signal(
                        source="polygon",
                        type="volume_spike",
                        score=vol_score,
                        confidence=vol_conf,
                        reason="; ".join(reason_parts),
                        raw=data,
                    )
                )

        # Enhanced options flow from yfinance chain data.
        # NOTE: This uses call/put ratio as a *complementary* signal to the
        # spec-compliant sweep-size thresholds (≥500 contracts / ≥$1M premium)
        # which are implemented in sentiment_normalizer via ROT options flow.
        # yfinance chain data lacks per-sweep contract/premium breakdown, so
        # ratio-based scoring is the best available heuristic here.
        call_put_ratio: float | None = data.get("call_put_ratio")
        if call_put_ratio is not None:
            call_put_ratio = safe_float(call_put_ratio)
            # Skip invalid ratios: negative or zero (NaN/division-by-zero)
            if call_put_ratio > 0:
                unusual_oi: bool = data.get("unusual_oi", False)

                if call_put_ratio > 2.0:
                    flow_score = 1.5
                    if unusual_oi:
                        flow_score = 2.0
                    signals.append(
                        Signal(
                            source="yfinance",
                            type="options_flow",
                            score=flow_score,
                            confidence=0.70,
                            reason=f"Call/put ratio {call_put_ratio:.1f}x"
                            + (" (unusual OI)" if unusual_oi else ""),
                            raw=data,
                        )
                    )
                elif call_put_ratio < 0.5:
                    flow_score = -1.5
                    if unusual_oi:
                        flow_score = -2.0
                    signals.append(
                        Signal(
                            source="yfinance",
                            type="options_flow",
                            score=flow_score,
                            confidence=0.70,
                            reason=f"Put-heavy ratio {call_put_ratio:.2f}x"
                            + (" (unusual OI)" if unusual_oi else ""),
                            raw=data,
                        )
                    )

        # Enhanced intraday volume from Alpaca
        vol_accel: float | None = data.get("volume_acceleration")
        if vol_accel is not None:
            vol_accel = safe_float(vol_accel)
            if vol_accel >= 2.0:
                accel_score = min(vol_accel / 2.0, 3.0)
                signals.append(
                    Signal(
                        source="alpaca",
                        type="volume_spike",
                        score=accel_score,
                        confidence=min(vol_accel / 5.0, 1.0),
                        reason=f"Intraday volume acceleration {vol_accel:.1f}x (last 3 bars vs prior 3)",
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
    sample = {
        "AAPL": {
            "volume_multiple": 3.5,
            "unusual_options": ["$190c sweep"],
        },
    }
    results = normalize(sample, timeframe="15m")
    for r in results:
        print(r.model_dump())
    print(f"Flow normalizer: {len(results)} snapshots ✅")
