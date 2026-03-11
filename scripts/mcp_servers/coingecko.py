"""CoinGecko MCP — real API integration.

Tools: top_gainers, dominance
No API key required for free tier.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERVICE_NAME = "CoinGecko MCP"

BASE_URL = "https://api.coingecko.com/api/v3"
TIMEOUT = 10.0


async def top_gainers(params: dict[str, Any]) -> list[dict]:
    """Fetch top gaining coins by 24h price change.

    Params:
        limit: int (default 20)

    Returns:
        [{"symbol": str, "change_24h": float, "market_cap": int}, ...]
    """
    limit = int(params.get("limit", 20))
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            f"{BASE_URL}/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": min(limit * 2, 100),
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "24h",
            },
        )
        resp.raise_for_status()
        coins = resp.json()

    # Sort by absolute 24h change descending, take top gainers
    coins.sort(
        key=lambda c: abs(c.get("price_change_percentage_24h", 0) or 0),
        reverse=True,
    )

    results: list[dict] = []
    for coin in coins[:limit]:
        results.append({
            "symbol": (coin.get("symbol") or "").upper(),
            "change_24h": coin.get("price_change_percentage_24h", 0.0) or 0.0,
            "market_cap": coin.get("market_cap", 0) or 0,
            "name": coin.get("name", ""),
            "price": coin.get("current_price", 0.0),
        })
    return results


async def dominance(params: dict[str, Any]) -> dict:
    """Fetch BTC and ETH market dominance.

    Returns:
        {"btc": float, "eth": float}
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(f"{BASE_URL}/global")
        resp.raise_for_status()
        data = resp.json().get("data", {})
        market_cap_pct = data.get("market_cap_percentage", {})

    return {
        "btc": round(market_cap_pct.get("btc", 0.0), 2),
        "eth": round(market_cap_pct.get("eth", 0.0), 2),
    }


TOOLS: dict[str, Any] = {
    "top_gainers": top_gainers,
    "dominance": dominance,
}
