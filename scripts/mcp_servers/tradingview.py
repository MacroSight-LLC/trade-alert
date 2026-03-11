"""TradingView MCP — real TA integration via tradingview-ta library.

Tools: bollinger_scan, rsi_scan
No API key required — uses tradingview-ta open-source library.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVICE_NAME = "TradingView MCP"


def _get_analysis(symbol: str, screener: str = "america", exchange: str = "NASDAQ",
                  interval: str = "15m") -> dict | None:
    """Fetch TradingView technical analysis for a symbol."""
    try:
        from tradingview_ta import Interval, TA_Handler

        interval_map = {
            "1m": Interval.INTERVAL_1_MINUTE,
            "5m": Interval.INTERVAL_5_MINUTES,
            "15m": Interval.INTERVAL_15_MINUTES,
            "1h": Interval.INTERVAL_1_HOUR,
            "4h": Interval.INTERVAL_4_HOURS,
            "1D": Interval.INTERVAL_1_DAY,
        }

        handler = TA_Handler(
            symbol=symbol,
            screener=screener,
            exchange=exchange,
            interval=interval_map.get(interval, Interval.INTERVAL_15_MINUTES),
        )
        analysis = handler.get_analysis()
        return {
            "summary": analysis.summary,
            "indicators": analysis.indicators,
        }
    except Exception as exc:
        logger.warning("TradingView TA failed for %s: %s", symbol, exc)
        return None


async def bollinger_scan(params: dict[str, Any]) -> dict:
    """Scan symbols for Bollinger Band squeeze and position.

    Params:
        symbols: list[str] — tickers.
        timeframe: str — e.g. "15m".

    Returns:
        {"results": [{"symbol", "bb_position", "squeeze", "timeframe"}, ...]}
    """
    symbols: list[str] = params.get("symbols", ["AAPL", "NVDA", "TSLA"])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]
    timeframe: str = params.get("timeframe", "15m")

    results: list[dict] = []
    for sym in symbols[:20]:
        analysis = _get_analysis(sym, interval=timeframe)
        if analysis is None:
            continue
        indicators = analysis.get("indicators", {})

        bb_upper = indicators.get("BB.upper", 0)
        bb_lower = indicators.get("BB.lower", 0)
        close = indicators.get("close", 0)

        bb_range = bb_upper - bb_lower if bb_upper and bb_lower else 1
        bb_position = (close - bb_lower) / bb_range if bb_range > 0 else 0.5

        # Squeeze detection: BB width < 20-period SMA of BB width
        # Approximate: narrow bands relative to price
        bb_width = bb_range / close if close > 0 else 0
        squeeze = bb_width < 0.03  # Less than 3% of price = tight squeeze

        results.append({
            "symbol": sym,
            "bb_position": round(bb_position, 4),
            "squeeze": squeeze,
            "timeframe": timeframe,
        })

    return {"results": results}


async def rsi_scan(params: dict[str, Any]) -> dict:
    """Scan symbols for RSI values.

    Params:
        symbols: list[str] — tickers.
        timeframe: str — e.g. "15m".

    Returns:
        {"results": [{"symbol", "rsi", "timeframe"}, ...]}
    """
    symbols: list[str] = params.get("symbols", ["AAPL", "NVDA", "TSLA"])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]
    timeframe: str = params.get("timeframe", "15m")

    results: list[dict] = []
    for sym in symbols[:20]:
        analysis = _get_analysis(sym, interval=timeframe)
        if analysis is None:
            continue
        indicators = analysis.get("indicators", {})
        rsi = indicators.get("RSI", 50.0)
        if rsi is not None:
            results.append({
                "symbol": sym,
                "rsi": round(rsi, 2),
                "timeframe": timeframe,
            })

    return {"results": results}


TOOLS: dict[str, Any] = {
    "bollinger_scan": bollinger_scan,
    "rsi_scan": rsi_scan,
}
