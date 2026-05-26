"""Postgres queries for legacy_alerts (trial/dev catalog, separate from prod alerts)."""

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg2.extras import RealDictCursor

from db import get_conn, _put_conn

logger = logging.getLogger(__name__)


def get_legacy_summary_stats() -> dict[str, Any]:
    """Aggregate KPIs for cherry-picked legacy_alerts only."""
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
            MIN(created_at) AS earliest,
            MAX(created_at) AS latest
        FROM legacy_alerts
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql)
            return dict(cur.fetchone() or {})
    finally:
        _put_conn(conn)


def get_legacy_recent_alerts(limit: int = 50) -> list[dict[str, Any]]:
    """Return legacy alerts newest first."""
    sql = """
        SELECT *
        FROM legacy_alerts
        ORDER BY created_at DESC
        LIMIT %s
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (limit,))
            return [dict(row) for row in cur.fetchall()]
    finally:
        _put_conn(conn)


def insert_legacy_alert(row: dict[str, Any]) -> int:
    """Insert one curated legacy row; returns new legacy_alerts.id."""
    sql = """
        INSERT INTO legacy_alerts (
            symbol, direction, edge_probability, confidence, timeframe,
            thesis, entry, timeframe_rationale, sentiment_context,
            unusual_activity, macro_regime, sources_agree, raw_snapshots,
            created_at, updated_at, outcome, outcome_pnl, outcome_pnl_pct,
            forecast_score, forecast_contradicted, langfuse_trace_id,
            source_alert_id, legacy_note, source_dump
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s
        )
        ON CONFLICT (source_alert_id, source_dump) DO NOTHING
        RETURNING id
    """
    entry = row.get("entry")
    if isinstance(entry, dict):
        entry = json.dumps(entry)
    unusual = row.get("unusual_activity")
    if unusual is None:
        unusual = json.dumps([])
    elif isinstance(unusual, (list, dict)):
        unusual = json.dumps(unusual)
    snapshots = row.get("raw_snapshots")
    if snapshots is None:
        snapshots = json.dumps([])
    elif isinstance(snapshots, (list, dict)):
        snapshots = json.dumps(snapshots)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    row["symbol"],
                    row["direction"],
                    row["edge_probability"],
                    row["confidence"],
                    row["timeframe"],
                    row["thesis"],
                    entry,
                    row.get("timeframe_rationale"),
                    row.get("sentiment_context"),
                    unusual,
                    row.get("macro_regime"),
                    row.get("sources_agree"),
                    snapshots,
                    row.get("created_at"),
                    row.get("updated_at") or row.get("created_at"),
                    row.get("outcome"),
                    row.get("outcome_pnl"),
                    row.get("outcome_pnl_pct"),
                    row.get("forecast_score"),
                    row.get("forecast_contradicted", False),
                    row.get("langfuse_trace_id"),
                    row["source_alert_id"],
                    row.get("legacy_note"),
                    row.get("source_dump"),
                ),
            )
            result = cur.fetchone()
            conn.commit()
            return int(result[0]) if result else 0
    finally:
        _put_conn(conn)


def legacy_alert_count() -> int:
    """Return number of rows in legacy_alerts."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM legacy_alerts")
            row = cur.fetchone()
            return int(row[0]) if row else 0
    finally:
        _put_conn(conn)
