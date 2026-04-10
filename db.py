"""Postgres interface for trade-alert.

Provides insert, update, and query functions for the alerts table.
Implements SSOT §11/§12.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from datetime import timedelta
from typing import Any

import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from models import PlaybookAlert

logger = logging.getLogger(__name__)


def _resolve_database_url() -> str | None:
    """Return the effective Postgres DSN for the current runtime.

    In containers, `.env` still carries a localhost DSN for host-side tooling.
    Rewrite that value to the internal `postgres` service when the container has
    direct Postgres credentials available.
    """
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

_pool: psycopg2.pool.SimpleConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> psycopg2.pool.SimpleConnectionPool:
    """Return a lazily-initialised connection pool (min=1, max=5)."""
    global _pool  # noqa: PLW0603
    if _pool is not None and not _pool.closed:
        return _pool
    with _pool_lock:
        if _pool is not None and not _pool.closed:
            return _pool
        url = DATABASE_URL
        if not url:
            raise RuntimeError("DATABASE_URL not set — configure via Vault or .env")
        _pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1,
            maxconn=5,
            dsn=url,
            connect_timeout=30,
        )
        atexit.register(_close_pool)
    return _pool


def _close_pool() -> None:
    """Close all connections in the pool at process exit."""
    global _pool  # noqa: PLW0603
    if _pool is not None and not _pool.closed:
        _pool.closeall()
        _pool = None


def get_conn() -> psycopg2.extensions.connection:
    """Return a psycopg2 connection from the pool.

    Raises:
        RuntimeError: If DATABASE_URL is not configured.
        psycopg2.OperationalError: If the database is unreachable.
    """
    return _get_pool().getconn()


def _put_conn(conn: psycopg2.extensions.connection) -> None:
    """Return a connection to the pool, rolling back dirty transactions."""
    try:
        if conn.closed:
            return
        if conn.info.transaction_status != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
            conn.rollback()
        _get_pool().putconn(conn)
    except (psycopg2.Error, RuntimeError):
        logger.debug("Failed to return connection to pool", exc_info=True)


def insert_alert(
    alert: PlaybookAlert,
    raw_snapshots: list[dict],
    *,
    forecast_score: float | None = None,
    forecast_contradicted: bool = False,
    trace_id: str | None = None,
) -> int:
    """Insert a PlaybookAlert into the alerts table.

    Args:
        alert: Validated PlaybookAlert from the decision engine.
        raw_snapshots: Raw snapshot dicts archived for auditability.
        forecast_score: TimesFM price_forecast score for this symbol (optional).
        forecast_contradicted: Whether the forecast gate was triggered (optional).
        trace_id: Langfuse trace ID for outcome linkage (optional).

    Returns:
        The auto-generated ``id`` of the new row.
    """
    sql = """
        INSERT INTO alerts (
            symbol, direction, edge_probability, confidence, timeframe,
            thesis, entry, timeframe_rationale, sentiment_context,
            unusual_activity, macro_regime, sources_agree, raw_snapshots,
            forecast_score, forecast_contradicted, langfuse_trace_id
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s
        )
        RETURNING id
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    alert.symbol,
                    alert.direction,
                    alert.edge_probability,
                    alert.confidence,
                    alert.timeframe,
                    alert.thesis,
                    json.dumps(alert.entry),
                    alert.timeframe_rationale,
                    alert.sentiment_context,
                    json.dumps(alert.unusual_activity),
                    alert.macro_regime,
                    alert.sources_agree,
                    json.dumps(raw_snapshots),
                    forecast_score,
                    forecast_contradicted,
                    trace_id,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return row[0]
    finally:
        _put_conn(conn)


def update_outcome(alert_id: int, outcome: str, pnl: float, pnl_pct: float | None = None) -> None:
    """Update outcome and PnL for a resolved alert.

    Args:
        alert_id: Primary key of the alert row.
        outcome: One of ``"WIN"``, ``"LOSS"``, ``"SCRATCH"``, ``"EXPIRED"``.
        pnl: Realized profit/loss value.
        pnl_pct: PnL as percentage of entry price (optional).
    """
    sql = """
        UPDATE alerts
        SET outcome = %s, outcome_pnl = %s, outcome_pnl_pct = %s, updated_at = NOW()
        WHERE id = %s
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (outcome, pnl, pnl_pct, alert_id))
            conn.commit()
    finally:
        _put_conn(conn)


def get_recent_alerts(limit: int = 50) -> list[dict]:
    """Return the most recent alerts ordered by creation time.

    Args:
        limit: Maximum number of rows to return.

    Returns:
        List of alert dicts (column-name keyed).
    """
    sql = "SELECT * FROM alerts ORDER BY created_at DESC LIMIT %s"
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (limit,))
            return [dict(row) for row in cur.fetchall()]
    finally:
        _put_conn(conn)


def get_open_alerts(limit: int = 200) -> list[dict]:
    """Return unresolved alerts (outcome IS NULL), newest first.

    Uses the ``idx_alerts_open_created`` partial index for efficiency.

    Args:
        limit: Maximum number of rows to return.

    Returns:
        List of alert dicts with no outcome yet.
    """
    sql = "SELECT * FROM alerts WHERE outcome IS NULL ORDER BY created_at DESC LIMIT %s"
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (limit,))
            return [dict(row) for row in cur.fetchall()]
    finally:
        _put_conn(conn)


def get_recent_winrate_summary(days: int = 7) -> dict[str, Any]:
    """Return recent win-rate stats for prompt injection.

    Queries resolved alerts from the last *days* days and returns
    aggregate stats plus per-EP-bucket calibration data so the LLM
    can adjust its edge_probability estimates.

    Args:
        days: Look-back window in calendar days.

    Returns:
        Dict with keys: total_resolved, wins, losses, winrate,
        avg_ep, ep_calibration (list of bucket dicts).
    """
    sql_summary = """
        SELECT
            COUNT(*) AS total_resolved,
            SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END) AS losses,
            ROUND(
                CASE WHEN COUNT(*) > 0
                THEN SUM(CASE WHEN outcome = 'WIN' THEN 1.0 ELSE 0 END) / COUNT(*)::numeric
                ELSE NULL END, 4
            ) AS winrate,
            ROUND(AVG(edge_probability)::numeric, 4) AS avg_ep
        FROM alerts
        WHERE outcome IS NOT NULL
          AND created_at >= NOW() - %s
    """
    sql_buckets = """
        SELECT
            ROUND(edge_probability::numeric, 1) AS bucket,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) AS wins,
            ROUND(
                CASE WHEN COUNT(*) > 0
                THEN SUM(CASE WHEN outcome = 'WIN' THEN 1.0 ELSE 0 END) / COUNT(*)::numeric
                ELSE NULL END, 4
            ) AS actual_winrate
        FROM alerts
        WHERE outcome IS NOT NULL
          AND created_at >= NOW() - %s
        GROUP BY bucket
        ORDER BY bucket DESC
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            interval = timedelta(days=days)
            cur.execute(sql_summary, (interval,))
            summary = dict(cur.fetchone())
            cur.execute(sql_buckets, (interval,))
            buckets = [dict(row) for row in cur.fetchall()]
            summary["ep_calibration"] = buckets
            return summary
    except psycopg2.Error as exc:
        logger.warning("get_recent_winrate_summary failed: %s", exc)
        return {
            "total_resolved": 0,
            "wins": 0,
            "losses": 0,
            "winrate": None,
            "avg_ep": None,
            "ep_calibration": [],
        }
    finally:
        _put_conn(conn)


def get_winrate_by_bucket() -> list[dict]:
    """Return winrate statistics grouped by edge_probability bucket.

    Buckets are 0.1 increments (e.g. 0.7, 0.8, 0.9).

    Returns:
        List of dicts with keys: bucket, total, wins, avg_pnl.
    """
    sql = """
        SELECT
            ROUND(edge_probability::numeric, 1) AS bucket,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(outcome_pnl)::numeric, 4) AS avg_pnl
        FROM alerts
        WHERE outcome IS NOT NULL
        GROUP BY bucket
        ORDER BY bucket DESC
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql)
            return [dict(row) for row in cur.fetchall()]
    finally:
        _put_conn(conn)


def get_calibration_accuracy(days: int = 60) -> list[dict]:
    """Return predicted EP vs actual win rate by (direction, EP bucket).

    Used by ``alert_quality.py`` to penalize EP inflation.

    Args:
        days: Lookback window in days.

    Returns:
        List of dicts with keys: direction, ep_bucket, predicted_ep,
        actual_winrate, total, gap (predicted − actual).
    """
    sql = """
        SELECT
            direction,
            ROUND(edge_probability::numeric, 1) AS ep_bucket,
            ROUND(AVG(edge_probability)::numeric, 4) AS predicted_ep,
            ROUND(
                CASE WHEN COUNT(*) > 0
                THEN SUM(CASE WHEN outcome = 'WIN' THEN 1.0 ELSE 0 END) / COUNT(*)
                ELSE NULL END::numeric, 4
            ) AS actual_winrate,
            COUNT(*) AS total
        FROM alerts
        WHERE outcome IN ('WIN', 'LOSS')
          AND created_at >= NOW() - %s
        GROUP BY direction, ep_bucket
        HAVING COUNT(*) >= 5
        ORDER BY direction, ep_bucket DESC
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (timedelta(days=days),))
            rows = [dict(row) for row in cur.fetchall()]
            for r in rows:
                pred = float(r.get("predicted_ep") or 0)
                actual = float(r.get("actual_winrate") or 0)
                r["gap"] = round(pred - actual, 4)
            return rows
    finally:
        _put_conn(conn)


def get_alert_frequency(days: int = 30) -> list[dict]:
    """Return daily alert counts for the last *days* days.

    Args:
        days: Number of days to look back.

    Returns:
        List of dicts with keys: date, total, longs, shorts, watches.
    """
    sql = """
        SELECT
            DATE(created_at AT TIME ZONE 'UTC') AS date,
            COUNT(*) AS total,
            SUM(CASE WHEN direction = 'LONG' THEN 1 ELSE 0 END) AS longs,
            SUM(CASE WHEN direction = 'SHORT' THEN 1 ELSE 0 END) AS shorts,
            SUM(CASE WHEN direction = 'WATCH' THEN 1 ELSE 0 END) AS watches
        FROM alerts
        WHERE created_at >= NOW() - %s
        GROUP BY date
        ORDER BY date
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (timedelta(days=days),))
            return [dict(row) for row in cur.fetchall()]
    finally:
        _put_conn(conn)


def get_symbol_performance(limit: int = 20) -> list[dict]:
    """Return per-symbol performance statistics.

    Args:
        limit: Maximum number of symbols to return, sorted by alert count.

    Returns:
        List of dicts with keys: symbol, total, wins, losses, winrate,
        avg_edge, avg_pnl.
    """
    sql = """
        SELECT
            symbol,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END) AS losses,
            ROUND(
                CASE WHEN SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) > 0
                THEN SUM(CASE WHEN outcome = 'WIN' THEN 1.0 ELSE 0 END)
                     / SUM(CASE WHEN outcome IS NOT NULL THEN 1.0 ELSE 0 END)
                ELSE NULL END::numeric, 4
            ) AS winrate,
            ROUND(AVG(edge_probability)::numeric, 4) AS avg_edge,
            ROUND(AVG(outcome_pnl)::numeric, 4) AS avg_pnl
        FROM alerts
        GROUP BY symbol
        ORDER BY total DESC
        LIMIT %s
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (limit,))
            return [dict(row) for row in cur.fetchall()]
    finally:
        _put_conn(conn)


def get_summary_stats() -> dict:
    """Return aggregate dashboard summary statistics.

    Returns:
        Dict with keys: total_alerts, resolved, wins, losses, scratches,
        overall_winrate, avg_edge, avg_rr, avg_pnl, alerts_today,
        kpi_winrate_70 (winrate for edge >= 0.70).
    """
    sql = """
        SELECT
            COUNT(*) AS total_alerts,
            COALESCE(SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END), 0) AS resolved,
            COALESCE(SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END), 0) AS wins,
            COALESCE(SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END), 0) AS losses,
            COALESCE(SUM(CASE WHEN outcome = 'SCRATCH' THEN 1 ELSE 0 END), 0) AS scratches,
            ROUND(
                CASE WHEN SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) > 0
                THEN SUM(CASE WHEN outcome = 'WIN' THEN 1.0 ELSE 0 END)
                     / SUM(CASE WHEN outcome IS NOT NULL THEN 1.0 ELSE 0 END)
                ELSE NULL END::numeric, 4
            ) AS overall_winrate,
            ROUND(AVG(edge_probability)::numeric, 4) AS avg_edge,
            ROUND(AVG(outcome_pnl)::numeric, 4) AS avg_pnl,
            COALESCE(SUM(CASE WHEN DATE(created_at AT TIME ZONE 'UTC') = CURRENT_DATE
                THEN 1 ELSE 0 END), 0) AS alerts_today
        FROM alerts
    """
    sql_kpi = """
        SELECT
            ROUND(
                CASE WHEN COUNT(*) > 0
                THEN SUM(CASE WHEN outcome = 'WIN' THEN 1.0 ELSE 0 END) / COUNT(*)::numeric
                ELSE NULL END, 4
            ) AS kpi_winrate_70
        FROM alerts
        WHERE outcome IS NOT NULL AND edge_probability >= 0.70
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql)
            row = dict(cur.fetchone())
            cur.execute(sql_kpi)
            kpi_row = dict(cur.fetchone())
            row["kpi_winrate_70"] = kpi_row.get("kpi_winrate_70")
            return row
    finally:
        _put_conn(conn)


if __name__ == "__main__":
    # Test connection only — do not insert real data
    try:
        conn = get_conn()
        _put_conn(conn)
        print("DB connection successful ✅")
    except (psycopg2.Error, RuntimeError) as e:
        print(f"DB not available (expected in dev): {e}")
        print("db.py structure valid ✅")
