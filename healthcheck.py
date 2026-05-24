"""Lightweight healthcheck worker for trade-alert.

Verifies Redis, Postgres, MCP servers, and recent alert activity.
Sends ops messages on infrastructure failures.
Implements SSOT §13.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from datetime import time as dt_time
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import psycopg2
import redis

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from constants import SNAPSHOT_KEY_PREFIX, SNAPSHOT_STALE_TTL_THRESHOLD, is_early_close, is_holiday
from langfuse_client import get_langfuse_client, register_langfuse_failure
from notifier_and_logger import send_ops_message
from redis_client import get_redis as _get_redis

logger = logging.getLogger(__name__)


def _resolve_database_url() -> str | None:
    """Return the effective Postgres DSN for the current runtime."""
    url = os.getenv("DATABASE_URL")
    if not url:
        return url

    if not os.path.exists("/.dockerenv"):
        return url

    if "localhost:5432" not in url and "127.0.0.1:5432" not in url:
        return url

    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        return url

    user = os.getenv("POSTGRES_USER", "trade_alert")
    host = os.getenv("POSTGRES_HOST", "postgres")
    db_name = os.getenv("POSTGRES_DB", "trade_alert")
    return f"postgresql://{user}:{password}@{host}:5432/{db_name}"


DATABASE_URL: str | None = _resolve_database_url()

# SSOT §3: all 12 MCP services with /health endpoints
# Uses env-var overrides matching pipeline_runner.py pattern
MCP_SERVICES: list[tuple[str, str]] = [
    ("tradingview-mcp", os.getenv("TRADINGVIEW_MCP_URL", "http://tradingview-mcp:8001") + "/health"),
    ("polygon-mcp", os.getenv("POLYGON_MCP_URL", "http://polygon-mcp:8002") + "/health"),
    ("discord-mcp", os.getenv("DISCORD_MCP_URL", "http://discord-mcp:8003") + "/health"),
    ("finnhub-mcp", os.getenv("FINNHUB_MCP_URL", "http://finnhub-mcp:8004") + "/health"),
    ("rot-mcp", os.getenv("ROT_MCP_URL", "http://rot-mcp:8005") + "/health"),
    ("edgar-mcp", os.getenv("EDGAR_MCP_URL", "http://edgar-mcp:8006") + "/health"),
    ("yfinance-mcp", os.getenv("YFINANCE_MCP_URL", "http://yfinance-mcp:8007") + "/health"),
    ("trading-mcp", os.getenv("TRADING_MCP_URL", "http://trading-mcp:8008") + "/health"),
    ("fred-mcp", os.getenv("FRED_MCP_URL", "http://fred-mcp:8009") + "/health"),
    ("spamshield-mcp", os.getenv("SPAMSHIELD_MCP_URL", "http://spamshield-mcp:8010") + "/health"),
    ("alpaca-mcp", os.getenv("ALPACA_MCP_URL", "http://alpaca-mcp:8011") + "/health"),
    # FU-006: TimesFM MCP health — prod verification pending forecast collector e2e
    ("timesfm-mcp", os.getenv("TIMESFM_MCP_URL", "http://timesfm-mcp:8012") + "/health"),
]


HEALTH_LOG_PATH: Path = Path(os.getenv("HEALTH_LOG_DIR", "logs")) / "health.jsonl"
MCP_HEALTH_TIMEOUT: float = float(os.getenv("MCP_HEALTH_TIMEOUT", "5.0"))
LANGFUSE_HOST: str = os.getenv("LANGFUSE_HOST", "http://langfuse:3000")
# Minimum TTL (seconds) before a snapshot key is considered stale.
REDIS_SNAPSHOT_STALE_THRESHOLD: int = SNAPSHOT_STALE_TTL_THRESHOLD


HEALTH_LOG_MAX_LINES: int = int(os.getenv("HEALTH_LOG_MAX_LINES", "2000"))
_ET = ZoneInfo("America/New_York")


def _snapshot_healthcheck_active(now: datetime | None = None) -> bool:
    """Return True when snapshot freshness should be enforced.

    Snapshot collectors only run during regular market hours. A small grace
    window after the opening bell avoids false alarms while the 09:30 ET run
    is still building Redis keys.

    Args:
        now: Datetime to check. Defaults to current ET time.
    """
    if now is None:
        now = datetime.now(tz=_ET)
    now_et = now.astimezone(_ET)
    if now_et.weekday() >= 5:
        return False
    if is_holiday(now_et.date()):
        return False

    open_with_grace = dt_time(9, 32)
    close_time = dt_time(13, 0) if is_early_close(now_et.date()) else dt_time(16, 0)
    return open_with_grace <= now_et.time() <= close_time


def _append_jsonl(record: dict) -> None:
    """Append a single JSON record to the structured health log.

    Creates the log directory if it doesn't exist. Rotates the log
    by keeping only the most recent HEALTH_LOG_MAX_LINES entries when
    the file exceeds the limit. Fails silently so logging never
    breaks the healthcheck itself.

    Args:
        record: Dict to serialize as one JSONL line.
    """
    try:
        HEALTH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HEALTH_LOG_PATH.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        # Rotate: keep only the tail when the file grows too large
        try:
            lines = HEALTH_LOG_PATH.read_text().splitlines()
            if len(lines) > HEALTH_LOG_MAX_LINES:
                keep = lines[-HEALTH_LOG_MAX_LINES:]
                HEALTH_LOG_PATH.write_text("\n".join(keep) + "\n")
        except OSError:
            pass
    except OSError as exc:
        logger.warning("Failed to write health.jsonl — %s", exc)


def check_redis() -> bool:
    """Ping Redis and return reachability status.

    Returns:
        True if Redis responds to PING, False otherwise.
    """
    try:
        r = _get_redis()
        r.ping()
        logger.info("Healthcheck: Redis OK")
        return True
    except redis.RedisError as exc:
        logger.error("Healthcheck: Redis FAILED — %s", exc)
        return False


def check_redis_snapshot_staleness() -> str | None:
    """Check TTL of snapshot queue keys for data staleness.

    Scans ``snapshots:*`` keys and inspects their TTL values.
    Redis TTLs count *down* from the initial EXPIRE value, so a low
    TTL means the key was set long ago and is about to expire.
    If every key's TTL is below ``REDIS_SNAPSHOT_STALE_THRESHOLD``
    (default 100 s), the snapshots are likely stale — collectors may
    have stopped producing fresh data.

    Returns:
        Warning string if all snapshot keys are stale, else None.
    """
    if not _snapshot_healthcheck_active():
        return None

    try:
        r = _get_redis()
        keys = cast(list[Any], r.keys(f"{SNAPSHOT_KEY_PREFIX}*"))
        if not keys:
            return (
                "No snapshot keys found in Redis — collectors may have stopped "
                "producing data. Check collector health and cron schedule."
            )
        ttls = [cast(int, r.ttl(k)) for k in keys]
        # TTL returns -1 for no-expiry, -2 for missing key
        valid_ttls = [t for t in ttls if t > 0]
        if not valid_ttls:
            return None
        if all(t < REDIS_SNAPSHOT_STALE_THRESHOLD for t in valid_ttls):
            return (
                f"Snapshot data may be stale — all {len(valid_ttls)} keys "
                f"have TTL < {REDIS_SNAPSHOT_STALE_THRESHOLD}s "
                f"(min={min(valid_ttls)}s, max={max(valid_ttls)}s)"
            )
        return None
    except redis.RedisError as exc:
        logger.warning("Healthcheck: Redis staleness check failed — %s", exc)
        return None


def check_postgres() -> bool:
    """Connect to Postgres and run SELECT 1.

    Returns:
        True if the query succeeds, False otherwise.
    """
    if not DATABASE_URL:
        logger.error("Healthcheck: DATABASE_URL not set")
        return False
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=30)
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


_WATCHDOG_ZERO_ALERT_THRESHOLD: int = int(os.getenv("WATCHDOG_ZERO_ALERT_THRESHOLD", "4"))
_WATCHDOG_KEY_PREFIX = "watchdog:zero_alerts:"
_WATCHDOG_TTL_SECONDS = 7200  # 2h auto-expiry so stale streaks don't persist overnight


def check_recent_alerts(timeframe: str) -> bool:
    """Query Postgres for recent alert activity and run zero-alert watchdog.

    Maintains a per-timeframe consecutive-zero-alert counter in Redis.
    Fires a single ops Discord message when the counter first hits the
    threshold (default 4 cycles = 1 hour for 15m) and resets on any alert.

    This is a soft check — returns True even when no alerts exist
    (silence is valid outside market hours). Returns False only on
    database errors.

    Args:
        timeframe: Pipeline timeframe label for logging context.

    Returns:
        True if the query succeeds (regardless of row count),
        False on database error.
    """
    try:
        from db import get_recent_alerts

        alerts = get_recent_alerts(limit=1)
        recent_count = len(alerts)
        logger.info("Healthcheck: %d recent alerts found", recent_count)

        # Zero-alert watchdog — only fires during active market hours
        if _snapshot_healthcheck_active():
            try:
                r = _get_redis()
                watchdog_key = f"{_WATCHDOG_KEY_PREFIX}{timeframe}"
                if recent_count == 0:
                    consecutive = r.incr(watchdog_key)
                    r.expire(watchdog_key, _WATCHDOG_TTL_SECONDS)
                    logger.info("Watchdog: %s zero-alert streak=%d", timeframe, consecutive)
                    # Fire exactly once when threshold is first crossed
                    if consecutive == _WATCHDOG_ZERO_ALERT_THRESHOLD:
                        today = datetime.now(tz=_ET).date().isoformat()
                        session_key = f"session:stats:{today}:{timeframe}"
                        session = cast(dict[Any, Any], r.hgetall(session_key) or {})
                        gate_parts = [
                            f"{k.decode().replace('gate_dir_', '')}: {v.decode()}"
                            for k, v in session.items()
                            if k.decode().startswith("gate_dir_")
                        ]
                        gate_parts.sort(key=lambda x: int(x.split(": ")[1]), reverse=True)
                        top_gates = ", ".join(gate_parts[:3]) if gate_parts else "no data"
                        candidates = session.get(b"llm_candidates", b"0").decode()
                        last_alert_str = (
                            str(alerts[0].get("created_at", "unknown"))[:16]
                            if alerts
                            else "no alerts on record"
                        )
                        msg = (
                            f"\u26a0\ufe0f **Zero-alert drought [{timeframe}]** \u2014 "
                            f"{consecutive} consecutive runs ({consecutive * 15}m) with no alerts fired.\n"
                            f"Candidates seen this run: {candidates}\n"
                            f"Top gate rejections today: {top_gates}\n"
                            f"Last alert: {last_alert_str}"
                        )
                        send_ops_message(msg)
                else:
                    # Reset streak counter whenever an alert exists
                    r.delete(watchdog_key)
            except Exception as exc:  # noqa: BLE001 — watchdog must never break the healthcheck
                logger.warning("Watchdog check failed (non-critical): %s", exc)

        return True
    except Exception as exc:  # noqa: BLE001 — health probe must never raise; report degraded state instead
        logger.error("Healthcheck: recent alerts query failed — %s", exc)
        return False


def check_mcps(timeout: float | None = None) -> tuple[list[str], list[str]]:
    """Hit /health on every MCP service defined in SSOT §3.

    Args:
        timeout: HTTP request timeout in seconds per service.
            Defaults to MCP_HEALTH_TIMEOUT env var (5.0).

    Returns:
        Tuple of (healthy_names, unhealthy_names).
    """
    if timeout is None:
        timeout = MCP_HEALTH_TIMEOUT
    max_retries = int(os.getenv("MCP_HEALTH_RETRIES", "2"))
    healthy: list[str] = []
    unhealthy: list[str] = []
    for name, url in MCP_SERVICES:
        ok = False
        for attempt in range(1, max_retries + 1):
            try:
                resp = httpx.get(url, timeout=timeout)
                if resp.status_code == 200:
                    healthy.append(name)
                    logger.info("Healthcheck: MCP %s OK", name)
                    ok = True
                    break
                logger.warning(
                    "Healthcheck: MCP %s returned %d (attempt %d/%d)",
                    name,
                    resp.status_code,
                    attempt,
                    max_retries,
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "Healthcheck: MCP %s unreachable (attempt %d/%d) — %s", name, attempt, max_retries, exc
                )
            if attempt < max_retries:
                time.sleep(1.0)
        if not ok:
            unhealthy.append(name)
    return healthy, unhealthy


def check_langfuse() -> str:
    """Check Langfuse observability service connectivity.

    Sends HTTP GET to ``LANGFUSE_HOST/api/public/health``.
    Then performs a lightweight authenticated SDK call to verify runtime keys
    are accepted by Langfuse.

    Returns ``"OK"`` on successful transport + auth checks, ``"DEGRADED"``
    on any failure.
    Langfuse is non-critical — a failure should never block alerts.

    Returns:
        ``"OK"`` or ``"DEGRADED"``.
    """
    url = f"{LANGFUSE_HOST.rstrip('/')}/api/public/health"
    try:
        resp = httpx.get(url, timeout=5.0)
        if resp.status_code == 200:
            logger.info("Healthcheck: Langfuse transport OK")
        else:
            logger.warning("Healthcheck: Langfuse returned %d", resp.status_code)
            return "DEGRADED"
    except httpx.HTTPError as exc:
        logger.warning("Healthcheck: Langfuse unreachable — %s", exc)
        return "DEGRADED"

    # Runtime auth probe: catches key drift where /health is green but SDK calls fail with 401.
    lf = get_langfuse_client()
    if lf is None:
        logger.warning("Healthcheck: Langfuse auth unavailable (client disabled)")
        return "DEGRADED"

    try:
        lf.fetch_traces(session_id="orchestrator-15m", limit=1, order_by="timestamp.DESC")
        logger.info("Healthcheck: Langfuse auth OK")
        return "OK"
    except Exception as exc:  # noqa: BLE001
        register_langfuse_failure(exc)
        logger.warning("Healthcheck: Langfuse auth failed — %s", exc)
        return "DEGRADED"


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
        snapshot_stale_warning = check_redis_snapshot_staleness()
        pg_ok = check_postgres()
        healthy_mcps, unhealthy_mcps = check_mcps()
        langfuse_status = check_langfuse()
        check_recent_alerts(timeframe)

        redis_circuit_degraded = False
        try:
            from validate_and_filter import is_redis_circuit_open

            redis_circuit_degraded = is_redis_circuit_open()
        except ImportError:
            pass

        # SSOT §13: structured JSONL log entry
        _append_jsonl(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "timeframe": timeframe,
                "redis_ok": redis_ok,
                "redis_circuit_open": redis_circuit_degraded,
                "snapshot_stale_warning": snapshot_stale_warning,
                "pg_ok": pg_ok,
                "mcp_healthy": healthy_mcps,
                "mcp_unhealthy": unhealthy_mcps,
                "langfuse": langfuse_status,
            }
        )

        redis_icon = "✅" if redis_ok else "❌"
        pg_icon = "✅" if pg_ok else "❌"
        mcp_icon = "✅" if not unhealthy_mcps else "❌"

        failures: list[str] = []
        if not redis_ok:
            failures.append(f"Redis={redis_icon}")
        if snapshot_stale_warning:
            failures.append(f"⏳ {snapshot_stale_warning}")
        if not pg_ok:
            failures.append(f"Postgres={pg_icon}")
        if unhealthy_mcps:
            failures.append(f"MCPs={mcp_icon} ({', '.join(unhealthy_mcps)})")
        if langfuse_status == "DEGRADED":
            failures.append("Langfuse=⚠️ DEGRADED")
        if redis_circuit_degraded:
            failures.append("Redis circuit=⚠️ DEGRADED (WATCH decay disabled)")

        if failures:
            msg = f"⚠️ Healthcheck FAILED [{timeframe}]: {' | '.join(failures)}"
            logger.warning(msg)
            send_ops_message(msg)
        else:
            logger.info("Healthcheck OK [%s]", timeframe)
    except Exception as exc:  # noqa: BLE001 — top-level healthcheck must swallow all; log and continue
        logger.error("Healthcheck unexpected error: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_healthcheck("manual")
    print("Healthcheck complete ✅")
