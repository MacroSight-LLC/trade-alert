"""Feedback loop: historical win-rate injection for prompt calibration.

Queries Postgres for resolved alert outcomes grouped by symbol, direction,
and timeframe, computes per-bucket win-rates, and formats the results
for injection into the LLM system prompt.

Implements SSOT §12 feedback loop — the decision engine should see
its own track record so it can self-calibrate edge_probability.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

MIN_WINRATE_SAMPLES: int = int(os.environ.get("MIN_WINRATE_SAMPLES", "10"))
WINRATE_MAX_BUCKETS: int = int(os.environ.get("WINRATE_MAX_BUCKETS", "20"))
WINRATE_STALENESS_DAYS: int = int(os.environ.get("WINRATE_STALENESS_DAYS", "7"))


def get_winrate_context(
    timeframe: str,
    lookback_days: int = 14,
) -> dict[str, Any]:
    """Query resolved alert outcomes per (symbol, direction, timeframe).

    Only includes outcomes with ``WIN`` or ``LOSS`` (target hit / stop hit).
    Buckets below ``MIN_WINRATE_SAMPLES`` are excluded.

    Args:
        timeframe: Pipeline timeframe filter (``"15m"`` or ``"1h"``).
        lookback_days: How many days of history to include.

    Returns:
        Dict with keys:

        - ``buckets``: ``{bucket_key: float}`` mapping of win-rates
        - ``bucket_counts``: ``{bucket_key: int}`` sample sizes (winrate_sample_count)
        - ``total_resolved``: total resolved outcomes in window
        - ``calibration_warning``: True if total < 30
    """
    result: dict[str, Any] = {
        "buckets": {},
        "bucket_counts": {},
        "total_resolved": 0,
        "calibration_warning": True,
    }

    try:
        from db import _put_conn, get_conn
    except ImportError:
        logger.warning("winrate_injector: db module unavailable — skipping")
        return result

    conn = None
    max_updated = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # psycopg2 doesn't support interval parameterisation directly,
            # so we interpolate lookback_days safely as an integer.
            cur.execute(
                """
                SELECT symbol, direction, outcome
                FROM alerts
                WHERE timeframe = %s
                  AND outcome IN ('WIN', 'LOSS')
                  AND updated_at > NOW() - make_interval(days => %s)
                """,
                (timeframe, lookback_days),
            )
            rows = cur.fetchall()
            cur.execute(
                "SELECT MAX(updated_at) FROM alerts WHERE timeframe = %s AND outcome IN ('WIN', 'LOSS')",
                (timeframe,),
            )
            max_updated = cur.fetchone()[0]
    except Exception as exc:
        logger.warning("winrate_injector: DB query failed — %s", exc)
        return result
    finally:
        if conn is not None:
            _put_conn(conn)

    if not rows:
        return result

    bucket_wins: dict[str, int] = {}
    bucket_total: dict[str, int] = {}

    for symbol, direction, outcome in rows:
        key = f"{symbol}_{direction}_{timeframe}"
        bucket_total[key] = bucket_total.get(key, 0) + 1
        if outcome == "WIN":
            bucket_wins[key] = bucket_wins.get(key, 0) + 1

    total_resolved = len(rows)

    min_sample = MIN_WINRATE_SAMPLES
    buckets: dict[str, float] = {}
    bucket_counts: dict[str, int] = {}
    ranked: list[tuple[str, int]] = []
    for key, count in bucket_total.items():
        if count >= min_sample:
            wins = bucket_wins.get(key, 0)
            buckets[key] = round(wins / count, 2) if count > 0 else 0.0
            bucket_counts[key] = count
            ranked.append((key, count))

    if len(buckets) > WINRATE_MAX_BUCKETS:
        ranked.sort(key=lambda x: x[1], reverse=True)
        keep = {k for k, _ in ranked[:WINRATE_MAX_BUCKETS]}
        buckets = {k: v for k, v in buckets.items() if k in keep}
        bucket_counts = {k: v for k, v in bucket_counts.items() if k in keep}
        logger.warning(
            "winrate_injector: truncated to %d buckets (had %d)",
            WINRATE_MAX_BUCKETS,
            len(ranked),
        )

    stale_warning = False
    if max_updated is not None:
        from datetime import UTC, datetime, timedelta

        if isinstance(max_updated, datetime):
            if max_updated.tzinfo is None:
                max_updated = max_updated.replace(tzinfo=UTC)
            stale_warning = max_updated < datetime.now(tz=UTC) - timedelta(days=WINRATE_STALENESS_DAYS)
            if stale_warning:
                logger.warning(
                    "winrate_injector: data stale (last update %s, threshold %dd)",
                    max_updated.isoformat(),
                    WINRATE_STALENESS_DAYS,
                )

    return {
        "buckets": buckets,
        "bucket_counts": bucket_counts,
        "total_resolved": total_resolved,
        "calibration_warning": total_resolved < 30,
        "stale_warning": stale_warning,
    }


def format_winrate_section(winrate_dict: dict[str, Any]) -> str:
    """Format win-rate context as a markdown section for prompt injection.

    Args:
        winrate_dict: Output of :func:`get_winrate_context`.

    Returns:
        Human-readable markdown string, or empty string if no data.
    """
    buckets = winrate_dict.get("buckets", {})
    bucket_counts = winrate_dict.get("bucket_counts", {})
    total = winrate_dict.get("total_resolved", 0)
    warning = winrate_dict.get("calibration_warning", True)

    if not buckets:
        return ""

    lines = [f"## Historical Accuracy (last 14d, min {MIN_WINRATE_SAMPLES} samples)"]
    for key in sorted(buckets.keys()):
        pct = int(buckets[key] * 100)
        n = bucket_counts.get(key, 0)
        lines.append(f"{key}: {pct}% win-rate (winrate_sample_count={n})")

    if warning:
        lines.append(
            f"⚠️ Calibration data thin (total resolved: {total} — need 30+ per bucket for reliability)"
        )

    return "\n".join(lines)
