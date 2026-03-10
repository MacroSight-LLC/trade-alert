"""Lightweight healthcheck worker for trade-alert.

Verifies Redis, Postgres, MCP servers, and recent alert activity.
Sends ops messages on infrastructure failures.
Implements SSOT §13.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg2
import redis

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from notifier_and_logger import send_ops_message

logger = logging.getLogger(__name__)

REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")
DATABASE_URL: str | None = os.getenv("DATABASE_URL")

# SSOT §3: all 10 MCP services with /health endpoints
MCP_SERVICES: list[tuple[str, str]] = [
    ("tradingview-mcp", "http://tradingview-mcp:8001/health"),
    ("polygon-mcp", "http://polygon-mcp:8002/health"),
    ("discord-mcp", "http://discord-mcp:8003/health"),
    ("finnhub-mcp", "http://finnhub-mcp:8004/health"),
    ("rot-mcp", "http://rot-mcp:8005/health"),
    ("crypto-orderbook-mcp", "http://crypto-orderbook-mcp:8006/health"),
    ("coingecko-mcp", "http://coingecko-mcp:8007/health"),
    ("trading-mcp", "http://trading-mcp:8008/health"),
    ("fred-mcp", "http://fred-mcp:8009/health"),
    ("spamshield-mcp", "http://spamshield-mcp:8010/health"),
]


HEALTH_LOG_PATH: Path = Path(os.getenv("HEALTH_LOG_DIR", "logs")) / "health.jsonl"


def _append_jsonl(record: dict) -> None:
    """Append a single JSON record to the structured health log.

    Creates the log directory if it doesn't exist. Fails silently
    so logging never breaks the healthcheck itself.

    Args:
        record: Dict to serialize as one JSONL line.
    """
    try:
        HEALTH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HEALTH_LOG_PATH.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        logger.warning("Failed to write health.jsonl — %s", exc)


def check_redis() -> bool:
    """Ping Redis and return reachability status.

    Returns:
        True if Redis responds to PING, False otherwise.
    """
    try:
        r = redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        logger.info("Healthcheck: Redis OK")
        return True
    except redis.RedisError as exc:
        logger.error("Healthcheck: Redis FAILED — %s", exc)
        return False


def check_postgres() -> bool:
    """Connect to Postgres and run SELECT 1.

    Returns:
        True if the query succeeds, False otherwise.
    """
    if not DATABASE_URL:
        logger.error("Healthcheck: DATABASE_URL not set")
        return False
    try:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            logger.info("Healthcheck: Postgres OK")
            return True
        finally:
            conn.close()
    except psycopg2.Error as exc:
        logger.error("Healthcheck: Postgres FAILED — %s", exc)
        return False


def check_recent_alerts(timeframe: str) -> bool:
    """Query Postgres for recent alert activity.

    This is a soft check — returns True even when no alerts exist
    (silence is valid). Returns False only on database errors.

    Args:
        timeframe: Pipeline timeframe label for logging context.

    Returns:
        True if the query succeeds (regardless of row count),
        False on database error.
    """
    try:
        from db import get_recent_alerts

        alerts = get_recent_alerts(limit=1)
        logger.info("Healthcheck: %d recent alerts found", len(alerts))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Healthcheck: recent alerts query failed — %s", exc)
        return False


def check_mcps(timeout: float = 5.0) -> tuple[list[str], list[str]]:
    """Hit /health on every MCP service defined in SSOT §3.

    Args:
        timeout: HTTP request timeout in seconds per service.

    Returns:
        Tuple of (healthy_names, unhealthy_names).
    """
    healthy: list[str] = []
    unhealthy: list[str] = []
    for name, url in MCP_SERVICES:
        try:
            resp = httpx.get(url, timeout=timeout)
            if resp.status_code == 200:
                healthy.append(name)
                logger.info("Healthcheck: MCP %s OK", name)
            else:
                unhealthy.append(name)
                logger.warning("Healthcheck: MCP %s returned %d", name, resp.status_code)
        except httpx.HTTPError as exc:
            unhealthy.append(name)
            logger.warning("Healthcheck: MCP %s unreachable — %s", name, exc)
    return healthy, unhealthy


def run_healthcheck(timeframe: str) -> None:
    """Run all healthchecks and alert ops on infrastructure failures.

    Checks Redis, Postgres, MCP services, and recent alert activity.
    Sends a descriptive failure message to the ops Discord channel
    when critical infrastructure is unhealthy (SSOT §13).

    Args:
        timeframe: Pipeline timeframe label (e.g. ``"15m"``, ``"1h"``).
    """
    try:
        redis_ok = check_redis()
        pg_ok = check_postgres()
        healthy_mcps, unhealthy_mcps = check_mcps()
        check_recent_alerts(timeframe)

        # SSOT §13: structured JSONL log entry
        _append_jsonl(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "timeframe": timeframe,
                "redis_ok": redis_ok,
                "pg_ok": pg_ok,
                "mcp_healthy": healthy_mcps,
                "mcp_unhealthy": unhealthy_mcps,
            }
        )

        redis_icon = "✅" if redis_ok else "❌"
        pg_icon = "✅" if pg_ok else "❌"
        mcp_icon = "✅" if not unhealthy_mcps else "❌"

        failures: list[str] = []
        if not redis_ok:
            failures.append(f"Redis={redis_icon}")
        if not pg_ok:
            failures.append(f"Postgres={pg_icon}")
        if unhealthy_mcps:
            failures.append(f"MCPs={mcp_icon} ({', '.join(unhealthy_mcps)})")

        if failures:
            msg = f"⚠️ Healthcheck FAILED [{timeframe}]: {' | '.join(failures)}"
            logger.warning(msg)
            send_ops_message(msg)
        else:
            logger.info("Healthcheck OK [%s]", timeframe)
    except Exception as exc:  # noqa: BLE001
        logger.error("Healthcheck unexpected error: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_healthcheck("manual")
    print("Healthcheck complete ✅")
