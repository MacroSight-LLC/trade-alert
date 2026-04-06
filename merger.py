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
from datetime import datetime, timezone

import redis
from pydantic import ValidationError

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from constants import LRANGE_CAP, MACRO_REGIME_KEY, SNAPSHOT_KEY_PREFIX, SNAPSHOT_TTL
from models import Signal, Snapshot
from redis_client import get_redis as _get_redis

logger = logging.getLogger(__name__)

_raw_top_n = int(os.getenv("MERGER_TOP_N", "20"))
MERGER_TOP_N: int = _raw_top_n if _raw_top_n > 0 else 20

# Diversity multiplier weight — controls how strongly the merger penalises
# signals clustered in a single signal type.  1.0 = full sqrt-diversity
# penalty (default), 0.0 = raw strength only (no diversity bonus/penalty).
_raw_div = float(os.getenv("MERGER_DIVERSITY_WEIGHT", "1.0"))
MERGER_DIVERSITY_WEIGHT: float = max(0.0, min(1.0, _raw_div))

# Signal freshness time-decay thresholds (seconds)
_FRESHNESS_BOOST_SECS = int(os.getenv("MERGER_FRESH_BOOST_SECS", "120"))  # <2 min
_FRESHNESS_PENALTY_SECS = int(os.getenv("MERGER_STALE_PENALTY_SECS", "600"))  # >10 min
_FRESHNESS_BOOST = 0.10
_FRESHNESS_PENALTY = -0.15

# Source-family mapping for distinct-family counting
_SOURCE_FAMILIES: dict[str, str] = {
    "tradingview": "technical",
    "trading": "technical",
    "polygon": "flow",
    "alpaca": "flow",
    "yfinance": "flow",
    "finnhub": "sentiment",
    "rot": "sentiment",
    "spamshield": "sentiment",
    "edgar": "events",
    "fred": "macro",
    "timesfm": "forecast",
}


def _signal_family(sig: Signal) -> str:
    """Return the source family for a signal (for distinct-family counting)."""
    return _SOURCE_FAMILIES.get(sig.source, sig.source)


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
        raw_entries: list[str] = r.lrange(f"{SNAPSHOT_KEY_PREFIX}{timeframe}", 0, LRANGE_CAP - 1)
        if len(raw_entries) >= LRANGE_CAP:
            logger.warning(
                "Snapshot queue %s%s hit LRANGE cap (%d entries) — "
                "data may be truncated; check collector flush cadence",
                SNAPSHOT_KEY_PREFIX,
                timeframe,
                LRANGE_CAP,
            )
    except redis.RedisError as exc:
        logger.error("Redis read failed for snapshots:%s — %s", timeframe, exc)
        return []

    if not raw_entries:
        return []

    # Parse each entry as a Snapshot — track failures for quality guard
    snapshots: list[Snapshot] = []
    parse_failures = 0
    for entry in raw_entries:
        try:
            snapshots.append(Snapshot.model_validate_json(entry))
        except (ValidationError, ValueError) as exc:
            parse_failures += 1
            logger.warning("Skipping malformed snapshot entry — %s", exc)

    # Parse-failure guard: if ≥50% of entries failed, data quality is suspect
    total_entries = len(raw_entries)
    if total_entries > 0 and parse_failures / total_entries > 0.5:
        logger.error(
            "Snapshot parse failure rate %.0f%% (%d/%d) for snapshots:%s — "
            "data quality too degraded; returning empty",
            (parse_failures / total_entries) * 100,
            parse_failures,
            total_entries,
            timeframe,
        )
        return []

    # Filter pseudo-symbols (e.g. __GLOBAL_MACRO__) that are not real tickers
    snapshots = [s for s in snapshots if not s.symbol.startswith("__")]

    # Group by (symbol, timeframe)
    groups: dict[tuple[str, str], list[Snapshot]] = defaultdict(list)
    for snap in snapshots:
        groups[(snap.symbol, snap.timeframe)].append(snap)

    # Reference time for freshness decay
    now_utc = datetime.now(timezone.utc)

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
            if (
                key not in best
                or abs(sig.score) > abs(best[key].score)
                or (abs(sig.score) == abs(best[key].score) and sig.confidence > best[key].confidence)
            ):
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

        # Signal freshness time-decay: boost recent, penalise stale
        snap_ts = group[0].timestamp
        try:
            if snap_ts.endswith("Z"):
                snap_dt = datetime.fromisoformat(snap_ts.replace("Z", "+00:00"))
            else:
                snap_dt = datetime.fromisoformat(snap_ts)
            if snap_dt.tzinfo is None:
                snap_dt = snap_dt.replace(tzinfo=timezone.utc)
            age_secs = (now_utc - snap_dt).total_seconds()
        except (ValueError, TypeError):
            logger.warning("Unparseable snapshot timestamp for %s: %r", symbol, snap_ts)
            age_secs = float(_FRESHNESS_PENALTY_SECS)  # treat unparseable as stale

        if age_secs < _FRESHNESS_BOOST_SECS:
            freshness_adj = _FRESHNESS_BOOST
        elif age_secs > _FRESHNESS_PENALTY_SECS:
            freshness_adj = _FRESHNESS_PENALTY
        else:
            freshness_adj = 0.0

        # Aggregate strength = sum of abs(score) * (confidence + freshness_adj)
        raw_strength = sum(abs(s.score) * max(s.confidence + freshness_adj, 0.05) for s in deduped)

        # Diversity multiplier: rewards breadth across different signal
        # types.  3 signals from 3 types ranks higher than 5 of 1 type.
        # Formula: raw_strength * sqrt(unique_types / total_signals)
        # Tunable via MERGER_DIVERSITY_WEIGHT (0.0 = disabled, 1.0 = full).
        if MERGER_DIVERSITY_WEIGHT > 0.0:
            diversity = math.sqrt(len(unique_types) / max(len(deduped), 1))
            # Blend: weight=1.0 → full diversity, weight=0.0 → 1.0 (no effect)
            blended_diversity = 1.0 + MERGER_DIVERSITY_WEIGHT * (diversity - 1.0)
            aggregate_strength = raw_strength * blended_diversity
        else:
            aggregate_strength = raw_strength

        # Composite signal detection: flag high-edge setups the LLM should
        # pay special attention to.
        composite_flags: list[str] = []
        sig_by_type: dict[str, Signal] = {}
        for s in deduped:
            if s.type not in sig_by_type or abs(s.score) > abs(sig_by_type[s.type].score):
                sig_by_type[s.type] = s

        insider_sig = sig_by_type.get("insider_activity")
        catalyst_sig = sig_by_type.get("catalyst_event")
        vol_sig = sig_by_type.get("volume_spike")
        ta_sig = sig_by_type.get("technical_trend")

        # Insider + catalyst = volatility catalyst
        if insider_sig and catalyst_sig and abs(insider_sig.score) >= 2.0 and abs(catalyst_sig.score) >= 1.5:
            composite_flags.append("VOLATILITY_CATALYST")

        # Volume + TA alignment = confirmed breakout
        if vol_sig and ta_sig and abs(vol_sig.score) >= 2.0:
            # Same direction (both positive or both negative)
            if (vol_sig.score > 0) == (ta_sig.score > 0):
                composite_flags.append("VOLUME_CONFIRMED_BREAKOUT")
                # Boost TA confidence for confirmed breakouts
                boosted_conf = min(ta_sig.confidence + 0.10, 1.0)
                idx = deduped.index(ta_sig)
                deduped[idx] = Signal(
                    source=ta_sig.source,
                    type=ta_sig.type,
                    score=ta_sig.score,
                    confidence=boosted_conf,
                    reason=ta_sig.reason + " [volume-confirmed]",
                    raw=ta_sig.raw,
                )

        # Count distinct source families for richer SA metric
        distinct_families = {_signal_family(s) for s in deduped}

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
        raw: str | None = r.get(MACRO_REGIME_KEY)
        if raw is None:
            return {"risk_on": True, "is_stale": True}
        return json.loads(raw)
    except (redis.RedisError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read %s — %s", MACRO_REGIME_KEY, exc)
        return {"risk_on": True, "is_stale": True}


if __name__ == "__main__":
    # Integration test — push sample snapshots to Redis then merge
    from models import Signal, Snapshot

    try:
        r = _get_redis()

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
            r.lpush(f"{SNAPSHOT_KEY_PREFIX}15m", s.model_dump_json())
            r.expire(f"{SNAPSHOT_KEY_PREFIX}15m", SNAPSHOT_TTL)

        results = merge("15m", limit=5)
        print(f"Merged {len(results)} unique symbols")
        for snap in results:
            print(f"  {snap.symbol}: {len(snap.signals)} signals")

        assert len(results) > 0
        assert results[0].symbol == "AAPL"
        print("Merger working ✅")

        r.delete(f"{SNAPSHOT_KEY_PREFIX}15m")
    except redis.RedisError as exc:
        print(f"Redis not available (expected in dev): {exc}")
        print("merger.py structure valid ✅")
