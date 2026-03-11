"""Crypto Orderbook MCP — real exchange orderbook data.

Tools: imbalance, depth
Uses Binance public API (no key required).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERVICE_NAME = "crypto-orderbook MCP"

BINANCE_URL = "https://api.binance.com/api/v3"
TIMEOUT = 10.0

# Map common crypto tickers to Binance trading pairs
_PAIR_MAP: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "AVAX": "AVAXUSDT",
    "DOGE": "DOGEUSDT",
    "ADA": "ADAUSDT",
    "DOT": "DOTUSDT",
    "MATIC": "MATICUSDT",
    "LINK": "LINKUSDT",
    "XRP": "XRPUSDT",
    "BNB": "BNBUSDT",
    "ATOM": "ATOMUSDT",
    "UNI": "UNIUSDT",
    "NEAR": "NEARUSDT",
    "ARB": "ARBUSDT",
    "OP": "OPUSDT",
}


def _to_pair(symbol: str) -> str:
    """Convert a ticker symbol to a Binance trading pair."""
    sym = symbol.upper().replace("-USD", "").replace("USDT", "")
    return _PAIR_MAP.get(sym, f"{sym}USDT")


async def imbalance(params: dict[str, Any]) -> dict:
    """Compute bid/ask imbalance from top orderbook levels.

    Params:
        symbols: list[str] — crypto tickers (e.g. ["BTC", "ETH"]).

    Returns:
        {"results": [{"symbol", "bid_imbalance", "price"}, ...]}
        bid_imbalance: -1..+1 (positive = bids dominate)
    """
    symbols: list[str] = params.get("symbols", [])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]

    results: list[dict] = []
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for sym in symbols[:15]:
            pair = _to_pair(sym)
            try:
                resp = await client.get(
                    f"{BINANCE_URL}/depth",
                    params={"symbol": pair, "limit": 20},
                )
                resp.raise_for_status()
                book = resp.json()

                # Sum bid and ask depth (quantity * price for top 20 levels)
                bid_total = sum(float(b[0]) * float(b[1]) for b in book.get("bids", []))
                ask_total = sum(float(a[0]) * float(a[1]) for a in book.get("asks", []))
                total = bid_total + ask_total

                if total > 0:
                    # Imbalance: -1 (asks dominate) to +1 (bids dominate)
                    imb = (bid_total - ask_total) / total
                else:
                    imb = 0.0

                # Get current price from best bid/ask midpoint
                best_bid = float(book["bids"][0][0]) if book.get("bids") else 0
                best_ask = float(book["asks"][0][0]) if book.get("asks") else 0
                mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0

                results.append({
                    "symbol": sym.upper(),
                    "bid_imbalance": round(imb, 4),
                    "price": round(mid, 2),
                })
            except httpx.HTTPError as exc:
                logger.warning("Binance orderbook error for %s: %s", sym, exc)

    return {"results": results}


async def depth(params: dict[str, Any]) -> dict:
    """Fetch total bid/ask depth for symbols.

    Params:
        symbols: list[str] — crypto tickers.

    Returns:
        {"results": [{"symbol", "bid_depth", "ask_depth"}, ...]}
    """
    symbols: list[str] = params.get("symbols", [])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]

    results: list[dict] = []
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for sym in symbols[:15]:
            pair = _to_pair(sym)
            try:
                resp = await client.get(
                    f"{BINANCE_URL}/depth",
                    params={"symbol": pair, "limit": 100},
                )
                resp.raise_for_status()
                book = resp.json()

                bid_depth = sum(float(b[1]) for b in book.get("bids", []))
                ask_depth = sum(float(a[1]) for a in book.get("asks", []))

                results.append({
                    "symbol": sym.upper(),
                    "bid_depth": round(bid_depth, 4),
                    "ask_depth": round(ask_depth, 4),
                })
            except httpx.HTTPError as exc:
                logger.warning("Binance depth error for %s: %s", sym, exc)

    return {"results": results}


TOOLS: dict[str, Any] = {
    "imbalance": imbalance,
    "depth": depth,
}
