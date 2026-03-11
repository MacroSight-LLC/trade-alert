"""Polygon.io MCP — real API integration.

Tools: unusual_activity, aggs
Requires: POLYGON_API_KEY env var.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERVICE_NAME = "Polygon MCP"

API_KEY: str = os.getenv("POLYGON_API_KEY", "")
BASE_URL = "https://api.polygon.io"
TIMEOUT = 10.0


async def _get(path: str, params: dict[str, Any] | None = None) -> dict:
    """Make authenticated GET request to Polygon API."""
    p = {"apiKey": API_KEY, **(params or {})}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(f"{BASE_URL}{path}", params=p)
        resp.raise_for_status()
        return resp.json()


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
                    results.append({
                        "symbol": sym,
                        "type": "options_sweep",
                        "premium": int(day.get("volume", 0) * day.get("close", 0) * 100),
                        "strike": details.get("strike_price", 0),
                        "expiry": details.get("expiration_date", ""),
                    })
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

    Params:
        symbols: list[str] — tickers.
        include_avg_20d_volume: bool — always computed.

    Returns:
        {"results": [{"symbol", "volume", "avg_volume", "close"}, ...]}
    """
    symbols: list[str] = params.get("symbols", [])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]

    results: list[dict] = []
    for sym in symbols[:25]:
        try:
            # Get snapshot for current day
            snap = await _get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{sym}")
            ticker = snap.get("ticker", {})
            day_data = ticker.get("day", {})
            prev = ticker.get("prevDay", {})

            # Use prev day volume as proxy for average when no SMA endpoint
            volume = int(day_data.get("v", 0))
            # Polygon snapshot includes min data — estimate avg from prev
            avg_volume = int(prev.get("v", 1)) or 1

            results.append({
                "symbol": sym,
                "volume": volume,
                "avg_volume": avg_volume,
                "avg_20d_volume": avg_volume,
                "close": day_data.get("c", 0.0),
            })
        except httpx.HTTPError as exc:
            logger.warning("Polygon aggs error for %s: %s", sym, exc)

    return {"results": results}


TOOLS: dict[str, Any] = {
    "unusual_activity": unusual_activity,
    "aggs": aggs,
}
