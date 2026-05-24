"""Postgres query helpers for the outcome tracker (SSOT §12).

Wraps ``db.py`` primitives with outcome-tracker-specific row mapping,
stale-alert expiry, and daily expiry-rate analytics.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta

from db import get_open_alerts, update_outcome

logger = logging.getLogger(__name__)

OUTCOME_WINDOW_HOURS: int = int(os.getenv("OUTCOME_WINDOW_HOURS", "4"))
OUTCOME_OPEN_ALERT_LIMIT: int = int(os.getenv("OUTCOME_OPEN_ALERT_LIMIT", "200"))
STALE_ALERT_DAYS: int = int(os.getenv("STALE_ALERT_DAYS", "7"))

_TIMEFRAME_EXPIRY_HOURS: dict[str, int] = {
    "5m": 1,
    "15m": 2,
    "1h": 6,
    "4h": 16,
    "1D": 48,
}


def map_db_row(row: dict) -> dict | None:
    """Transform a raw Postgres alert row into flat format for evaluation.

    Args:
        row: Dict from ``get_open_alerts()`` (JSONB ``entry`` column).

    Returns:
        Flat dict with ``entry_level``, ``stop_level``, ``target_level``,
        ``fired_at``, plus passthrough of other keys.  Returns ``None``
        if required price data is missing or non-positive.
    """
    entry = row.get("entry", {})
    if isinstance(entry, str):
        try:
            entry = json.loads(entry)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.error("Malformed entry JSON for alert %s: %s", row.get("id"), exc)
            return None

    for key in ("level", "stop", "target"):
        if key not in entry:
            logger.error(
                "Missing '%s' key in entry for alert %s — skipping",
                key,
                row.get("id"),
            )
            return None

    entry_level = float(entry.get("level", 0))
    stop_level = float(entry.get("stop", 0))
    target_level = float(entry.get("target", 0))

    if entry_level <= 0 or stop_level <= 0 or target_level <= 0:
        logger.error(
            "Non-positive price for alert %s: level=%.4f stop=%.4f target=%.4f — skipping",
            row.get("id"),
            entry_level,
            stop_level,
            target_level,
        )
        return None

    return {
        **row,
        "entry_level": entry_level,
        "stop_level": stop_level,
        "target_level": target_level,
        "fired_at": row.get("created_at"),
    }


def evaluate_outcome(
    alert_row: dict,
    current_price: float,
    *,
    timeframe: str | None = None,
) -> str | None:
    """Determine outcome for an alert given the current market price.

    Args:
        alert_row: Dict with keys ``direction``, ``entry_level``,
            ``stop_level``, ``target_level``, ``fired_at`` (datetime).
        current_price: Latest market price for the symbol.
        timeframe: Alert timeframe for per-timeframe expiry windows.
            Falls back to ``OUTCOME_WINDOW_HOURS`` env var if not given.

    Returns:
        ``"WIN"``, ``"LOSS"``, ``"EXPIRED"``, or ``None`` (still open).
    """
    try:
        direction: str = alert_row["direction"]
        stop_level: float = float(alert_row["stop_level"])
        target_level: float = float(alert_row["target_level"])
        fired_at: datetime = alert_row["fired_at"]

        if direction == "LONG":
            if current_price >= target_level:
                return "WIN"
            if current_price <= stop_level:
                return "LOSS"
        elif direction == "SHORT":
            if current_price <= target_level:
                return "WIN"
            if current_price >= stop_level:
                return "LOSS"
        else:
            logger.warning("Unknown direction '%s' — cannot evaluate outcome", direction)
            return None

        expiry_hours = _TIMEFRAME_EXPIRY_HOURS.get(timeframe or "", OUTCOME_WINDOW_HOURS)
        now = datetime.now(UTC)
        if isinstance(fired_at, datetime):
            deadline = fired_at + timedelta(hours=expiry_hours)
            if now >= deadline:
                return "EXPIRED"

        return None
    except (KeyError, TypeError, ValueError) as exc:
        logger.error("evaluate_outcome error: %s", exc)
        return None


def fetch_open_alerts(limit: int | None = None) -> list[dict]:
    """Return unresolved alerts (outcome IS NULL), newest first."""
    return get_open_alerts(limit=limit or OUTCOME_OPEN_ALERT_LIMIT)


def write_outcome(
    alert_id: int,
    outcome: str,
    pnl: float,
    *,
    pnl_pct: float | None = None,
) -> None:
    """Persist a resolved outcome to Postgres."""
    update_outcome(alert_id, outcome, pnl, pnl_pct=pnl_pct)


def expire_stale_alerts(*, stale_days: int | None = None, limit: int | None = None) -> int:
    """Auto-expire open alerts older than ``stale_days``.

    Returns:
        Number of alerts expired this cycle.
    """
    days = stale_days if stale_days is not None else STALE_ALERT_DAYS
    expired = 0
    try:
        rows = fetch_open_alerts(limit=limit)
        now = datetime.now(UTC)
        for row in rows:
            created = row.get("created_at")
            if isinstance(created, datetime) and (now - created).days >= days:
                write_outcome(row["id"], "EXPIRED", 0.0, pnl_pct=0.0)
                logger.info(
                    "Expired stale alert %s (id=%s, age=%dd)",
                    row.get("symbol"),
                    row["id"],
                    (now - created).days,
                )
                expired += 1
    except Exception as exc:
        logger.error("Stale alert cleanup failed: %s", exc)
    return expired


def fetch_daily_expiry_stats() -> dict[str, int] | None:
    """Return EXPIRED and total resolved alert counts for the last 24 hours."""
    from psycopg2.extras import RealDictCursor

    from db import _put_conn, get_conn

    sql = """
        SELECT
            COALESCE(SUM(CASE WHEN outcome = 'EXPIRED' THEN 1 ELSE 0 END), 0) AS expired,
            COALESCE(SUM(CASE WHEN outcome IN ('WIN','LOSS','EXPIRED') THEN 1 ELSE 0 END), 0) AS total
        FROM alerts
        WHERE outcome IS NOT NULL
          AND updated_at >= NOW() - INTERVAL '24 hours'
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql)
            row = cur.fetchone()
    finally:
        _put_conn(conn)

    if not row:
        return None
    return {"expired": int(row["expired"]), "total": int(row["total"])}
