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
import secrets
import time
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from db import (
    get_alert_frequency,
    get_recent_alerts,
    get_summary_stats,
    get_symbol_performance,
    get_winrate_by_bucket,
)
from log_config import configure_logging

configure_logging()
logger = logging.getLogger(__name__)

_DASHBOARD_CACHE_TTL: int = int(os.environ.get("DASHBOARD_CACHE_TTL_SECONDS", "60"))
_DASHBOARD_POLL_INTERVAL_MS: int = int(os.environ.get("DASHBOARD_POLL_INTERVAL_MS", "30000"))


def _cache_get(key: str) -> str | None:
    try:
        from redis_client import get_redis

        raw = get_redis().get(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            return raw.decode()
        return str(raw)
    except Exception:  # noqa: BLE001
        return None


def _cache_set(key: str, value: str, ttl: int = _DASHBOARD_CACHE_TTL) -> None:
    try:
        from redis_client import get_redis

        get_redis().setex(key, ttl, value)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Dashboard cache set failed: %s", exc)


def _cached_json(key: str, builder: Any) -> dict[str, Any]:
    import json as _json

    cached = _cache_get(key)
    if cached:
        parsed = _json.loads(cached)
        if isinstance(parsed, dict):
            return parsed
    data = builder()
    if not isinstance(data, dict):
        data = {"data": data}
    try:
        _cache_set(key, _json.dumps(data, default=str))
    except (TypeError, ValueError):
        pass
    return data


# ── Authentication ───────────────────────────────────────────────────────

DASHBOARD_API_KEY: str | None = os.getenv("DASHBOARD_API_KEY")
DASHBOARD_REQUIRE_AUTH: bool = os.getenv("DASHBOARD_REQUIRE_AUTH", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def _require_api_key(
    request: Request,
    api_key: str | None = Depends(_api_key_header),
) -> None:
    """Reject requests when auth is required and the header is missing/wrong."""
    if DASHBOARD_REQUIRE_AUTH and not DASHBOARD_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DASHBOARD_API_KEY must be set when DASHBOARD_REQUIRE_AUTH=true",
        )
    if DASHBOARD_API_KEY is None:
        return  # auth disabled — allow (dev mode)
    if not api_key or not secrets.compare_digest(api_key, DASHBOARD_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


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
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler — returns structured JSON 500, never raw stack traces."""
    logger.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


_cors_origins = [
    o.strip() for o in os.getenv("DASHBOARD_CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()
]
if any(o == "*" for o in _cors_origins):
    raise RuntimeError("DASHBOARD_CORS_ORIGINS=* is forbidden — set an explicit allow-list of origins.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET"],
    allow_headers=["Content-Type", "X-API-Key"],
)


class _RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log method, path, status code, and latency for every request."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        """Process a request and log its result."""
        import time as _time

        start = _time.monotonic()
        response = await call_next(request)
        elapsed_ms = (_time.monotonic() - start) * 1000
        logger.info(
            "HTTP %s %s %d %.1fms client=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            request.client.host if request.client else "-",
        )
        return response


app.add_middleware(_RequestLoggingMiddleware)

router = APIRouter(prefix="/api", dependencies=[Depends(_require_api_key)])

_RATE_LIMIT = "60/minute"


# ── Health & Auth Probes (outside the authenticated router) ──────────


@app.get("/health")
def health_check() -> dict[str, str]:
    """Unauthenticated liveness probe for k8s / docker healthcheck."""
    return {"status": "ok"}


@app.get("/metrics")
def prometheus_metrics() -> Response:
    """Unauthenticated Prometheus scrape endpoint."""
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/auth/test", dependencies=[Depends(_require_api_key)])
def auth_test() -> dict[str, Any]:
    """Authenticated probe to validate API key."""
    return {"authenticated": True, "timestamp": datetime.utcnow().isoformat()}


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
def api_alerts(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """Return recent alerts with pagination."""
    rows = get_recent_alerts(limit + offset)
    return _clean_rows(rows[offset : offset + limit])


@router.get("/health")
@limiter.limit(_RATE_LIMIT)
def api_health(request: Request) -> dict[str, Any]:
    """Container health, Redis latency, last pipeline run."""

    def _build() -> dict[str, Any]:
        redis_latency_ms: float | None = None
        redis_ok = False
        try:
            from redis_client import get_redis

            start = time.monotonic()
            get_redis().ping()
            redis_latency_ms = round((time.monotonic() - start) * 1000, 2)
            redis_ok = True
        except Exception:  # noqa: BLE001
            redis_ok = False

        last_run: float | None = None
        try:
            from prometheus_client import REGISTRY

            for metric in REGISTRY.collect():
                if metric.name == "pipeline_last_run_timestamp":
                    for sample in metric.samples:
                        last_run = sample.value
        except Exception:  # noqa: BLE001
            pass

        return {
            "status": "live" if redis_ok else "degraded",
            "redis_ok": redis_ok,
            "redis_latency_ms": redis_latency_ms,
            "last_pipeline_run_ts": last_run,
        }

    return _cached_json("dashboard:api:health", _build)


@router.get("/session-stats")
@limiter.limit(_RATE_LIMIT)
def api_session_stats(
    request: Request,
    timeframe: str = Query(default="15m"),
    date: str = Query(default="today"),
) -> dict[str, Any]:
    """Gate rejection counts and pass rates from Redis session stats."""

    def _build() -> dict[str, Any]:
        from zoneinfo import ZoneInfo

        if date == "today":
            session_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        else:
            session_date = date
        key = f"session:stats:{session_date}:{timeframe}"
        try:
            from redis_client import get_redis

            raw = get_redis().hgetall(key) or {}
            stats: dict[str, int] = {}
            raw_items: Iterable[tuple[Any, Any]]
            if isinstance(raw, dict):
                raw_items = raw.items()
            else:
                raw_items = ()
            for k, v in raw_items:
                field = k.decode() if isinstance(k, bytes) else str(k)
                try:
                    stats[field] = int(v)
                except (TypeError, ValueError):
                    continue
            return {"date": session_date, "timeframe": timeframe, "stats": stats}
        except Exception as exc:  # noqa: BLE001
            return {"date": session_date, "timeframe": timeframe, "stats": {}, "error": str(exc)}

    return _cached_json(f"dashboard:api:session-stats:{timeframe}:{date}", _build)


@router.get("/kpis")
@limiter.limit(_RATE_LIMIT)
def api_kpis(request: Request) -> dict[str, Any]:
    """Aggregated KPIs for dashboard header."""

    def _build() -> dict[str, Any]:
        summary = get_summary_stats()
        rejected = 0
        passed = 0
        try:
            from zoneinfo import ZoneInfo

            from redis_client import get_redis

            session_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
            for tf in ("15m", "1h"):
                key = f"session:stats:{session_date}:{tf}"
                raw = get_redis().hgetall(key) or {}
                if not isinstance(raw, dict):
                    continue
                for k, v in raw.items():
                    field = k.decode() if isinstance(k, bytes) else k
                    if field == "alerts_rejected":
                        rejected += int(v)
                    if field == "alerts_passed_total":
                        passed += int(v)
        except Exception:  # noqa: BLE001
            pass
        total_gate = rejected + passed
        rejection_rate = round(rejected / total_gate, 4) if total_gate else 0.0
        return {
            **summary,
            "gate_rejection_rate": rejection_rate,
            "poll_interval_ms": _DASHBOARD_POLL_INTERVAL_MS,
        }

    return _cached_json("dashboard:api:kpis", _build)


@router.get("/circuit-breaker")
@limiter.limit(_RATE_LIMIT)
def api_circuit_breaker(request: Request) -> dict[str, Any]:
    """Current Redis circuit breaker state for WATCH decay."""
    try:
        from validate_and_filter import is_redis_circuit_open

        open_state = is_redis_circuit_open()
        return {"redis_circuit_open": open_state, "status": "degraded" if open_state else "ok"}
    except ImportError:
        return {"redis_circuit_open": False, "status": "unknown"}


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
