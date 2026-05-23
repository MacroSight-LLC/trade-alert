"""Postgres alert logging helpers for trade-alert notifier pipeline."""

from __future__ import annotations

import logging
import uuid

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from db import insert_alert
from log_config import configure_logging
from metrics import DB_INSERTS
from models import PlaybookAlert

configure_logging()
logger = logging.getLogger(__name__)


def extract_forecast_scores(snapshots: list[dict]) -> dict[str, float]:
    """Build per-symbol forecast scores from raw snapshot signal lists."""
    forecast_scores: dict[str, float] = {}
    for snap in snapshots:
        sym = snap.get("symbol", "")
        for sig in snap.get("signals", []):
            if sig.get("type") == "price_forecast":
                try:
                    val = float(sig.get("score", 0))
                    if sym not in forecast_scores or abs(val) > abs(forecast_scores[sym]):
                        forecast_scores[sym] = val
                except (TypeError, ValueError):
                    pass
    return forecast_scores


def get_similar_alert_stats(symbol: str, direction: str, ep: float) -> str:
    """Query Postgres for historical win-rate of similar alerts."""
    try:
        from db import _put_conn, get_conn

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT outcome, COUNT(*) as cnt
                    FROM alerts
                    WHERE symbol = %s
                      AND direction = %s
                      AND edge_probability BETWEEN %s AND %s
                      AND outcome IN ('WIN', 'LOSS')
                      AND created_at > NOW() - INTERVAL '30 days'
                    GROUP BY outcome
                    """,
                    (symbol, direction, ep - 0.05, ep + 0.05),
                )
                rows = cur.fetchall()
        finally:
            _put_conn(conn)

        if not rows:
            return "\U0001f4ca First alert for this setup"

        wins = sum(r[1] for r in rows if r[0] == "WIN")
        total = sum(r[1] for r in rows)
        if total < 2:
            return "\U0001f4ca First alert for this setup"
        pct = int(wins / total * 100)
        return f"\U0001f4ca Similar past alerts: {pct}% win rate (N={total})"
    except Exception:
        return ""


def batch_similar_alert_stats(alerts: list[PlaybookAlert]) -> dict[str, str]:
    """Batch-fetch historical win-rate stats for multiple alerts in one query."""
    if not alerts:
        return {}
    try:
        from db import _put_conn, get_conn

        lookups: list[tuple[str, str, float, float]] = []
        for a in alerts:
            lookups.append((a.symbol, a.direction, a.edge_probability - 0.05, a.edge_probability + 0.05))

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT a.symbol, a.direction, a.outcome, COUNT(*) as cnt
                    FROM alerts a
                    INNER JOIN (
                        SELECT unnest(%s::text[]) AS symbol,
                               unnest(%s::text[]) AS direction,
                               unnest(%s::float8[]) AS ep_low,
                               unnest(%s::float8[]) AS ep_high
                    ) lookup ON a.symbol = lookup.symbol
                            AND a.direction = lookup.direction
                            AND a.edge_probability BETWEEN lookup.ep_low AND lookup.ep_high
                    WHERE a.outcome IN ('WIN', 'LOSS')
                      AND a.created_at > NOW() - INTERVAL '30 days'
                    GROUP BY a.symbol, a.direction, a.outcome
                    """,
                    (
                        [t[0] for t in lookups],
                        [t[1] for t in lookups],
                        [t[2] for t in lookups],
                        [t[3] for t in lookups],
                    ),
                )
                rows = cur.fetchall()
        finally:
            _put_conn(conn)

        agg: dict[tuple[str, str], dict[str, int]] = {}
        for sym, direction, outcome, cnt in rows:
            key = (sym, direction)
            agg.setdefault(key, {})
            agg[key][outcome] = agg[key].get(outcome, 0) + cnt

        result: dict[str, str] = {}
        for a in alerts:
            k = (a.symbol, a.direction)
            stats = agg.get(k, {})
            wins = stats.get("WIN", 0)
            total = sum(stats.values())
            lookup_key = f"{a.symbol}:{a.direction}"
            if total < 2:
                result[lookup_key] = "\U0001f4ca First alert for this setup"
            else:
                pct = int(wins / total * 100)
                result[lookup_key] = f"\U0001f4ca Similar past alerts: {pct}% win rate (N={total})"
        return result
    except Exception:
        return {}


def persist_alert(
    alert: PlaybookAlert,
    snapshots: list[dict],
    forecast_scores: dict[str, float],
    *,
    trace_id: str | None = None,
) -> tuple[int, str] | None:
    """Insert alert into Postgres; return (alert_id, idempotency_key) or None on failure."""
    try:
        fc = forecast_scores.get(alert.symbol)
        idempotency_key = str(uuid.uuid4())
        alert_id = insert_alert(
            alert,
            snapshots,
            forecast_score=fc,
            forecast_contradicted=False,
            trace_id=trace_id or None,
            idempotency_key=idempotency_key,
        )
        DB_INSERTS.labels(status="success").inc()
        return alert_id, idempotency_key
    except Exception as exc:
        logger.error(
            "Postgres insert failed for %s — skipping Discord send: %s",
            alert.symbol,
            exc,
        )
        DB_INSERTS.labels(status="failure").inc()
        return None
