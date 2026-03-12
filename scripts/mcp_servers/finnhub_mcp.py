"""Finnhub MCP — real API integration.

Tools: sentiment, news_symbol
Requires: FINNHUB_API_KEY env var.

Sentiment uses the free-tier /company-news endpoint with local keyword
scoring instead of the paid /news-sentiment endpoint (which returns 403
on the free plan).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERVICE_NAME = "Finnhub MCP"

API_KEY: str = os.getenv("FINNHUB_API_KEY", "")
BASE_URL = "https://finnhub.io/api/v1"
TIMEOUT = 10.0

# ── Local headline sentiment scoring ─────────────────────────────────

_BULLISH_WORDS: frozenset[str] = frozenset(
    {
        "surge",
        "surges",
        "surging",
        "rally",
        "rallies",
        "rallying",
        "beat",
        "beats",
        "upgrade",
        "upgrades",
        "upgraded",
        "buy",
        "bull",
        "bullish",
        "record",
        "strong",
        "strength",
        "growth",
        "breakout",
        "boost",
        "boosts",
        "boosted",
        "soar",
        "soars",
        "soaring",
        "gain",
        "gains",
        "jumps",
        "outperform",
        "outperforms",
        "positive",
        "rises",
        "rising",
    }
)

_BEARISH_WORDS: frozenset[str] = frozenset(
    {
        "crash",
        "crashes",
        "crashing",
        "plunge",
        "plunges",
        "plunging",
        "miss",
        "misses",
        "missed",
        "downgrade",
        "downgrades",
        "downgraded",
        "sell",
        "bear",
        "bearish",
        "warning",
        "warns",
        "warned",
        "weak",
        "weakness",
        "decline",
        "declines",
        "declining",
        "drop",
        "drops",
        "dropping",
        "cut",
        "cuts",
        "slump",
        "fall",
        "falls",
        "falling",
        "loss",
        "losses",
        "negative",
        "underperform",
        "underperforms",
        "disappoints",
    }
)


def _score_headlines(articles: list[dict[str, str]]) -> tuple[float, int]:
    """Score a list of news articles using keyword sentiment analysis.

    Args:
        articles: List of dicts with at least a ``headline`` key.

    Returns:
        (score, article_count) where score is clamped to [-1, +1].
    """
    if not articles:
        return 0.0, 0

    bullish_hits = 0
    bearish_hits = 0
    for article in articles:
        words = article.get("headline", "").lower().split()
        for w in words:
            if w in _BULLISH_WORDS:
                bullish_hits += 1
            elif w in _BEARISH_WORDS:
                bearish_hits += 1

    total_hits = bullish_hits + bearish_hits
    if total_hits == 0:
        return 0.0, len(articles)

    raw_score = (bullish_hits - bearish_hits) / total_hits
    score = max(-1.0, min(1.0, raw_score))
    return round(score, 4), len(articles)


# ── Finnhub API helpers ──────────────────────────────────────────────


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Make authenticated GET request to Finnhub."""
    p = {"token": API_KEY, **(params or {})}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(f"{BASE_URL}{path}", params=p)
        resp.raise_for_status()
        return resp.json()


async def _fetch_company_news(symbol: str) -> list[dict[str, str]]:
    """Fetch recent company news articles for a single symbol (free tier)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    data = await _get("/company-news", {"symbol": symbol, "from": week_ago, "to": today})
    return [
        {
            "headline": a.get("headline", ""),
            "source": a.get("source", ""),
            "url": a.get("url", ""),
        }
        for a in (data or [])[:15]
    ]


# ── Tool implementations ─────────────────────────────────────────────


async def sentiment(params: dict[str, Any]) -> dict:
    """Fetch news sentiment for a batch of symbols.

    Uses the free-tier /company-news endpoint with local keyword scoring
    instead of the paid /news-sentiment endpoint.

    Params:
        symbols: list[str] — tickers to query.
        aggregate: bool — ignored (always aggregate).

    Returns:
        {"results": [{"symbol": str, "score": float, "articles": int}, ...]}
    """
    symbols: list[str] = params.get("symbols", [])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]

    results: list[dict] = []
    for sym in symbols[:20]:
        try:
            articles = await _fetch_company_news(sym)
            score, article_count = _score_headlines(articles)
            results.append(
                {
                    "symbol": sym,
                    "score": score,
                    "articles": article_count,
                }
            )
        except httpx.HTTPError as exc:
            logger.warning("Finnhub sentiment error for %s: %s", sym, exc)

    return {"results": results}


async def news_symbol(params: dict[str, Any]) -> dict:
    """Fetch recent news articles for a symbol.

    Params:
        symbol: str — single ticker.

    Returns:
        {"articles": [{"headline": str, "source": str, "url": str}, ...]}
    """
    symbol: str = params.get("symbol", "AAPL")
    try:
        articles = await _fetch_company_news(symbol)
        return {"articles": articles}
    except httpx.HTTPError as exc:
        logger.warning("Finnhub news error for %s: %s", symbol, exc)
        return {"articles": []}


TOOLS: dict[str, Any] = {
    "sentiment": sentiment,
    "news_symbol": news_symbol,
}
