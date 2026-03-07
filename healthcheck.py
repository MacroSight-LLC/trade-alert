"""Lightweight healthcheck worker for trade-alert.

Verifies Redis, Postgres, and recent alert activity.
Sends ops messages on infrastructure failures.
Implements SSOT §13.
"""

from __future__ import annotations

import logging
import os

import psycopg2
import redis

from notifier_and_logger import send_ops_message

logger = logging.getLogger(__name__)

REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")
DATABASE_URL: str | None = os.getenv("DATABASE_URL")


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


def run_healthcheck(timeframe: str) -> None:
    """Run all healthchecks and alert ops on infrastructure failures.

    Checks Redis, Postgres, and recent alert activity. If Redis or
    Postgres is unreachable, sends a descriptive failure message to
    the ops Discord channel.

    Args:
        timeframe: Pipeline timeframe label (e.g. ``"15m"``, ``"1h"``).
    """
    try:
        redis_ok = check_redis()
        pg_ok = check_postgres()
        check_recent_alerts(timeframe)

        redis_icon = "✅" if redis_ok else "❌"
        pg_icon = "✅" if pg_ok else "❌"

        if not redis_ok or not pg_ok:
            msg = f"⚠️ Healthcheck FAILED [{timeframe}]: Redis={redis_icon} Postgres={pg_icon}"
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
