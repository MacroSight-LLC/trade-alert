"""Real MCP server framework.

Each MCP service is a FastAPI app that exposes /health and /tool/{tool_name}
endpoints.  The server discovers its port from the first CLI argument and
loads the corresponding handler module from mcp_servers/.

Usage:
    python scripts/mcp_server.py 8001          # TradingView MCP
    python scripts/mcp_server.py 8002          # Polygon MCP
    ...

Handler modules are in scripts/mcp_servers/<name>.py and must export:
    SERVICE_NAME: str
    TOOLS: dict[str, Callable[[dict], Awaitable[dict|list]]]

Falls back to mock responses if the handler module raises or if
the required API key is not set (graceful degradation).
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Map port → handler module name (relative to scripts.mcp_servers)
PORT_TO_MODULE: dict[int, str] = {
    8001: "tradingview",
    8002: "polygon_io",
    8003: "discord_mcp",
    8004: "finnhub_mcp",
    8005: "rot",
    8006: "crypto_orderbook",
    8007: "coingecko",
    8008: "trading",
    8009: "fred",
    8010: "spamshield",
}


def create_app(port: int) -> FastAPI:
    """Create a FastAPI app for the given MCP port."""
    module_name = PORT_TO_MODULE.get(port)
    if module_name is None:
        raise ValueError(f"No handler module registered for port {port}")

    mod = importlib.import_module(f"mcp_servers.{module_name}")
    service_name: str = getattr(mod, "SERVICE_NAME", f"MCP-{port}")
    tools: dict[str, Any] = getattr(mod, "TOOLS")

    app = FastAPI(title=service_name)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({
            "status": "healthy",
            "service": service_name,
            "port": port,
            "mock": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    @app.post("/tool/{tool_name}")
    @app.get("/tool/{tool_name}")
    async def tool_call(tool_name: str, request: Request) -> JSONResponse:
        handler = tools.get(tool_name)
        if handler is None:
            return JSONResponse(
                {"error": f"Unknown tool: {tool_name}", "available": list(tools.keys())},
                status_code=404,
            )
        try:
            if request.method == "POST":
                try:
                    params = await request.json()
                except Exception:
                    params = {}
            else:
                params = dict(request.query_params)
            result = await handler(params)
            return JSONResponse(result)
        except Exception as exc:
            logger.exception("Tool %s failed: %s", tool_name, exc)
            return JSONResponse(
                {"error": str(exc), "tool": tool_name},
                status_code=502,
            )

    return app


def main() -> None:
    """Entry point — read port from CLI arg and start server."""
    if len(sys.argv) < 2:
        print("Usage: python mcp_server.py <port>")
        sys.exit(1)

    port = int(sys.argv[1])
    app = create_app(port)
    logger.info("Starting %s on :%d", PORT_TO_MODULE.get(port, "?"), port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
