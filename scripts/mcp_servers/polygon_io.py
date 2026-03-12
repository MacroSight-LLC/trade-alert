"""Polygon.io MCP — real API integration.

Tools: unusual_activity, aggs
Requires: POLYGON_API_KEY env var.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
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


async def _get(path: str, params: dict[str, Any] | None = None) -> dict:
    """Make authenticated GET request to Polygon API with 429 retry."""
    global _last_request_time
    async with _rate_lock:
        now = time.monotonic()
        wait = _REQUEST_DELAY - (now - _last_request_time)
        if wait > 0:
            logger.debug("Polygon rate-limit: sleeping %.1fs before %s", wait, path)
            await asyncio.sleep(wait)
        _last_request_time = time.monotonic()
    p = {"apiKey": API_KEY, **(params or {})}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for attempt in range(_MAX_429_RETRIES + 1):
            resp = await client.get(f"{BASE_URL}{path}", params=p)
            if resp.status_code == 429 and attempt < _MAX_429_RETRIES:
                logger.info("Polygon 429 on %s, backing off %.0fs", path, _429_BACKOFF * (attempt + 1))
                await asyncio.sleep(_429_BACKOFF * (attempt + 1))
                _last_request_time = time.monotonic()
                continue
            resp.raise_for_status()
            return resp.json()
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


TOOLS: dict[str, Any] = {
    "unusual_activity": unusual_activity,
    "aggs": aggs,
}
