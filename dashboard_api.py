"""FastAPI dashboard API for trade-alert analytics.

Serves the analytics dashboard frontend and provides JSON endpoints
for winrate, alert frequency, symbol performance, and summary stats.
Implements SSOT Phase 9 — Dashboard.

Usage:
    uvicorn dashboard_api:app --host 0.0.0.0 --port 8080 --reload
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from db import (
    get_alert_frequency,
    get_recent_alerts,
    get_summary_stats,
    get_symbol_performance,
    get_winrate_by_bucket,
)

# ── Authentication ───────────────────────────────────────────────────────

DASHBOARD_API_KEY: str | None = os.getenv("DASHBOARD_API_KEY")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def _require_api_key(
    request: Request,
    api_key: str | None = Depends(_api_key_header),
) -> None:
    """Reject requests when DASHBOARD_API_KEY is set and the header is missing/wrong."""
    if DASHBOARD_API_KEY is None:
        return  # auth disabled — allow (dev mode)
    if not api_key or api_key != DASHBOARD_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


logger = logging.getLogger(__name__)

# ── Pydantic response models ────────────────────────────────────────────


class SummaryResponse(BaseModel):
    """Aggregate dashboard KPIs."""

    total_alerts: int = 0
    resolved: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    overall_winrate: float | None = None
    avg_edge: float | None = None
    avg_pnl: float | None = None
    alerts_today: int = 0
    kpi_winrate_70: float | None = None


class WinrateBucket(BaseModel):
    """Winrate data for one edge_probability bucket."""

    bucket: float
    total: int
    wins: int
    avg_pnl: float | None = None


class AlertFrequencyDay(BaseModel):
    """Alert counts for a single day."""

    date: str
    total: int
    longs: int = 0
    shorts: int = 0
    watches: int = 0


class SymbolPerformance(BaseModel):
    """Per-symbol performance metrics."""

    symbol: str
    total: int
    wins: int = 0
    losses: int = 0
    winrate: float | None = None
    avg_edge: float | None = None
    avg_pnl: float | None = None


# ── Helpers ──────────────────────────────────────────────────────────────


def _serialize(obj: Any) -> Any:
    """Convert Decimal/date/datetime to JSON-safe types."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return obj


def _clean_rows(rows: list[dict]) -> list[dict]:
    """Apply _serialize to every value in a list of dicts."""
    return [{k: _serialize(v) for k, v in row.items()} for row in rows]


def _clean_dict(d: dict) -> dict:
    """Apply _serialize to every value in a single dict."""
    return {k: _serialize(v) for k, v in d.items()}


# ── FastAPI app ──────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="Trade Alert Dashboard",
    description="Analytics dashboard for the trade-alert engine (SSOT Phase 9).",
    version="0.9.0",
)

app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})


app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("DASHBOARD_CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["GET"],
    allow_headers=["Content-Type", "X-API-Key"],
)

router = APIRouter(prefix="/api", dependencies=[Depends(_require_api_key)])

_RATE_LIMIT = "60/minute"


@router.get("/summary", response_model=SummaryResponse)
@limiter.limit(_RATE_LIMIT)
def api_summary(request: Request) -> dict:
    """Return aggregate dashboard KPIs."""
    return _clean_dict(get_summary_stats())


@router.get("/winrate", response_model=list[WinrateBucket])
@limiter.limit(_RATE_LIMIT)
def api_winrate(request: Request) -> list[dict]:
    """Return winrate by edge_probability bucket."""
    return _clean_rows(get_winrate_by_bucket())


@router.get("/frequency", response_model=list[AlertFrequencyDay])
@limiter.limit(_RATE_LIMIT)
def api_frequency(request: Request, days: int = Query(default=30, ge=1, le=365)) -> list[dict]:
    """Return daily alert counts."""
    return _clean_rows(get_alert_frequency(days))


@router.get("/symbols", response_model=list[SymbolPerformance])
@limiter.limit(_RATE_LIMIT)
def api_symbols(request: Request, limit: int = Query(default=20, ge=1, le=100)) -> list[dict]:
    """Return per-symbol performance."""
    return _clean_rows(get_symbol_performance(limit))


@router.get("/alerts")
@limiter.limit(_RATE_LIMIT)
def api_alerts(request: Request, limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
    """Return recent alerts."""
    return _clean_rows(get_recent_alerts(limit))


app.include_router(router)


# ── Serve the dashboard HTML ────────────────────────────────────────────

_DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"


@app.get("/", response_class=HTMLResponse)
def serve_dashboard() -> FileResponse:
    """Serve the single-file dashboard UI."""
    return FileResponse(_DASHBOARD_PATH, media_type="text/html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
