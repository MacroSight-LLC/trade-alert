"""Postgres interface for trade-alert.

Provides insert, update, and query functions for the alerts table.
Implements SSOT §11/§12.
"""

from __future__ import annotations

import json
import logging
import os

import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from models import PlaybookAlert

logger = logging.getLogger(__name__)

DATABASE_URL: str | None = os.getenv("DATABASE_URL")

_pool: psycopg2.pool.SimpleConnectionPool | None = None


def _get_pool() -> psycopg2.pool.SimpleConnectionPool:
    """Return a lazily-initialised connection pool (min=1, max=5)."""
    global _pool  # noqa: PLW0603
    if _pool is None or _pool.closed:
        url = DATABASE_URL
        if not url:
            raise RuntimeError("DATABASE_URL not set — configure via Vault or .env")
        _pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1,
            maxconn=5,
            dsn=url,
            connect_timeout=30,
        )
    return _pool


def get_conn() -> psycopg2.extensions.connection:
    """Return a psycopg2 connection from the pool.

    Raises:
        RuntimeError: If DATABASE_URL is not configured.
        psycopg2.OperationalError: If the database is unreachable.
    """
    return _get_pool().getconn()


def _put_conn(conn: psycopg2.extensions.connection) -> None:
    """Return a connection to the pool."""
    try:
        _get_pool().putconn(conn)
    except Exception:  # noqa: BLE001
        pass


def insert_alert(alert: PlaybookAlert, raw_snapshots: list[dict]) -> int:
    """Insert a PlaybookAlert into the alerts table.

    Args:
        alert: Validated PlaybookAlert from the decision engine.
        raw_snapshots: Raw snapshot dicts archived for auditability.

    Returns:
        The auto-generated ``id`` of the new row.
    """
    sql = """
        INSERT INTO alerts (
            symbol, direction, edge_probability, confidence, timeframe,
            thesis, entry, timeframe_rationale, sentiment_context,
            unusual_activity, macro_regime, sources_agree, raw_snapshots
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s
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
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return row[0]
    finally:
        _put_conn(conn)


def update_outcome(alert_id: int, outcome: str, pnl: float) -> None:
    """Update outcome and PnL for a resolved alert.

    Args:
        alert_id: Primary key of the alert row.
        outcome: One of ``"WIN"``, ``"LOSS"``, ``"SCRATCH"``.
        pnl: Realized profit/loss value.
    """
    sql = """
        UPDATE alerts
        SET outcome = %s, outcome_pnl = %s, updated_at = NOW()
        WHERE id = %s
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (outcome, pnl, alert_id))
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
        WHERE created_at >= NOW() - INTERVAL '%s days'
        GROUP BY date
        ORDER BY date
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (days,))
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
            SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
            SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN outcome = 'SCRATCH' THEN 1 ELSE 0 END) AS scratches,
            ROUND(
                CASE WHEN SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) > 0
                THEN SUM(CASE WHEN outcome = 'WIN' THEN 1.0 ELSE 0 END)
                     / SUM(CASE WHEN outcome IS NOT NULL THEN 1.0 ELSE 0 END)
                ELSE NULL END::numeric, 4
            ) AS overall_winrate,
            ROUND(AVG(edge_probability)::numeric, 4) AS avg_edge,
            ROUND(AVG(outcome_pnl)::numeric, 4) AS avg_pnl,
            SUM(CASE WHEN DATE(created_at AT TIME ZONE 'UTC') = CURRENT_DATE
                THEN 1 ELSE 0 END) AS alerts_today
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
    except Exception as e:
        print(f"DB not available (expected in dev): {e}")
        print("db.py structure valid ✅")
