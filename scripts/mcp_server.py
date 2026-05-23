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

Falls back to error responses if the handler module raises or if
the required API key is not set (graceful degradation).
"""

from __future__ import annotations

import importlib
import logging
import sys
from datetime import UTC, datetime
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from log_config import configure_logging

configure_logging()
logger = logging.getLogger(__name__)

# Maximum request body size (10 MB)
_MAX_BODY_BYTES = 10 * 1024 * 1024

# Map port → handler module name (relative to scripts.mcp_servers)
PORT_TO_MODULE: dict[int, str] = {
    8001: "tradingview",
    8002: "polygon_io",
    8003: "discord_mcp",
    8004: "finnhub_mcp",
    8005: "rot",
    8006: "edgar_mcp",
    8007: "yfinance_mcp",
    8008: "trading",
    8009: "fred",
    8010: "spamshield",
    8011: "alpaca_mcp",
    8012: "timesfm_mcp",
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

    class _LimitBodyMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > _MAX_BODY_BYTES:
                return JSONResponse(
                    {"error": "Request body too large"},
                    status_code=413,
                )
            return await call_next(request)

    app.add_middleware(_LimitBodyMiddleware)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "healthy",
                "service": service_name,
                "port": port,
                "live": True,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

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
