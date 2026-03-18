"""Feedback loop: historical win-rate injection for prompt calibration.

Queries Postgres for resolved alert outcomes grouped by direction and
signal type, computes per-bucket win-rates, and formats the results
for injection into the LLM system prompt.

Implements SSOT §12 feedback loop — the decision engine should see
its own track record so it can self-calibrate edge_probability.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_winrate_context(
    timeframe: str,
    lookback_days: int = 14,
) -> dict[str, Any]:
    """Query resolved alert outcomes grouped by direction.

    Extracts distinct signal types from ``raw_snapshots`` JSONB to
    build a ``{direction}_{signal_type}`` bucket key (e.g.
    ``LONG_technical_trend``).  Only buckets with ≥ 5 resolved
    outcomes are included to avoid noisy small-sample statistics.

    Args:
        timeframe: Pipeline timeframe filter (``"15m"`` or ``"1h"``).
        lookback_days: How many days of history to include.

    Returns:
        Dict with keys:

        - ``buckets``: ``{bucket_key: float}`` mapping of win-rates
        - ``bucket_counts``: ``{bucket_key: int}`` sample sizes
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
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # psycopg2 doesn't support interval parameterisation directly,
            # so we interpolate lookback_days safely as an integer.
            cur.execute(
                """
                SELECT direction, outcome, raw_snapshots
                FROM alerts
                WHERE timeframe = %s
                  AND outcome IN ('WIN', 'LOSS')
                  AND updated_at > NOW() - make_interval(days => %s)
                """,
                (timeframe, lookback_days),
            )
            rows = cur.fetchall()
    except Exception as exc:
        logger.warning("winrate_injector: DB query failed — %s", exc)
        return result
    finally:
        if conn is not None:
            _put_conn(conn)

    if not rows:
        return result

    # Tally wins per bucket: direction + dominant signal types
    bucket_wins: dict[str, int] = {}
    bucket_total: dict[str, int] = {}

    for direction, outcome, raw_snapshots in rows:
        # Extract distinct signal types from raw_snapshots JSONB
        sig_types: set[str] = set()
        if raw_snapshots:
            snapshots = raw_snapshots if isinstance(raw_snapshots, list) else []
            for snap in snapshots:
                if isinstance(snap, dict):
                    for sig in snap.get("signals", []):
                        if isinstance(sig, dict):
                            sig_types.add(sig.get("type", "unknown"))

        # Create a bucket for each direction + signal_type combination
        if sig_types:
            for st in sig_types:
                key = f"{direction}_{st}"
                bucket_total[key] = bucket_total.get(key, 0) + 1
                if outcome == "WIN":
                    bucket_wins[key] = bucket_wins.get(key, 0) + 1
        else:
            # Fallback: just direction
            key = direction
            bucket_total[key] = bucket_total.get(key, 0) + 1
            if outcome == "WIN":
                bucket_wins[key] = bucket_wins.get(key, 0) + 1

    total_resolved = len(rows)

    # Filter to buckets with minimum sample size of 5
    min_sample = 5
    buckets: dict[str, float] = {}
    bucket_counts: dict[str, int] = {}
    for key, count in bucket_total.items():
        if count >= min_sample:
            wins = bucket_wins.get(key, 0)
            buckets[key] = round(wins / count, 2) if count > 0 else 0.0
            bucket_counts[key] = count

    return {
        "buckets": buckets,
        "bucket_counts": bucket_counts,
        "total_resolved": total_resolved,
        "calibration_warning": total_resolved < 30,
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

    lines = ["## Historical Accuracy (last 14d, min 5 samples)"]
    for key in sorted(buckets.keys()):
        pct = int(buckets[key] * 100)
        n = bucket_counts.get(key, 0)
        # Format key: "LONG_technical_trend" → "LONG technical_trend"
        display_key = key.replace("_", " ", 1)
        lines.append(f"{display_key}: {pct}% (n={n})")

    if warning:
        lines.append(
            f"⚠️ Calibration data thin (total resolved: {total} — need 30+ per bucket for reliability)"
        )

    return "\n".join(lines)
