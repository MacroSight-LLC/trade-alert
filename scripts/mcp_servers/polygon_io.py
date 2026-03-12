"""Polygon.io MCP — real API integration.

Tools: unusual_activity, aggs, grouped_daily
Requires: POLYGON_API_KEY env var.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import date, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERVICE_NAME = "Polygon MCP"

API_KEY: str = os.getenv("POLYGON_API_KEY", "")
BASE_URL = "https://api.polygon.io"
TIMEOUT = 10.0

# Free-tier: 5 requests/min → 12s between individual API calls
_REQUEST_DELAY = 12.5
_MAX_429_RETRIES = 2
_429_BACKOFF = 15.0

# Global per-request rate limiter (protects across all tools)
_rate_lock = asyncio.Lock()
_last_request_time: float = 0.0

# ── Response-level cache (Issue #9: dedup across collectors) ─────────
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 120.0  # seconds


def _cache_get(key: str) -> Any | None:
    """Return cached response if still fresh, else None."""
    entry = _CACHE.get(key)
    if entry is None:
        return None
    ts, val = entry
    if time.monotonic() - ts > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return val


def _cache_set(key: str, val: Any) -> None:
    """Store a response in cache."""
    _CACHE[key] = (time.monotonic(), val)


async def _get(path: str, params: dict[str, Any] | None = None) -> dict:
    """Make authenticated GET request to Polygon API with 429 retry and caching."""
    global _last_request_time

    # Build cache key from path + sorted params
    p = {"apiKey": API_KEY, **(params or {})}
    # Exclude apiKey from cache key for safety
    cache_params = {k: v for k, v in sorted(p.items()) if k != "apiKey"}
    cache_key = f"{path}|{cache_params}"
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.debug("Polygon cache hit: %s", path)
        return cached

    async with _rate_lock:
        now = time.monotonic()
        wait = _REQUEST_DELAY - (now - _last_request_time)
        if wait > 0:
            logger.debug("Polygon rate-limit: sleeping %.1fs before %s", wait, path)
            await asyncio.sleep(wait)
        _last_request_time = time.monotonic()
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for attempt in range(_MAX_429_RETRIES + 1):
            resp = await client.get(f"{BASE_URL}{path}", params=p)
            if resp.status_code == 429 and attempt < _MAX_429_RETRIES:
                logger.info("Polygon 429 on %s, backing off %.0fs", path, _429_BACKOFF * (attempt + 1))
                await asyncio.sleep(_429_BACKOFF * (attempt + 1))
                _last_request_time = time.monotonic()
                continue
            resp.raise_for_status()
            result = resp.json()
            _cache_set(cache_key, result)
            return result
    return {}


async def unusual_activity(params: dict[str, Any]) -> dict:
    """Fetch unusual options activity via Polygon snapshot endpoint.

    Params:
        symbols: list[str] — tickers to check.

    Returns:
        {"results": [{"symbol", "type", "premium", "strike", "expiry"}, ...]}
    """
    symbols: list[str] = params.get("symbols", [])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]

    results: list[dict] = []
    # Use the options snapshot endpoint to detect unusual volume/OI
    for sym in symbols[:25]:
        try:
            data = await _get(
                f"/v3/snapshot/options/{sym}",
                {"limit": 5, "order": "desc", "sort": "volume"},
            )
            for item in data.get("results", []):
                details = item.get("details", {})
                day = item.get("day", {})
                if day.get("volume", 0) > day.get("open_interest", 1) * 0.5:
                    results.append(
                        {
                            "symbol": sym,
                            "type": "options_sweep",
                            "premium": int(day.get("volume", 0) * day.get("close", 0) * 100),
                            "strike": details.get("strike_price", 0),
                            "expiry": details.get("expiration_date", ""),
                        }
                    )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403:
                logger.debug("Polygon options not available for %s (plan limit)", sym)
            else:
                logger.warning("Polygon unusual_activity error for %s: %s", sym, exc)
        except httpx.HTTPError as exc:
            logger.warning("Polygon unusual_activity error for %s: %s", sym, exc)

    return {"results": results}


async def aggs(params: dict[str, Any]) -> dict:
    """Fetch aggregate bars and compute volume vs 20d average.

    Uses the free-tier /v2/aggs/ticker/{sym}/prev endpoint (previous-day bar)
    and /v2/aggs/ticker/{sym}/range for a 20-day lookback to compute avg volume.

    Params:
        symbols: list[str] — tickers.

    Returns:
        {"results": [{"symbol", "volume", "avg_volume", "close"}, ...]}
    """
    symbols: list[str] = params.get("symbols", [])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]

    from datetime import date, timedelta

    results: list[dict] = []
    for sym in symbols[:10]:
        try:
            # Previous-day close (free-tier endpoint)
            prev = await _get(f"/v2/aggs/ticker/{sym}/prev")
            prev_results = prev.get("results", [])
            if not prev_results:
                logger.debug("Polygon prev returned no results for %s", sym)
                continue
            bar = prev_results[0]
            volume = int(bar.get("v", 0))
            close = bar.get("c", 0.0)

            # 20-day daily bars for average volume (free-tier endpoint)
            end = date.today().isoformat()
            start = (date.today() - timedelta(days=30)).isoformat()
            hist = await _get(
                f"/v2/aggs/ticker/{sym}/range/1/day/{start}/{end}",
                {"adjusted": "true", "limit": 20},
            )
            hist_bars = hist.get("results", [])
            if hist_bars:
                avg_volume = int(sum(b.get("v", 0) for b in hist_bars) / len(hist_bars)) or 1
            else:
                avg_volume = volume or 1

            results.append(
                {
                    "symbol": sym,
                    "volume": volume,
                    "avg_volume": avg_volume,
                    "avg_20d_volume": avg_volume,
                    "close": close,
                }
            )
        except httpx.HTTPError as exc:
            logger.warning("Polygon aggs error for %s: %s", sym, exc)

    return {"results": results}


def _previous_trading_day() -> str:
    """Return the most recent completed trading day as YYYY-MM-DD.

    Skips weekends.  Does not account for market holidays (Polygon will
    simply return an empty result set for holidays, which is safe).
    """
    today = date.today()
    # If today is Monday, previous trading day is Friday (3 days back).
    # If Sunday, Friday (2 days). Saturday, Friday (1 day). Else yesterday.
    offset = {0: 3, 5: 1, 6: 2}.get(today.weekday(), 1)
    return (today - timedelta(days=offset)).isoformat()


async def grouped_daily(params: dict[str, Any]) -> dict:
    """Fetch grouped daily bars for ALL US equities in a single API call.

    Uses /v2/aggs/grouped/locale/us/market/stocks/{date} which returns
    every ticker's previous-day bar.  This eliminates the per-symbol
    bottleneck on the free tier (1 call instead of 2N calls).

    Params:
        symbols: list[str] — tickers to filter results for.
            If empty, returns top 100 by volume.
        date: str (optional) — YYYY-MM-DD.  Defaults to previous trading day.

    Returns:
        {"results": [{"symbol", "volume", "avg_volume", "close"}, ...]}
    """
    symbols: list[str] = params.get("symbols", [])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]
    wanted = {s.upper() for s in symbols} if symbols else set()

    target_date = params.get("date") or _previous_trading_day()

    try:
        data = await _get(
            f"/v2/aggs/grouped/locale/us/market/stocks/{target_date}",
            {"adjusted": "true"},
        )
    except httpx.HTTPError as exc:
        logger.warning("Polygon grouped_daily error: %s", exc)
        return {"results": []}

    all_bars = data.get("results", [])

    # Build a volume lookup for avg estimation (we only have 1 day here;
    # the caller can divide by a separately-fetched 20-day avg if needed).
    results: list[dict] = []
    for bar in all_bars:
        sym = bar.get("T", "")
        if wanted and sym not in wanted:
            continue
        results.append(
            {
                "symbol": sym,
                "volume": int(bar.get("v", 0)),
                "avg_volume": int(bar.get("v", 0)),  # single-day proxy
                "avg_20d_volume": int(bar.get("v", 0)),
                "close": bar.get("c", 0.0),
                "open": bar.get("o", 0.0),
                "high": bar.get("h", 0.0),
                "low": bar.get("l", 0.0),
            }
        )

    # If no filter, return top 100 by volume
    if not wanted:
        results.sort(key=lambda r: r["volume"], reverse=True)
        results = results[:100]

    return {"results": results}


TOOLS: dict[str, Any] = {
    "unusual_activity": unusual_activity,
    "aggs": aggs,
    "grouped_daily": grouped_daily,
}
