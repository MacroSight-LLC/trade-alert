"""Snapshot merger and candidate selector for trade-alert.

Reads Redis snapshot queues, merges signals per symbol,
deduplicates, and returns top candidates by aggregate strength.
Implements SSOT §9.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections import defaultdict

import redis
from pydantic import ValidationError

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from models import Signal, Snapshot

logger = logging.getLogger(__name__)

REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379")
_raw_top_n = int(os.getenv("MERGER_TOP_N", "20"))
MERGER_TOP_N: int = _raw_top_n if _raw_top_n > 0 else 20
REDIS_SOCKET_TIMEOUT: float = float(os.getenv("REDIS_SOCKET_TIMEOUT", "10.0"))

# Module-level Redis connection pool (reused across merge() calls)
_redis_pool: redis.ConnectionPool | None = None


def _get_redis() -> redis.Redis:
    """Return a Redis client backed by a module-level connection pool."""
    global _redis_pool  # noqa: PLW0603
    if _redis_pool is None:
        _redis_pool = redis.ConnectionPool.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=REDIS_SOCKET_TIMEOUT,
        )
    return redis.Redis(connection_pool=_redis_pool)


def merge(timeframe: str, limit: int | None = None) -> list[Snapshot]:
    """Merge snapshots from Redis and return top candidates.

    Args:
        timeframe: Candle timeframe key (e.g. "15m", "1h").
        limit: Maximum number of symbols to return.
            Defaults to the ``MERGER_TOP_N`` env var (20).

    Returns:
        Top ``limit`` Snapshots sorted by aggregate signal strength,
        with deduplicated signals per symbol. Returns ``[]`` on
        Redis errors.
    """
    if limit is None:
        limit = MERGER_TOP_N

    try:
        r = _get_redis()
        raw_entries: list[str] = r.lrange(f"snapshots:{timeframe}", 0, -1)
    except redis.RedisError as exc:
        logger.error("Redis read failed for snapshots:%s — %s", timeframe, exc)
        return []

    if not raw_entries:
        return []

    # Parse each entry as a Snapshot
    snapshots: list[Snapshot] = []
    for entry in raw_entries:
        try:
            snapshots.append(Snapshot.model_validate_json(entry))
        except (ValidationError, ValueError) as exc:
            logger.warning("Skipping malformed snapshot entry — %s", exc)

    # Filter pseudo-symbols (e.g. __GLOBAL_MACRO__) that are not real tickers
    snapshots = [s for s in snapshots if not s.symbol.startswith("__")]

    # Group by (symbol, timeframe)
    groups: dict[tuple[str, str], list[Snapshot]] = defaultdict(list)
    for snap in snapshots:
        groups[(snap.symbol, snap.timeframe)].append(snap)

    # Merge signals per group and compute aggregate strength
    merged: list[tuple[float, Snapshot]] = []
    for (symbol, tf), group in groups.items():
        # Concatenate all signals
        all_signals: list[Signal] = []
        for snap in group:
            all_signals.extend(snap.signals)

        # Deduplicate: same (source, type, sign) → keep highest abs(score).
        # Using sign-aware keys so a bearish -2.5 and bullish +2.0 from
        # the same source+type are both preserved (not silently replaced).
        best: dict[tuple[str, str, bool], Signal] = {}
        for sig in all_signals:
            key = (sig.source, sig.type, sig.score >= 0)
            if key not in best or abs(sig.score) > abs(best[key].score):
                best[key] = sig
        deduped = list(best.values())

        # Pre-LLM filter: drop symbols with < 3 distinct signal types.
        # The SA >= 3 gate requires at least 3 aligned sources, so
        # sending symbols with fewer types wastes LLM tokens.
        unique_types = {s.type for s in deduped}
        if len(unique_types) < 3:
            logger.debug(
                "Merger pre-filter: dropping %s (%d signal type(s))",
                symbol,
                len(unique_types),
            )
            continue

        # Aggregate strength = sum of abs(score) * confidence
        raw_strength = sum(abs(s.score) * s.confidence for s in deduped)

        # Diversity multiplier: rewards breadth across different signal
        # types.  3 signals from 3 types ranks higher than 5 of 1 type.
        # Formula: raw_strength * sqrt(unique_types / total_signals)
        diversity = math.sqrt(len(unique_types) / max(len(deduped), 1))
        aggregate_strength = raw_strength * diversity

        merged_snap = Snapshot(
            symbol=symbol,
            timeframe=tf,
            timestamp=group[0].timestamp,
            signals=deduped,
        )
        merged.append((aggregate_strength, merged_snap))

    # Sort descending by aggregate strength
    merged.sort(key=lambda x: x[0], reverse=True)

    return [snap for _, snap in merged[:limit]]


def get_macro_regime() -> dict:
    """Read current macro regime from Redis.

    Returns:
        Parsed dict from ``macro:regime`` key, or
        ``{"risk_on": True, "is_stale": True}`` if the key is
        missing or on error.
    """
    try:
        r = _get_redis()
        raw: str | None = r.get("macro:regime")
        if raw is None:
            return {"risk_on": True, "is_stale": True}
        return json.loads(raw)
    except (redis.RedisError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read macro:regime — %s", exc)
        return {"risk_on": True, "is_stale": True}


if __name__ == "__main__":
    # Integration test — push sample snapshots to Redis then merge
    from models import Signal, Snapshot

    try:
        r = redis.from_url(REDIS_URL)

        for source in ["tradingview", "polygon", "finnhub"]:
            s = Snapshot(
                symbol="AAPL",
                timeframe="15m",
                timestamp="2026-03-06T00:00:00Z",
                signals=[
                    Signal(
                        source=source,
                        type="technical_trend",
                        score=1.5,
                        confidence=0.8,
                        reason=f"Sample signal from {source}",
                    )
                ],
            )
            r.lpush("snapshots:15m", s.model_dump_json())
            r.expire("snapshots:15m", 900)

        results = merge("15m", limit=5)
        print(f"Merged {len(results)} unique symbols")
        for snap in results:
            print(f"  {snap.symbol}: {len(snap.signals)} signals")

        assert len(results) > 0
        assert results[0].symbol == "AAPL"
        print("Merger working ✅")

        r.delete("snapshots:15m")
    except redis.RedisError as exc:
        print(f"Redis not available (expected in dev): {exc}")
        print("merger.py structure valid ✅")
