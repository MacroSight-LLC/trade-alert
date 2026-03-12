"""TradingView MCP — real TA integration via tradingview-ta library.

Tools: bollinger_scan, rsi_scan
No API key required — uses tradingview-ta open-source library.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

SERVICE_NAME = "TradingView MCP"


# Max retries for 429 rate-limit errors
_MAX_RETRIES = 2
_RETRY_DELAY = 20.0  # TradingView rate limit resets after ~60s

# --- Exchange / screener auto-detection ---

_CRYPTO_SYMBOLS: set[str] = {
    "BTC",
    "ETH",
    "SOL",
    "BNB",
    "AVAX",
    "ADA",
    "XRP",
    "DOGE",
    "DOT",
    "MATIC",
    "LINK",
    "SHIB",
    "UNI",
    "LTC",
    "ATOM",
    "APT",
    "ARB",
    "OP",
    "SUI",
    "NEAR",
    "FIL",
    "ICP",
    "HBAR",
    "VET",
    "ALGO",
    "FTM",
    "SAND",
    "MANA",
    "AAVE",
    "MKR",
}

# Equities that trade on exchanges other than NASDAQ
_EXCHANGE_OVERRIDES: dict[str, str] = {
    "SPY": "AMEX",
    "QQQ": "AMEX",
    "IWM": "AMEX",
    "DIA": "AMEX",
    "XLF": "AMEX",
    "XLE": "AMEX",
    "XLK": "AMEX",
    "XLV": "AMEX",
    "GLD": "AMEX",
    "SLV": "AMEX",
    "TLT": "AMEX",
    "HYG": "AMEX",
    "VXX": "AMEX",
    "UVXY": "AMEX",
    "JPM": "NYSE",
    "BAC": "NYSE",
    "WMT": "NYSE",
    "JNJ": "NYSE",
    "V": "NYSE",
    "MA": "NYSE",
    "UNH": "NYSE",
    "HD": "NYSE",
    "PG": "NYSE",
    "KO": "NYSE",
    "DIS": "NYSE",
    "NKE": "NYSE",
    "CVX": "NYSE",
    "XOM": "NYSE",
    "GS": "NYSE",
    "BA": "NYSE",
    "CAT": "NYSE",
    "GM": "NYSE",
    "F": "NYSE",
}


def _resolve_screener_exchange(symbol: str) -> tuple[str, str]:
    """Return (screener, exchange) for a symbol.

    Crypto symbols → ("crypto", "BINANCE")
    Known ETFs/NYSE stocks → ("america", <override>)
    Default → ("america", "NASDAQ")
    """
    sym = symbol.upper().replace("USDT", "").replace("USD", "")
    if sym in _CRYPTO_SYMBOLS or symbol.upper().endswith("USDT"):
        return ("crypto", "BINANCE")
    override = _EXCHANGE_OVERRIDES.get(symbol.upper())
    if override:
        return ("america", override)
    return ("america", "NASDAQ")


# In-memory TTL cache — avoids re-fetching the same symbol/interval within _CACHE_TTL seconds.
# Key: (symbol, screener, exchange, interval) → (timestamp, result_dict)
_CACHE: dict[tuple[str, str, str, str], tuple[float, dict | None]] = {}
_CACHE_TTL = 300.0  # 5 minutes


def _get_analysis(
    symbol: str, screener: str = "america", exchange: str = "NASDAQ", interval: str = "15m"
) -> dict | None:
    """Fetch TradingView technical analysis for a symbol (cached)."""
    from tradingview_ta import Interval, TA_Handler

    # Binance needs USDT-suffixed pairs (e.g. BTCUSDT)
    tv_symbol = symbol
    if screener == "crypto" and not symbol.upper().endswith("USDT"):
        tv_symbol = f"{symbol.upper()}USDT"

    cache_key = (symbol, screener, exchange, interval)
    now = time.monotonic()
    cached = _CACHE.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    interval_map = {
        "1m": Interval.INTERVAL_1_MINUTE,
        "5m": Interval.INTERVAL_5_MINUTES,
        "15m": Interval.INTERVAL_15_MINUTES,
        "1h": Interval.INTERVAL_1_HOUR,
        "4h": Interval.INTERVAL_4_HOURS,
        "1D": Interval.INTERVAL_1_DAY,
    }

    for attempt in range(_MAX_RETRIES + 1):
        try:
            handler = TA_Handler(
                symbol=tv_symbol,
                screener=screener,
                exchange=exchange,
                interval=interval_map.get(interval, Interval.INTERVAL_15_MINUTES),
            )
            analysis = handler.get_analysis()
            result = {
                "summary": analysis.summary,
                "indicators": analysis.indicators,
            }
            _CACHE[cache_key] = (now, result)
            return result
        except Exception as exc:
            msg = str(exc)
            if "429" in msg and attempt < _MAX_RETRIES:
                import time as _time

                _time.sleep(_RETRY_DELAY * (attempt + 1))
                continue
            logger.warning("TradingView TA failed for %s: %s", symbol, exc)
            _CACHE[cache_key] = (now, None)
            return None
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
    for i, sym in enumerate(symbols[:20]):
        screener, exchange = _resolve_screener_exchange(sym)
        cache_key = (sym, screener, exchange, timeframe)
        if i > 0 and cache_key not in _CACHE:
            await asyncio.sleep(6.0)  # Rate limit: TradingView aggressively 429s
        analysis = _get_analysis(sym, screener=screener, exchange=exchange, interval=timeframe)
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

        results.append(
            {
                "symbol": sym,
                "bb_position": round(bb_position, 4),
                "squeeze": squeeze,
                "timeframe": timeframe,
            }
        )

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
    for i, sym in enumerate(symbols[:20]):
        screener, exchange = _resolve_screener_exchange(sym)
        cache_key = (sym, screener, exchange, timeframe)
        if i > 0 and cache_key not in _CACHE:
            await asyncio.sleep(6.0)  # Rate limit: TradingView aggressively 429s
        analysis = _get_analysis(sym, screener=screener, exchange=exchange, interval=timeframe)
        if analysis is None:
            continue
        indicators = analysis.get("indicators", {})
        rsi = indicators.get("RSI", 50.0)
        if rsi is not None:
            results.append(
                {
                    "symbol": sym,
                    "rsi": round(rsi, 2),
                    "timeframe": timeframe,
                }
            )

    return {"results": results}


TOOLS: dict[str, Any] = {
    "bollinger_scan": bollinger_scan,
    "rsi_scan": rsi_scan,
}
