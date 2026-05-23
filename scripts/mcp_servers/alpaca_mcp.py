"""Alpaca Markets MCP — free-tier data API integration.

Tools: intraday_bars, volume_profile, latest_quote
Requires: ALPACA_API_KEY and ALPACA_SECRET_KEY env vars.

Free tier uses IEX feed (15-min delayed) — acceptable for 15m/1h cycles.
Rate limit: 200 req/min.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERVICE_NAME = "Alpaca MCP"

_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
_BASE_URL = "https://data.alpaca.markets"
_TIMEOUT = 12.0

# In-memory cache with 15-min TTL
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 900.0


def _cache_get(key: str) -> Any | None:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    ts, val = entry
    if time.monotonic() - ts > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return val


def _cache_set(key: str, val: Any) -> None:
    _CACHE[key] = (time.monotonic(), val)


async def _get(path: str, params: dict[str, Any] | None = None) -> dict:
    """Make authenticated GET request to Alpaca data API."""
    headers = {
        "APCA-API-KEY-ID": _API_KEY,
        "APCA-API-SECRET-KEY": _SECRET_KEY,
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{_BASE_URL}{path}",
            params=params or {},
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()


def _market_open_today() -> str:
    """Return today's market open as ISO 8601 (approx 9:30 ET = 14:30 UTC)."""
    now = datetime.now(UTC)
    open_time = now.replace(hour=14, minute=30, second=0, microsecond=0)
    if now < open_time:
        open_time -= timedelta(days=1)
    return open_time.strftime("%Y-%m-%dT%H:%M:%SZ")


async def intraday_bars(params: dict[str, Any]) -> dict:
    """Fetch 15-minute bars for today's session.

    Computes volume acceleration: last 3 bars avg volume vs prior 3 bars.

    Params:
        symbols: list[str] — tickers to query (max 10).

    Returns:
        {"results": [{"symbol": str, "bars": int, "session_volume": int,
                       "volume_acceleration": float|None, "last_close": float|None}, ...]}
    """
    symbols: list[str] = params.get("symbols", [])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]

    if not _API_KEY:
        logger.warning("ALPACA_API_KEY not set — returning empty results")
        return {"results": []}

    results: list[dict] = []
    start = _market_open_today()

    for sym in symbols[:20]:
        cache_key = f"intraday_bars:{sym}"
        cached = _cache_get(cache_key)
        if cached is not None:
            results.append(cached)
            continue

        try:
            data = await _get(
                f"/v2/stocks/{sym}/bars",
                {
                    "timeframe": "15Min",
                    "start": start,
                    "limit": 30,
                    "feed": "iex",
                },
            )
            bars = data.get("bars", [])
            session_volume = sum(int(b.get("v", 0)) for b in bars)
            last_close = bars[-1].get("c") if bars else None

            # Volume acceleration: last 3 bars vs prior 3
            vol_accel: float | None = None
            if len(bars) >= 6:
                recent_vol = sum(int(b.get("v", 0)) for b in bars[-3:]) / 3
                prior_vol = sum(int(b.get("v", 0)) for b in bars[-6:-3]) / 3
                if prior_vol > 0:
                    vol_accel = round(recent_vol / prior_vol, 2)

            entry = {
                "symbol": sym.upper(),
                "bars": len(bars),
                "session_volume": session_volume,
                "volume_acceleration": vol_accel,
                "last_close": last_close,
            }
            _cache_set(cache_key, entry)
            results.append(entry)
        except httpx.HTTPError as exc:
            logger.warning("Alpaca intraday_bars error for %s: %s", sym, exc)

    return {"results": results}


async def volume_profile(params: dict[str, Any]) -> dict:
    """Compute VWAP and session volume context from intraday bars.

    Params:
        symbols: list[str] — tickers to query (max 10).

    Returns:
        {"results": [{"symbol": str, "vwap": float|None,
                       "session_volume": int, "bar_count": int}, ...]}
    """
    symbols: list[str] = params.get("symbols", [])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]

    if not _API_KEY:
        return {"results": []}

    results: list[dict] = []
    start = _market_open_today()

    for sym in symbols[:20]:
        cache_key = f"volume_profile:{sym}"
        cached = _cache_get(cache_key)
        if cached is not None:
            results.append(cached)
            continue

        try:
            data = await _get(
                f"/v2/stocks/{sym}/bars",
                {
                    "timeframe": "15Min",
                    "start": start,
                    "limit": 30,
                    "feed": "iex",
                },
            )
            bars = data.get("bars", [])
            total_vol = 0
            total_pv = 0.0
            for b in bars:
                v = int(b.get("v", 0))
                typical_price = (float(b.get("h", 0)) + float(b.get("l", 0)) + float(b.get("c", 0))) / 3.0
                total_vol += v
                total_pv += typical_price * v

            vwap = round(total_pv / total_vol, 4) if total_vol > 0 else None

            entry = {
                "symbol": sym.upper(),
                "vwap": vwap,
                "session_volume": total_vol,
                "bar_count": len(bars),
            }
            _cache_set(cache_key, entry)
            results.append(entry)
        except httpx.HTTPError as exc:
            logger.warning("Alpaca volume_profile error for %s: %s", sym, exc)

    return {"results": results}


async def latest_quote(params: dict[str, Any]) -> dict:
    """Fetch latest snapshot quote with bid/ask spread.

    Params:
        symbols: list[str] — tickers to query (max 10).

    Returns:
        {"results": [{"symbol": str, "last_price": float|None,
                       "bid": float|None, "ask": float|None,
                       "spread": float|None, "volume": int|None}, ...]}
    """
    symbols: list[str] = params.get("symbols", [])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]

    if not _API_KEY:
        return {"results": []}

    results: list[dict] = []
    for sym in symbols[:10]:
        try:
            data = await _get(f"/v2/stocks/{sym}/snapshot", {"feed": "iex"})
            latest_trade = data.get("latestTrade", {})
            latest_quote_data = data.get("latestQuote", {})
            minute_bar = data.get("minuteBar", {})

            bid = latest_quote_data.get("bp")
            ask = latest_quote_data.get("ap")
            spread = round(ask - bid, 4) if bid is not None and ask is not None else None

            results.append(
                {
                    "symbol": sym.upper(),
                    "last_price": latest_trade.get("p"),
                    "bid": bid,
                    "ask": ask,
                    "spread": spread,
                    "volume": minute_bar.get("v"),
                }
            )
        except httpx.HTTPError as exc:
            logger.warning("Alpaca latest_quote error for %s: %s", sym, exc)

    return {"results": results}


TOOLS: dict[str, Any] = {
    "intraday_bars": intraday_bars,
    "volume_profile": volume_profile,
    "latest_quote": latest_quote,
}
