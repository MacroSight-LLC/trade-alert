"""Mock MCP server for local development and testing.

Serves synthetic /health and tool endpoints on ports 8001-8010,
matching the 10 MCP services defined in SSOT §5.

Usage:
    python scripts/mock_mcp_server.py          # all 10 ports
    python scripts/mock_mcp_server.py 8001     # single port
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

MCP_SERVICES: dict[int, str] = {
    8001: "TradingView MCP",
    8002: "Polygon MCP",
    8003: "Discord MCP",
    8004: "Finnhub MCP",
    8005: "ROT MCP",
    8006: "crypto-orderbook MCP",
    8007: "CoinGecko MCP",
    8008: "trading-mcp server",
    8009: "FRED bundle MCP",
    8010: "SpamShieldpro MCP",
}

# Synthetic tool responses per port
MOCK_RESPONSES: dict[int, dict] = {
    8001: {
        "bollinger_scan": {
            "results": [
                {"symbol": "AAPL", "bb_position": 0.15, "squeeze": True, "timeframe": "15m"},
                {"symbol": "NVDA", "bb_position": 0.92, "squeeze": False, "timeframe": "15m"},
            ]
        },
        "rsi_scan": {
            "results": [
                {"symbol": "AAPL", "rsi": 28.5, "timeframe": "15m"},
                {"symbol": "TSLA", "rsi": 72.3, "timeframe": "15m"},
            ]
        },
    },
    8002: {
        "unusual_activity": {
            "results": [
                {
                    "symbol": "NVDA",
                    "type": "options_sweep",
                    "premium": 2500000,
                    "strike": 900,
                    "expiry": "0DTE",
                },
            ]
        },
        "aggs": {
            "results": [
                {"symbol": "AAPL", "volume": 85000000, "avg_volume": 55000000, "avg_20d_volume": 55000000, "close": 185.50},
                {"symbol": "NVDA", "volume": 120000000, "avg_volume": 60000000, "avg_20d_volume": 60000000, "close": 875.00},
            ]
        },
    },
    8003: {
        "send_rich_embed": {"status": "sent", "message_id": "mock-12345"},
    },
    8004: {
        "sentiment": {
            "results": [
                {"symbol": "AAPL", "score": 0.45, "articles": 12},
                {"symbol": "NVDA", "score": 0.72, "articles": 28},
            ]
        },
        "news_symbol": {"articles": [{"headline": "Mock headline", "source": "mock"}]},
    },
    8005: {
        "trending_tickers": {
            "results": [
                {"symbol": "NVDA", "mentions": 450, "sentiment": "strong_bullish"},
                {"symbol": "TSLA", "mentions": 320, "sentiment": "bullish"},
            ]
        },
        "options_flow": {
            "results": [
                {"symbol": "NVDA", "flow_type": "call_sweep", "premium": 1200000},
            ]
        },
    },
    8006: {
        "imbalance": {
            "results": [
                {"symbol": "BTC", "bid_imbalance": 0.65, "price": 67000.0},
                {"symbol": "ETH", "bid_imbalance": 0.55, "price": 3500.0},
            ]
        },
        "depth": {"results": [{"symbol": "BTC", "bid_depth": 150.0, "ask_depth": 120.0}]},
    },
    8007: {
        "top_gainers": {
            "results": [
                {"symbol": "SOL", "change_24h": 8.5, "market_cap": 65000000000},
                {"symbol": "AVAX", "change_24h": 5.2, "market_cap": 12000000000},
            ]
        },
        "dominance": {"results": [{"btc": 52.1, "eth": 17.3}]},
    },
    8008: {
        "screen": {
            "results": [
                {"symbol": "AAPL", "market_cap": 3000000000000, "pe_ratio": 28.5},
                {"symbol": "MSFT", "market_cap": 3100000000000, "pe_ratio": 35.2},
            ]
        },
        "insiders": {"results": [{"symbol": "NVDA", "type": "buy", "shares": 10000}]},
    },
    8009: {
        "vix_level": {"value": 14.2, "vix_level": 14.2},
        "yield_curve": {"value": 15.0, "spread_bps": 15.0},
        "fed_funds": {"value": 5.33, "rate": 5.33},
    },
    8010: {
        "classify_text": {"is_spam": False, "confidence": 0.95},
    },
}


def create_app(port: int) -> FastAPI:
    """Create a FastAPI app for a specific MCP port."""
    service_name = MCP_SERVICES.get(port, f"Unknown MCP ({port})")
    app = FastAPI(title=service_name)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "healthy",
                "service": service_name,
                "port": port,
                "mock": True,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    @app.post("/tool/{tool_name}")
    @app.get("/tool/{tool_name}")
    async def tool_call(tool_name: str) -> JSONResponse:
        responses = MOCK_RESPONSES.get(port, {})
        if tool_name in responses:
            return JSONResponse(responses[tool_name])
        return JSONResponse(
            {"error": f"Unknown tool: {tool_name}", "available": list(responses.keys())},
            status_code=404,
        )

    return app


async def run_servers(ports: list[int]) -> None:
    """Run mock MCP servers on the specified ports."""
    import uvicorn

    tasks = []
    for port in ports:
        app = create_app(port)
        config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
        server = uvicorn.Server(config)
        tasks.append(asyncio.create_task(server.serve()))
        logger.info("Mock %s on :%d", MCP_SERVICES.get(port, "?"), port)

    await asyncio.gather(*tasks)


def main() -> None:
    """Entry point for mock MCP server."""
    if len(sys.argv) > 1:
        ports = [int(p) for p in sys.argv[1:]]
    else:
        ports = list(MCP_SERVICES.keys())

    logger.info("Starting mock MCP servers on ports: %s", ports)
    asyncio.run(run_servers(ports))


if __name__ == "__main__":
    main()
