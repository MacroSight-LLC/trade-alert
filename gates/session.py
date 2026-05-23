"""Market session gate overlays and session stats."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo


def _market_hours_status(now: datetime | None = None) -> str:
    """Resolve market hours via validate_and_filter for test patch compatibility."""
    import validate_and_filter as vf

    return vf.get_market_hours_status(now)


def _get_redis():
    import validate_and_filter as vf

    return vf.get_redis()


if TYPE_CHECKING:
    from validate_and_filter import GateRejection

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")
_SESSION_STATS_TTL_SECONDS = int(os.environ.get("SESSION_STATS_TTL_SECONDS", "604800"))

# Server-side market-session gating controls.
_MARKET_HOURS_GATES_ENABLED: bool = os.environ.get("MARKET_HOURS_GATES_ENABLED", "1") == "1"
_SESSION_PREPOST_EP_BUMP: float = float(os.environ.get("SESSION_PREPOST_EP_BUMP", "0.03"))
_SESSION_PREPOST_CONF_BUMP: float = float(os.environ.get("SESSION_PREPOST_CONF_BUMP", "0.05"))
_SESSION_PREPOST_SA_BUMP: int = int(os.environ.get("SESSION_PREPOST_SA_BUMP", "2"))


def _session_stats_key(timeframe: str, now: datetime | None = None) -> str:
    now_utc = now or datetime.now(UTC)
    session_date = now_utc.astimezone(_ET).date().isoformat()
    return f"session:stats:{session_date}:{timeframe}"


def _record_session_gate_metrics(
    timeframe: str,
    llm_candidates: int,
    directional_passed: int,
    watch_kept: int,
    directional_rejections: list[tuple[str, GateRejection]],
    watch_rejections: list[tuple[str, GateRejection]],
    dedup_suppressed_count: int = 0,
) -> None:
    try:
        redis_client = _get_redis()
        key = _session_stats_key(timeframe)
        pipe = redis_client.pipeline()
        pipe.hincrby(key, "decision_runs", 1)
        pipe.hincrby(key, "llm_candidates", llm_candidates)
        pipe.hincrby(key, "alerts_passed", directional_passed)
        pipe.hincrby(key, "alerts_passed_directional", directional_passed)
        pipe.hincrby(key, "alerts_passed_total", directional_passed + watch_kept)
        pipe.hincrby(key, "watch_kept", watch_kept)
        total_rejections = len(directional_rejections) + len(watch_rejections)
        pipe.hincrby(key, "alerts_rejected", total_rejections)
        pipe.hincrby(key, "alerts_rejected_directional", len(directional_rejections))
        pipe.hincrby(key, "alerts_rejected_watch", len(watch_rejections))
        if dedup_suppressed_count:
            pipe.hincrby(key, "alerts_dedup_suppressed", dedup_suppressed_count)
        for _symbol, gate in directional_rejections:
            pipe.hincrby(key, f"gate_dir_{gate.value}", 1)
            pipe.hincrby(key, f"gate_{gate.value}", 1)
        for _symbol, gate in watch_rejections:
            pipe.hincrby(key, f"gate_watch_{gate.value}", 1)
            pipe.hincrby(key, f"gate_{gate.value}", 1)
        pipe.expire(key, _SESSION_STATS_TTL_SECONDS)
        pipe.execute()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to record session gate metrics: %s", exc)


def _market_session_bucket(now: datetime | None = None) -> str:
    status = _market_hours_status(now)
    lower = status.lower()
    if lower.startswith("regular trading hours"):
        return "regular"
    if lower.startswith("pre-market"):
        return "pre"
    if lower.startswith("after-hours"):
        return "after"
    return "closed"


def _apply_market_session_gate_overlays(
    ep_gate: float,
    sa_gate: int,
    conf_gate: float,
    timeframe: str,
    now: datetime | None = None,
) -> tuple[float, int, float, str]:
    """Apply market-session gate overlays (SSOT §10.2 market session gates)."""
    import validate_and_filter as vf

    session_bucket = vf._market_session_bucket(now)
    if not vf._MARKET_HOURS_GATES_ENABLED:
        return ep_gate, sa_gate, conf_gate, session_bucket

    from gate_config import EXTENDED_HOURS_ALERTS_ENABLED

    if timeframe == "15m" and session_bucket in {"pre", "after"} and not EXTENDED_HOURS_ALERTS_ENABLED:
        return (
            min(ep_gate + vf._SESSION_PREPOST_EP_BUMP, 0.95),
            sa_gate + vf._SESSION_PREPOST_SA_BUMP,
            min(conf_gate + vf._SESSION_PREPOST_CONF_BUMP, 0.99),
            session_bucket,
        )
    return ep_gate, sa_gate, conf_gate, session_bucket
