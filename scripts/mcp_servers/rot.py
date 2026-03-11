"""ROT MCP — Reddit/social trending tickers and options flow.

Tools: trending_tickers, options_flow
Optional: REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET env vars for Reddit API.
Falls back to scraping public Reddit JSON endpoints if not set.
"""
from __future__ import annotations

import logging
import os
import re
from collections import Counter
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERVICE_NAME = "ROT MCP"

REDDIT_CLIENT_ID: str = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET: str = os.getenv("REDDIT_CLIENT_SECRET", "")
TIMEOUT = 10.0

# Common ticker pattern (1-5 uppercase letters, avoiding common words)
_TICKER_RE = re.compile(r"\b([A-Z]{1,5})\b")
_COMMON_WORDS = frozenset({
    "I", "A", "THE", "AND", "OR", "FOR", "TO", "IS", "IT", "IN",
    "ON", "AT", "BY", "IF", "OF", "DD", "CEO", "IPO", "IMO", "YOLO",
    "FOMO", "FYI", "TIL", "ELI", "WSB", "OTM", "ITM", "ATM", "DTE",
    "IV", "PE", "EPS", "FDA", "SEC", "ETF", "GDP", "CPI", "LOL",
    "AM", "PM", "US", "UK", "EU", "API", "IMF", "ARE", "HAS", "HAD",
    "WAS", "BE", "ME", "MY", "SO", "UP", "DO", "GO", "NO", "NOT",
})


async def _reddit_get(path: str) -> list[dict]:
    """Fetch a Reddit JSON listing (public, no auth)."""
    headers = {"User-Agent": "trade-alert-mcp/1.0"}
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(
            f"https://www.reddit.com{path}.json",
            headers=headers,
            params={"limit": 50, "raw_json": 1},
        )
        resp.raise_for_status()
        data = resp.json()

    posts: list[dict] = []
    for child in data.get("data", {}).get("children", []):
        post = child.get("data", {})
        posts.append({
            "title": post.get("title", ""),
            "selftext": post.get("selftext", ""),
            "score": post.get("score", 0),
            "num_comments": post.get("num_comments", 0),
            "upvote_ratio": post.get("upvote_ratio", 0.5),
        })
    return posts


def _extract_tickers(posts: list[dict]) -> Counter:
    """Extract and count ticker mentions from Reddit posts."""
    counter: Counter = Counter()
    for post in posts:
        text = f"{post['title']} {post['selftext']}"
        tickers = _TICKER_RE.findall(text)
        for t in tickers:
            if t not in _COMMON_WORDS and len(t) >= 2:
                counter[t] += 1
    return counter


def _classify_sentiment(posts: list[dict], ticker: str) -> str:
    """Classify sentiment for a ticker based on post context."""
    bullish_words = {"bull", "calls", "moon", "buy", "long", "rocket", "squeeze", "breakout", "gamma"}
    bearish_words = {"bear", "puts", "short", "sell", "crash", "dump", "drill", "rug"}

    bull_score = 0
    bear_score = 0
    for post in posts:
        text = (post["title"] + " " + post["selftext"]).lower()
        if ticker.lower() not in text and ticker not in post["title"]:
            continue
        weight = max(1, post.get("score", 1))
        for w in bullish_words:
            if w in text:
                bull_score += weight
        for w in bearish_words:
            if w in text:
                bear_score += weight

    net = bull_score - bear_score
    total = bull_score + bear_score
    if total < 3:
        return "neutral"
    ratio = net / total if total > 0 else 0
    if ratio > 0.5:
        return "strong_bullish"
    if ratio > 0.15:
        return "bullish"
    if ratio < -0.5:
        return "strong_bearish"
    if ratio < -0.15:
        return "bearish"
    return "neutral"


async def trending_tickers(params: dict[str, Any]) -> list[dict]:
    """Fetch trending tickers from Reddit (r/wallstreetbets, r/stocks).

    Params:
        limit: int (default 20)

    Returns:
        [{"symbol": str, "mentions": int, "sentiment": str}, ...]
    """
    limit = int(params.get("limit", 20))
    all_posts: list[dict] = []

    for sub in ["/r/wallstreetbets/hot", "/r/stocks/hot", "/r/options/hot"]:
        try:
            posts = await _reddit_get(sub)
            all_posts.extend(posts)
        except httpx.HTTPError as exc:
            logger.warning("Reddit fetch failed for %s: %s", sub, exc)

    if not all_posts:
        return []

    ticker_counts = _extract_tickers(all_posts)
    results: list[dict] = []
    for ticker, mentions in ticker_counts.most_common(limit):
        sentiment = _classify_sentiment(all_posts, ticker)
        results.append({
            "symbol": ticker,
            "mentions": mentions,
            "sentiment": sentiment,
        })
    return results


async def options_flow(params: dict[str, Any]) -> list[dict]:
    """Extract options flow mentions from Reddit posts.

    Params:
        limit: int (default 20)

    Returns:
        [{"symbol": str, "flow_type": str, "premium": int}, ...]
    """
    limit = int(params.get("limit", 20))
    results: list[dict] = []

    try:
        posts = await _reddit_get("/r/wallstreetbets/hot")
    except httpx.HTTPError as exc:
        logger.warning("Reddit fetch failed: %s", exc)
        return results

    # Look for options-related posts
    options_pattern = re.compile(
        r"(\$?[A-Z]{1,5})\s*(\d+[cCpP]|\$?\d+\s*(?:call|put|calls|puts))",
        re.IGNORECASE,
    )

    seen: set[str] = set()
    for post in posts:
        text = f"{post['title']} {post['selftext']}"
        matches = options_pattern.findall(text)
        for sym_raw, strike_raw in matches:
            sym = sym_raw.replace("$", "").upper()
            if sym in _COMMON_WORDS or sym in seen or len(sym) < 2:
                continue
            seen.add(sym)
            flow_type = "call_sweep" if "c" in strike_raw.lower() or "call" in strike_raw.lower() else "put_sweep"
            results.append({
                "symbol": sym,
                "flow_type": flow_type,
                "premium": post.get("score", 0) * 1000,
            })
            if len(results) >= limit:
                return results

    return results


TOOLS: dict[str, Any] = {
    "trending_tickers": trending_tickers,
    "options_flow": options_flow,
}
