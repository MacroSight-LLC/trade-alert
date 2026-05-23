"""TradingView MCP — TA computed from Alpaca intraday bars.

Tools: ta_scan, bollinger_scan, rsi_scan
Requires: ALPACA_API_KEY and ALPACA_SECRET_KEY env vars.

RSI-14 and Bollinger Bands (SMA-20 +/- 2sigma) are computed locally from
the most recent Alpaca bars. This replaces the old tradingview-ta scraper,
which is permanently rate-limited in production.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from statistics import mean, stdev
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERVICE_NAME = "TradingView MCP"

_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
_BASE_URL = "https://data.alpaca.markets"
_TIMEOUT = 12.0

# Alpaca data API allows substantially more throughput than the old scraper.
_REQUEST_DELAY = float(os.getenv("ALPACA_REQUEST_DELAY", "0.35"))

# Global per-request rate limiter (shared budget with polygon_io across containers is
# not needed here — each container has its own process; this guards within the MCP server)
_rate_lock = asyncio.Lock()
_last_request_time: float = 0.0

# In-memory TTL cache — avoids re-fetching the same symbol/timeframe within _CACHE_TTL seconds.
# Key: (symbol, timeframe) → (timestamp, result_dict | None)
_CACHE: dict[tuple[str, str], tuple[float, dict | None]] = {}
_CACHE_TTL = 900.0  # 15 min — matches cron interval


def _compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI-14 from a list of closing prices (oldest first)."""
    if len(closes) < period + 1:
        return None

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    # Seed with simple average over first `period` deltas
    avg_gain = mean(gains[:period])
    avg_loss = mean(losses[:period])

    # Wilder smoothing for remaining bars
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def _compute_bb(closes: list[float]) -> tuple[float, bool]:
    """Bollinger Bands from close prices (SMA-20, ± 2σ).

    Returns:
        bb_position: 0.0 (at lower band) → 1.0 (at upper band); 0.5 = midline.
        squeeze: True when band width / SMA < 3%.
    """
    n = min(len(closes), 20)
    if n < 2:
        return 0.5, False
    window = closes[-n:]
    sma = mean(window)
    sd = stdev(window)
    bb_upper = sma + 2 * sd
    bb_lower = sma - 2 * sd
    close = closes[-1]
    bb_range = bb_upper - bb_lower
    bb_position = (close - bb_lower) / bb_range if bb_range > 0 else 0.5
    squeeze = (bb_range / sma) < 0.03 if sma > 0 else False
    return round(bb_position, 4), squeeze


def _alpaca_timeframe(timeframe: str) -> str:
    mapping = {
        "1m": "1Min",
        "5m": "5Min",
        "15m": "15Min",
        "1h": "1Hour",
    }
    return mapping.get(timeframe, "15Min")


async def _fetch_bars(symbol: str, timeframe: str) -> list[float] | None:
    """Fetch recent close prices from Alpaca bars endpoint.

    Returns closes oldest-first, enough for RSI-14 and BB-20.
    """
    global _last_request_time

    if not _API_KEY or not _SECRET_KEY:
        logger.warning("Alpaca credentials not set — TradingView MCP returning empty")
        return None

    now_utc = datetime.now(timezone.utc)
    if timeframe == "1h":
        start = (now_utc - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        limit = 30
    else:
        start = (now_utc - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        limit = 30

    path = f"/v2/stocks/{symbol}/bars"
    params = {
        "timeframe": _alpaca_timeframe(timeframe),
        "start": start,
        "limit": limit,
        "feed": "iex",
    }
    headers = {
        "APCA-API-KEY-ID": _API_KEY,
        "APCA-API-SECRET-KEY": _SECRET_KEY,
        "Accept": "application/json",
    }

    async with _rate_lock:
        wait = _REQUEST_DELAY - (time.monotonic() - _last_request_time)
        if wait > 0:
            logger.debug("Alpaca TA rate-limit: sleeping %.2fs before %s", wait, symbol)
            await asyncio.sleep(wait)
        _last_request_time = time.monotonic()

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.get(f"{_BASE_URL}{path}", params=params, headers=headers)
            resp.raise_for_status()
            bars = resp.json().get("bars", [])
            if not bars:
                logger.debug("Alpaca bars: no data for %s", symbol)
                return None
            return [float(b["c"]) for b in bars if "c" in b]
        except httpx.HTTPError as exc:
            logger.warning("Alpaca TA bars error for %s: %s", symbol, exc)
            return None
    return None


async def ta_scan(params: dict[str, Any]) -> dict:
    """Combined TA scan: RSI-14 + Bollinger Bands from Alpaca bars.

    Params:
        symbols: list[str] — tickers (max 8 per call).
        timeframe: str — e.g. "15m" or "1h".

    Returns:
        {"results": [{"symbol", "rsi", "bb_position", "squeeze", "timeframe"}, ...]}
    """
    symbols: list[str] = params.get("symbols", ["AAPL", "NVDA", "TSLA"])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]
    timeframe: str = params.get("timeframe", "15m")

    results: list[dict] = []
    for sym in symbols[:8]:
        sym = sym.upper()
        cache_key = (sym, timeframe)
        now_mono = time.monotonic()
        cached = _CACHE.get(cache_key)
        if cached and (now_mono - cached[0]) < _CACHE_TTL:
            if cached[1] is not None:
                results.append(cached[1])
            continue

        closes = await _fetch_bars(sym, timeframe)
        if closes is None or len(closes) < 2:
            _CACHE[cache_key] = (now_mono, None)
            continue

        rsi = _compute_rsi(closes)
        bb_position, squeeze = _compute_bb(closes)

        entry: dict[str, Any] = {
            "symbol": sym,
            "timeframe": timeframe,
            "bb_position": bb_position,
            "squeeze": squeeze,
            "close": round(closes[-1], 4),
            "current_price": round(closes[-1], 4),
        }
        if rsi is not None:
            entry["rsi"] = rsi

        _CACHE[cache_key] = (now_mono, entry)
        results.append(entry)

    return {"results": results}


# Keep legacy tool names as aliases so older workflow YAML still works
# DEPRECATED: alias for backward compat; no active workflows use this as of v1.1.0.
# Remove after confirming via CHANGELOG that old callers are gone.
async def bollinger_scan(params: dict[str, Any]) -> dict:
    """Legacy alias — delegates to ta_scan, returns only BB fields."""
    full = await ta_scan(params)
    return {"results": [{k: v for k, v in r.items() if k != "rsi"} for r in full.get("results", [])]}


# DEPRECATED: alias for backward compat; no active workflows use this as of v1.1.0.
# Remove after confirming via CHANGELOG that old callers are gone.
async def rsi_scan(params: dict[str, Any]) -> dict:
    """Legacy alias — delegates to ta_scan, returns only RSI fields."""
    full = await ta_scan(params)
    return {
        "results": [
            {k: v for k, v in r.items() if k not in ("bb_position", "squeeze")}
            for r in full.get("results", [])
        ]
    }


TOOLS: dict[str, Any] = {
    "ta_scan": ta_scan,
    "bollinger_scan": bollinger_scan,
    "rsi_scan": rsi_scan,
}
