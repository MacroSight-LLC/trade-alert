"""Finnhub MCP — real API integration.

Tools: sentiment, news_symbol
Requires: FINNHUB_API_KEY env var.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERVICE_NAME = "Finnhub MCP"

API_KEY: str = os.getenv("FINNHUB_API_KEY", "")
BASE_URL = "https://finnhub.io/api/v1"
TIMEOUT = 10.0


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Make authenticated GET request to Finnhub."""
    p = {"token": API_KEY, **(params or {})}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(f"{BASE_URL}{path}", params=p)
        resp.raise_for_status()
        return resp.json()


async def sentiment(params: dict[str, Any]) -> list[dict]:
    """Fetch news sentiment for a batch of symbols.

    Params:
        symbols: list[str] — tickers to query.
        aggregate: bool — ignored (always aggregate).

    Returns:
        [{"symbol": str, "score": float, "articles": int}, ...]
    """
    symbols: list[str] = params.get("symbols", [])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]

    results: list[dict] = []
    for sym in symbols[:20]:
        try:
            data = await _get("/news-sentiment", {"symbol": sym})
            buzz = data.get("buzz", {})
            sent = data.get("sentiment", {})

            # Finnhub returns sentiment as {bullishPercent, bearishPercent}
            bullish = sent.get("bullishPercent", 0.5)
            bearish = sent.get("bearishPercent", 0.5)
            # Convert to -1..+1 scale: net = bull - bear
            score = round(bullish - bearish, 4)
            articles = buzz.get("articlesInLastWeek", 0)

            results.append({
                "symbol": sym,
                "score": score,
                "articles": articles,
            })
        except httpx.HTTPError as exc:
            logger.warning("Finnhub sentiment error for %s: %s", sym, exc)

    return results


async def news_symbol(params: dict[str, Any]) -> dict:
    """Fetch recent news articles for a symbol.

    Params:
        symbol: str — single ticker.

    Returns:
        {"articles": [{"headline": str, "source": str, "url": str}, ...]}
    """
    symbol: str = params.get("symbol", "AAPL")
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    try:
        data = await _get("/company-news", {"symbol": symbol, "from": week_ago, "to": today})
        articles = [
            {
                "headline": a.get("headline", ""),
                "source": a.get("source", ""),
                "url": a.get("url", ""),
            }
            for a in (data or [])[:10]
        ]
        return {"articles": articles}
    except httpx.HTTPError as exc:
        logger.warning("Finnhub news error for %s: %s", symbol, exc)
        return {"articles": []}


TOOLS: dict[str, Any] = {
    "sentiment": sentiment,
    "news_symbol": news_symbol,
}
