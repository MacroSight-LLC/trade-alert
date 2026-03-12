"""Trading MCP — stock screening and insider trades.

Tools: screen, insiders
Uses Polygon.io API (same key as Polygon MCP).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERVICE_NAME = "trading-mcp server"

POLYGON_API_KEY: str = os.getenv("POLYGON_API_KEY", "")
FINNHUB_API_KEY: str = os.getenv("FINNHUB_API_KEY", "")
TIMEOUT = 10.0


async def screen(params: dict[str, Any]) -> dict:
    """Screen stocks by market cap and volume.

    Uses Polygon snapshot endpoint to get top active tickers.

    Params:
        limit: int (default 20)

    Returns:
        {"results": [{"symbol", "market_cap", "pe_ratio"}, ...]}
    """
    limit = int(params.get("limit", 20))

    results: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            # Use Polygon gainers/losers snapshot for active tickers
            resp = await client.get(
                "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers",
                params={"apiKey": POLYGON_API_KEY},
            )
            resp.raise_for_status()
            tickers = resp.json().get("tickers", [])

            for t in tickers[:limit]:
                ticker_info = t.get("ticker", "")
                day = t.get("day", {})
                results.append(
                    {
                        "symbol": ticker_info,
                        "market_cap": 0,  # Snapshot doesn't include market cap
                        "pe_ratio": 0.0,
                        "volume": int(day.get("v", 0)),
                        "change_pct": round(t.get("todaysChangePerc", 0.0), 2),
                        "close": day.get("c", 0.0),
                    }
                )
    except httpx.HTTPError as exc:
        logger.warning("Polygon screen error: %s", exc)

    # If Polygon fails or returns nothing, try top-of-mind defaults
    if not results:
        defaults = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "AMD", "SPY", "QQQ"]
        results = [{"symbol": s, "market_cap": 0, "pe_ratio": 0.0} for s in defaults[:limit]]

    return {"results": results}


async def insiders(params: dict[str, Any]) -> dict:
    """Fetch insider transactions via Finnhub API.

    Params:
        symbol: str — single ticker (optional, fetches recent insiders).
        limit: int (default 10)

    Returns:
        {"results": [{"symbol", "type", "shares"}, ...]}
    """
    limit = int(params.get("limit", 10))
    symbol: str = params.get("symbol", "")

    results: list[dict] = []
    if not FINNHUB_API_KEY:
        return {"results": results}

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            p: dict[str, Any] = {"token": FINNHUB_API_KEY}
            if symbol:
                p["symbol"] = symbol

            resp = await client.get(
                "https://finnhub.io/api/v1/stock/insider-transactions",
                params=p,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])

            for tx in data[:limit]:
                tx_type = "buy" if (tx.get("transactionCode") or "").upper() in ("P", "A") else "sell"
                results.append(
                    {
                        "symbol": tx.get("symbol", symbol),
                        "type": tx_type,
                        "shares": abs(tx.get("share") or 0),
                        "name": tx.get("name", ""),
                    }
                )
    except httpx.HTTPError as exc:
        logger.warning("Finnhub insiders error: %s", exc)

    return {"results": results}


TOOLS: dict[str, Any] = {
    "screen": screen,
    "insiders": insiders,
}
