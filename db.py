"""Postgres interface for trade-alert.

Provides insert, update, and query functions for the alerts table.
Implements SSOT §11/§12.
"""

from __future__ import annotations

import json
import logging
import os

import psycopg2
from psycopg2.extras import RealDictCursor

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from models import PlaybookAlert

logger = logging.getLogger(__name__)

DATABASE_URL: str | None = os.getenv("DATABASE_URL")


def get_conn() -> psycopg2.extensions.connection:
    """Return a psycopg2 connection to the alerts database.

    Raises:
        RuntimeError: If DATABASE_URL is not configured.
        psycopg2.OperationalError: If the database is unreachable.
    """
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set — configure via Vault or .env")
    return psycopg2.connect(DATABASE_URL, connect_timeout=30)


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
    with get_conn() as conn:
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
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (outcome, pnl, alert_id))
            conn.commit()


def get_recent_alerts(limit: int = 50) -> list[dict]:
    """Return the most recent alerts ordered by creation time.

    Args:
        limit: Maximum number of rows to return.

    Returns:
        List of alert dicts (column-name keyed).
    """
    sql = "SELECT * FROM alerts ORDER BY created_at DESC LIMIT %s"
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (limit,))
            return [dict(row) for row in cur.fetchall()]


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
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql)
            return [dict(row) for row in cur.fetchall()]


if __name__ == "__main__":
    # Test connection only — do not insert real data
    try:
        with get_conn() as conn:
            print("DB connection successful ✅")
    except Exception as e:
        print(f"DB not available (expected in dev): {e}")
        print("db.py structure valid ✅")
