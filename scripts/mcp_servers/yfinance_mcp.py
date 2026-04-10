"""Yahoo Finance MCP — free data via yfinance library.

Tools: short_ratio, options_activity, institutional_holders
No API key required.  Uses yfinance (synchronous) wrapped in asyncio.to_thread().
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

logger = logging.getLogger(__name__)

SERVICE_NAME = "Yahoo Finance MCP"

# In-memory cache with 15-min TTL (matches cron cycle)
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 900.0


def _safe_int(val: Any) -> int:
    """Convert to int, treating None/NaN/Inf as 0."""
    if val is None:
        return 0
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return 0
        return int(f)
    except (ValueError, TypeError):
        return 0


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


def _get_ticker_info(sym: str) -> dict:
    """Synchronous helper — called via to_thread."""
    import yfinance as yf  # noqa: PLC0415 — lazy import to avoid top-level blocking

    try:
        ticker = yf.Ticker(sym)
        return ticker.info or {}
    except Exception as exc:
        logger.warning("yfinance info error for %s: %s", sym, exc)
        return {}


def _get_options_chain(sym: str) -> dict:
    """Synchronous helper — fetch nearest-expiry options chain."""
    import yfinance as yf  # noqa: PLC0415

    try:
        ticker = yf.Ticker(sym)
        expirations = ticker.options
        if not expirations:
            return {"calls": [], "puts": [], "expiry": ""}
        nearest = expirations[0]
        chain = ticker.option_chain(nearest)
        return {
            "calls": chain.calls.to_dict("records") if hasattr(chain.calls, "to_dict") else [],
            "puts": chain.puts.to_dict("records") if hasattr(chain.puts, "to_dict") else [],
            "expiry": nearest,
        }
    except Exception as exc:
        logger.warning("yfinance options error for %s: %s", sym, exc)
        return {"calls": [], "puts": [], "expiry": ""}


def _get_institutional_holders(sym: str) -> list[dict]:
    """Synchronous helper — fetch institutional holders."""
    import yfinance as yf  # noqa: PLC0415

    try:
        ticker = yf.Ticker(sym)
        holders = ticker.institutional_holders
        if holders is None or holders.empty:
            return []
        return holders.head(5).to_dict("records")
    except Exception as exc:
        logger.warning("yfinance institutional_holders error for %s: %s", sym, exc)
        return []


async def short_ratio(params: dict[str, Any]) -> dict:
    """Fetch short interest data for symbols.

    Params:
        symbols: list[str] — tickers to query (max 10).

    Returns:
        {"results": [{"symbol": str, "si_pct_float": float|None,
                       "short_ratio": float|None, "shares_short": int|None}, ...]}
    """
    symbols: list[str] = params.get("symbols", [])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]

    results: list[dict] = []
    for sym in symbols[:20]:
        cache_key = f"short_ratio:{sym}"
        cached = _cache_get(cache_key)
        if cached is not None:
            results.append(cached)
            continue

        info = await asyncio.to_thread(_get_ticker_info, sym)
        entry = {
            "symbol": sym.upper(),
            "si_pct_float": info.get("shortPercentOfFloat"),
            "short_ratio": info.get("shortRatio"),
            "shares_short": info.get("sharesShort"),
            "date_short_interest": info.get("dateShortInterest"),
        }
        _cache_set(cache_key, entry)
        results.append(entry)

    return {"results": results}


async def options_activity(params: dict[str, Any]) -> dict:
    """Analyze options chain for call/put ratio and volume.

    Params:
        symbols: list[str] — tickers to query (max 5).

    Returns:
        {"results": [{"symbol": str, "call_put_ratio": float|None,
                       "total_call_volume": int, "total_put_volume": int,
                       "unusual_oi": bool, "expiry": str}, ...]}
    """
    symbols: list[str] = params.get("symbols", [])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]

    results: list[dict] = []
    for sym in symbols[:10]:
        cache_key = f"options_activity:{sym}"
        cached = _cache_get(cache_key)
        if cached is not None:
            results.append(cached)
            continue

        chain = await asyncio.to_thread(_get_options_chain, sym)
        calls = chain.get("calls", [])
        puts = chain.get("puts", [])

        total_call_vol = sum(_safe_int(c.get("volume")) for c in calls)
        total_put_vol = sum(_safe_int(p.get("volume")) for p in puts)
        total_call_oi = sum(_safe_int(c.get("openInterest")) for c in calls)
        total_put_oi = sum(_safe_int(p.get("openInterest")) for p in puts)

        ratio = round(total_call_vol / total_put_vol, 2) if total_put_vol > 0 else None

        # Unusual OI: total volume > 50% of total OI (high turnover)
        total_oi = total_call_oi + total_put_oi
        total_vol = total_call_vol + total_put_vol
        unusual_oi = total_oi > 0 and total_vol > total_oi * 0.5

        entry = {
            "symbol": sym.upper(),
            "call_put_ratio": ratio,
            "total_call_volume": total_call_vol,
            "total_put_volume": total_put_vol,
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "unusual_oi": unusual_oi,
            "expiry": chain.get("expiry", ""),
        }
        _cache_set(cache_key, entry)
        results.append(entry)

    return {"results": results}


async def institutional_holders(params: dict[str, Any]) -> dict:
    """Fetch top institutional holders for symbols.

    Params:
        symbols: list[str] — tickers to query (max 5).

    Returns:
        {"results": [{"symbol": str, "holders": [{"Holder": str,
                       "Shares": int, "Value": float, ...}, ...]}, ...]}
    """
    symbols: list[str] = params.get("symbols", [])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]

    results: list[dict] = []
    for sym in symbols[:5]:
        holders = await asyncio.to_thread(_get_institutional_holders, sym)
        # Serialize any non-JSON-safe types
        clean_holders: list[dict] = []
        for h in holders:
            clean: dict[str, Any] = {}
            for k, v in h.items():
                if hasattr(v, "isoformat"):
                    clean[k] = v.isoformat()
                elif hasattr(v, "item"):
                    clean[k] = v.item()
                else:
                    clean[k] = v
            clean_holders.append(clean)
        results.append({"symbol": sym.upper(), "holders": clean_holders})

    return {"results": results}


TOOLS: dict[str, Any] = {
    "short_ratio": short_ratio,
    "options_activity": options_activity,
    "institutional_holders": institutional_holders,
}
