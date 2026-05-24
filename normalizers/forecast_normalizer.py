"""TimesFM price-forecast normalizer (SSOT §7 extension).

Transforms raw TimesFM MCP forecast results into ``price_forecast`` Signals.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast

from models import Signal, Snapshot
from normalizers import clamp, safe_float


def normalize(raw_results: dict[str, Any], *, timeframe: str) -> list[Snapshot]:
    """Convert TimesFM MCP output into Snapshots with price_forecast signals.

    Args:
        raw_results: Dict keyed by symbol. Each value contains:
            - median_forecast (list[float]): predicted prices per horizon bar.
            - quantiles (dict): ``{"p10": [...], "p50": [...], "p90": [...]}``.
            - current_price (float): last observed close price.
            - horizon_bars (int): number of forecast bars.
            - direction_pct (float): % change from current_price to median endpoint.
        timeframe: Candle timeframe, e.g. ``"15m"``.

    Returns:
        List of Snapshots, one per valid symbol.
    """
    snapshots: list[Snapshot] = []
    now = datetime.now(UTC)

    for symbol, data in raw_results.items():
        median_forecast = data.get("median_forecast")
        if not median_forecast or not isinstance(median_forecast, list):
            continue

        current_price = safe_float(data.get("current_price"))
        if current_price <= 0:
            # Graceful degradation: emit low-confidence neutral forecast
            # so the symbol stays visible to the merger.
            snapshots.append(
                Snapshot(
                    symbol=symbol,
                    timeframe=cast(Literal["5m", "15m", "1h", "4h", "1D"], timeframe),
                    timestamp=now,
                    signals=[
                        Signal(
                            source="timesfm",
                            type="price_forecast",
                            score=0.0,
                            confidence=0.20,
                            reason="TimesFM: forecast available but current price missing",
                            raw=data,
                        )
                    ],
                )
            )
            continue

        direction_pct = safe_float(data.get("direction_pct"))

        # Score: map ±30% predicted move to ±3.0 range
        score = clamp(direction_pct * 10.0, -3.0, 3.0)

        # Confidence: narrow quantile spread → high confidence
        quantiles = data.get("quantiles", {})
        p10 = quantiles.get("p10", [])
        p90 = quantiles.get("p90", [])

        if p10 and p90 and len(p10) == len(p90):
            spreads = [abs(hi - lo) for hi, lo in zip(p90, p10)]
            mean_spread = sum(spreads) / len(spreads) if spreads else 0.0
            confidence = clamp(1.0 - (mean_spread / current_price), 0.0, 1.0)
        else:
            # No quantile data — use a conservative default
            confidence = 0.4

        horizon_bars = data.get("horizon_bars", len(median_forecast))
        endpoint = median_forecast[-1]
        endpoint_pct = ((endpoint - current_price) / current_price) * 100 if current_price else 0.0

        # Build human-readable reason
        p10_pct = ""
        p90_pct = ""
        if p10 and p90:
            p10_end = ((p10[-1] - current_price) / current_price) * 100 if current_price else 0.0
            p90_end = ((p90[-1] - current_price) / current_price) * 100 if current_price else 0.0
            p10_pct = f", p10={p10_end:+.1f}%"
            p90_pct = f", p90={p90_end:+.1f}%"

        reason = (
            f"TimesFM: {endpoint_pct:+.1f}% median forecast "
            f"over {horizon_bars} bars (${current_price:.2f}→${endpoint:.2f}"
            f"{p10_pct}{p90_pct})"
        )

        signal = Signal(
            source="timesfm",
            type="price_forecast",
            score=score,
            confidence=round(confidence, 4),
            reason=reason,
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
            "median_forecast": [150.5, 151.0, 151.8, 152.5],
            "quantiles": {
                "p10": [149.0, 148.5, 148.0, 147.5],
                "p50": [150.5, 151.0, 151.8, 152.5],
                "p90": [152.0, 153.5, 155.0, 157.0],
            },
            "current_price": 150.0,
            "horizon_bars": 4,
            "direction_pct": 1.67,
        },
    }
    results = normalize(sample, timeframe="15m")
    for r in results:
        print(r.model_dump())
    print(f"Forecast normalizer: {len(results)} snapshots ✅")
