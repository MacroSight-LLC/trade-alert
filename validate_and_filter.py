"""Server-side validate-and-filter for LLM decision output.

Extracted from inline decision YAML code blocks for testability and
DRY across both 15m and 1h decision workflows.

Implements SSOT §4 (PlaybookAlert validation) and §10.2/10.3 gates:
  - edge_probability, sources_agree, confidence thresholds
  - EP ceiling by actual source count (prevents LLM inflation)
  - VIX hard gate (universal reject when VIX > 30)
    - Symbol hallucination detection (symbol absent from merged snapshots)
    - Deterministic server-side sources_agree from aligned signal families
  - R:R minimum (2:1 for actionable alerts)
  - R:R zero-risk rejection (stop == level → hard reject)
  - VIX + risk-off soft gate (VIX > 25 suppresses weak longs/shorts)
  - 1h macro veto (strong macro_risk_off score discounts longs)
"""

from __future__ import annotations

import re
import json
import logging
import math
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from constants import MACRO_STALE_SECONDS as _MACRO_STALE_SECONDS
from constants import get_market_hours_status
from metrics import ALERTS_PER_CYCLE, GATE_REJECTIONS
from models import PlaybookAlert
from redis_client import get_redis

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")
_SESSION_STATS_TTL_SECONDS = int(os.environ.get("SESSION_STATS_TTL_SECONDS", "604800"))


# ── Gate-rejection enum ──────────────────────────────────────────
class GateRejection(str, Enum):
    """Structured gate rejection reasons for telemetry."""

    VIX_HARD = "vix_hard"
    SOURCE_HALLUCINATION = "source_hallucination"
    EP_THRESHOLD = "ep_threshold"
    SA_THRESHOLD = "sa_threshold"
    HIGH_CONFIDENCE_ALIGNMENT = "high_confidence_alignment"
    CONF_THRESHOLD = "conf_threshold"
    RR_MINIMUM = "rr_minimum"
    RR_ZERO_RISK = "rr_zero_risk"
    ENTRY_ORDER_INVALID = "entry_order_invalid"
    MACRO_VETO = "macro_veto"
    VIX_SOFT = "vix_soft"
    FORECAST_CONTRADICTS = "forecast_contradicts"
    TIMEFRAME_INVALID = "timeframe_invalid"
    ENTRY_MARKET_DRIFT = "entry_market_drift"
    VOLUME_UNCONFIRMED = "volume_unconfirmed"
    WATCH_EP_THRESHOLD = "watch_ep_threshold"
    WATCH_SA_THRESHOLD = "watch_sa_threshold"
    WATCH_CONF_THRESHOLD = "watch_conf_threshold"
    WATCH_CAP = "watch_cap"
    WATCH_DROPPED_DIRECTIONAL_PRESENT = "watch_dropped_directional_present"
    WATCH_DECAY = "watch_decay"
    MARKET_SESSION_CLOSED = "market_session_closed"


# Valid alert timeframes
_VALID_TIMEFRAMES = {"5m", "15m", "1h", "4h", "1D"}


# ── EP ceiling lookup table ──────────────────────────────────────
# Maps the number of *actual* distinct signal types in the snapshot
# to the maximum allowed edge_probability.  Prevents the LLM from
# assigning inflated EP values that aren't supported by the evidence.
# Overridable via EP_CEILING_JSON env var (JSON dict str→float).
_DEFAULT_EP_CEILING: dict[int, float] = {
    1: 0.55,
    2: 0.65,
    3: 0.75,
    4: 0.85,
    5: 0.90,
    6: 0.92,
    7: 0.95,
    8: 0.96,
    9: 0.97,
    10: 0.98,
    11: 0.99,
}


def _load_ep_ceiling() -> dict[int, float]:
    """Load EP ceiling from EP_CEILING_JSON env var or use defaults.

    Returns:
        Mapping of source count → max edge_probability.
    """
    raw = os.environ.get("EP_CEILING_JSON", "")
    if raw:
        try:
            parsed = json.loads(raw)
            return {int(k): float(v) for k, v in parsed.items()}
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("Invalid EP_CEILING_JSON, using defaults: %s", e)
    return dict(_DEFAULT_EP_CEILING)


EP_CEILING: dict[int, float] = _load_ep_ceiling()

# Per-timeframe gate thresholds (SSOT §10.2 / §10.3)
# Configurable via env vars: GATE_EP_15M, GATE_EP_1H, GATE_SA, GATE_CONF
_GATE_EP: dict[str, float] = {
    "15m": float(os.environ.get("GATE_EP_15M", "0.70")),
    "1h": float(os.environ.get("GATE_EP_1H", "0.75")),
}
# SA gate = minimum number of independent signal families aligned to direction.
# 7 families exist (trend, volume, sentiment, flow, events, macro, positioning);
# requiring 4 ensures meaningful multi-family conviction.
_GATE_SA: int = int(os.environ.get("GATE_SA", "4"))
_GATE_CONF: float = float(os.environ.get("GATE_CONF", "0.75"))
_HIGH_CONFIDENCE_MIN: float = float(os.environ.get("HIGH_CONFIDENCE_MIN", "0.85"))
_HIGH_CONFIDENCE_MIN_SA: int = int(os.environ.get("HIGH_CONFIDENCE_MIN_SA", "5"))

# Per-timeframe R:R minimums (reward must be >= N × risk to be actionable).
# 15m setups are shorter-lived so 2:1 is sufficient; 1h setups warrant 2.5:1.
_GATE_RR: dict[str, float] = {
    "15m": float(os.environ.get("GATE_RR_15M", "2.0")),
    "1h": float(os.environ.get("GATE_RR_1H", "2.5")),
}

# Limited WATCH policy (borderline-only, conservative)
_WATCH_MAX_PER_RUN: int = int(os.environ.get("WATCH_MAX_PER_RUN", "1"))
_WATCH_SA_MIN: int = int(os.environ.get("WATCH_SA_MIN", "2"))
_WATCH_CONF_MIN: float = float(os.environ.get("WATCH_CONF_MIN", "0.60"))
_WATCH_EP_DELTA: float = float(os.environ.get("WATCH_EP_DELTA", "0.05"))
# WATCH decay: drop WATCH alerts that persist unresolved across N pipeline cycles
_WATCH_DECAY_CYCLES: int = int(os.environ.get("WATCH_DECAY_CYCLES", "4"))
_WATCH_DECAY_TTL_SECONDS: int = int(os.environ.get("WATCH_DECAY_TTL_SECONDS", str(60 * 60 * 24)))
# WATCH cap by market regime — stressed regimes get fewer WATCHes.
# _WATCH_MAX_PER_RUN is kept as the backward-compatible default for stressed regimes.
_WATCH_MAX_STRESSED: int = int(os.environ.get("WATCH_MAX_STRESSED", str(_WATCH_MAX_PER_RUN)))
_WATCH_MAX_NEUTRAL: int = int(os.environ.get("WATCH_MAX_NEUTRAL", "2"))
_WATCH_MAX_TRENDING: int = int(os.environ.get("WATCH_MAX_TRENDING", "3"))
# WATCH promotion: sort bonus multiplier for setups with improving EP across cycles.
_WATCH_PROMOTION_BONUS_MULT: float = float(os.environ.get("WATCH_PROMOTION_BONUS_MULT", "1.15"))
_WATCH_PROMOTION_MIN_CYCLES: int = int(os.environ.get("WATCH_PROMOTION_MIN_CYCLES", "2"))

# Deterministic sources_agree from server-side family alignment.
# Minimum mean family score to count a family as directionally aligned.
# Scores range 0–3; 0.25 = ~8% of max, requiring at least weak directional commitment.
_SA_FAMILY_MIN_SCORE: float = float(os.environ.get("SA_FAMILY_MIN_SCORE", "0.25"))
# Include top-level macro context in deterministic sources_agree when
# per-symbol macro signals are unavailable (they are stripped upstream).
_SA_INCLUDE_MACRO_CONTEXT: bool = os.environ.get("SA_INCLUDE_MACRO_CONTEXT", "1") == "1"
_SA_MACRO_CONTEXT_SCORE: float = float(os.environ.get("SA_MACRO_CONTEXT_SCORE", "0.50"))
_SA_FORECAST_CONFIRM_BONUS_ENABLED: bool = os.environ.get("SA_FORECAST_CONFIRM_BONUS_ENABLED", "1") == "1"
_SA_FORECAST_BONUS_THRESHOLD: float = float(os.environ.get("SA_FORECAST_BONUS_THRESHOLD", "0.80"))

# Server-side market-session gating controls.
_MARKET_HOURS_GATES_ENABLED: bool = os.environ.get("MARKET_HOURS_GATES_ENABLED", "1") == "1"
_SESSION_PREPOST_EP_BUMP: float = float(os.environ.get("SESSION_PREPOST_EP_BUMP", "0.03"))
_SESSION_PREPOST_CONF_BUMP: float = float(os.environ.get("SESSION_PREPOST_CONF_BUMP", "0.05"))
_SESSION_PREPOST_SA_BUMP: int = int(os.environ.get("SESSION_PREPOST_SA_BUMP", "2"))

# Macro veto bypass thresholds (configurable)
_MACRO_VETO_SA: int = int(os.environ.get("MACRO_VETO_SA", "6"))
_MACRO_VETO_EP: float = float(os.environ.get("MACRO_VETO_EP", "0.90"))

# VIX soft-gate bypass thresholds (configurable)
_VIX_SOFT_THRESHOLD: float = float(os.environ.get("VIX_SOFT_THRESHOLD", "25.0"))
_VIX_SOFT_SA: int = int(os.environ.get("VIX_SOFT_SA", "3"))
_VIX_SOFT_EP: float = float(os.environ.get("VIX_SOFT_EP", "0.72"))

# Forecast contradiction gate thresholds (configurable)
_FORECAST_GATE_SCORE_THRESHOLD: float = float(os.environ.get("FORECAST_GATE_SCORE_THRESHOLD", "0.8"))
_FORECAST_GATE_SA: int = int(os.environ.get("FORECAST_GATE_SA", "5"))
_FORECAST_GATE_EP: float = float(os.environ.get("FORECAST_GATE_EP", "0.85"))

# Volume confirmation: minimum volume_spike score required for LONG/SHORT.
# Alerts without volume confirmation get confidence downgraded by this amount.
# In choppy / risk_off_high_vix regimes the penalty is larger — thin-volume
# breakouts are significantly more unreliable in indecisive or stressed markets.
_VOLUME_CONFIRM_SCORE: float = float(os.environ.get("VOLUME_CONFIRM_SCORE", "1.5"))
_VOLUME_CONFIRM_PENALTY: float = float(os.environ.get("VOLUME_CONFIRM_PENALTY", "0.05"))
_VOLUME_CONFIRM_PENALTY_CHOPPY: float = float(os.environ.get("VOLUME_CONFIRM_PENALTY_CHOPPY", "0.10"))

# Reject alerts where entry is too far from latest reference price (e.g., stale/unrealistic fills)
_ENTRY_MARKET_DRIFT_MAX_PCT: float = float(os.environ.get("ENTRY_MARKET_DRIFT_MAX_PCT", "0.03"))
_ENTRY_MARKET_DRIFT_VIX_BUMP: float = float(os.environ.get("ENTRY_MARKET_DRIFT_VIX_BUMP", "0.01"))
_ENTRY_MARKET_DRIFT_PREPOST_BUMP: float = float(os.environ.get("ENTRY_MARKET_DRIFT_PREPOST_BUMP", "0.01"))
_ENTRY_MARKET_DRIFT_CAP_PCT: float = float(os.environ.get("ENTRY_MARKET_DRIFT_CAP_PCT", "0.08"))
# Second VIX tier: extreme-volatility regimes (VIX >= 30) add extra tolerance on top of soft-threshold bump
_ENTRY_MARKET_DRIFT_VIX_HIGH_THRESHOLD: float = float(os.environ.get("ENTRY_MARKET_DRIFT_VIX_HIGH_THRESHOLD", "30.0"))
_ENTRY_MARKET_DRIFT_VIX_HIGH_BUMP: float = float(os.environ.get("ENTRY_MARKET_DRIFT_VIX_HIGH_BUMP", "0.02"))

# Dynamic gate controls (regime + timeframe overlays)
_DYNAMIC_GATES_ENABLED: bool = os.environ.get("DYNAMIC_GATES_ENABLED", "1") == "1"
_REGIME_CHOPPY_EP_BUMP: float = float(os.environ.get("REGIME_CHOPPY_EP_BUMP", "0.03"))
_REGIME_CHOPPY_CONF_BUMP: float = float(os.environ.get("REGIME_CHOPPY_CONF_BUMP", "0.03"))
_REGIME_CHOPPY_SA_BUMP: int = int(os.environ.get("REGIME_CHOPPY_SA_BUMP", "1"))
_REGIME_TRENDING_EP_REDUCE: float = float(os.environ.get("REGIME_TRENDING_EP_REDUCE", "0.01"))
_REGIME_TRENDING_CONF_REDUCE: float = float(os.environ.get("REGIME_TRENDING_CONF_REDUCE", "0.01"))
# risk_off_high_vix regime (VIX 25–30 + risk_off=True): tighter gates, same magnitude as choppy.
# Previously this regime was classified but completely unhandled — gates fell through unchanged.
_REGIME_RISK_OFF_HIGH_VIX_EP_BUMP: float = float(os.environ.get("REGIME_RISK_OFF_HIGH_VIX_EP_BUMP", "0.03"))
_REGIME_RISK_OFF_HIGH_VIX_CONF_BUMP: float = float(os.environ.get("REGIME_RISK_OFF_HIGH_VIX_CONF_BUMP", "0.03"))
_REGIME_RISK_OFF_HIGH_VIX_SA_BUMP: int = int(os.environ.get("REGIME_RISK_OFF_HIGH_VIX_SA_BUMP", "1"))
_TF_EP_OFFSET_15M: float = float(os.environ.get("TF_EP_OFFSET_15M", "0.00"))
_TF_EP_OFFSET_1H: float = float(os.environ.get("TF_EP_OFFSET_1H", "0.00"))
_TF_CONF_OFFSET_15M: float = float(os.environ.get("TF_CONF_OFFSET_15M", "0.00"))
_TF_CONF_OFFSET_1H: float = float(os.environ.get("TF_CONF_OFFSET_1H", "0.00"))

_TYPE_FAMILY: dict[str, str] = {
    "technical_trend": "trend",
    "relative_strength": "trend",
    "price_forecast": "trend",
    "volume_spike": "volume",
    "sentiment_bull": "sentiment",
    "sentiment_bear": "sentiment",
    "options_flow": "flow",
    "insider_activity": "events",
    "catalyst_event": "events",
    "macro_risk_off": "macro",
    "short_interest": "positioning",
}


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _rr(alert: PlaybookAlert) -> float:
    risk = abs(alert.entry["level"] - alert.entry["stop"])
    if risk <= 0:
        return 0.0
    reward = abs(alert.entry["target"] - alert.entry["level"])
    return reward / risk


def _candidate_distribution(alerts: list[PlaybookAlert]) -> dict[str, float]:
    return {
        "count": float(len(alerts)),
        "median_ep": _median([a.edge_probability for a in alerts]),
        "median_conf": _median([a.confidence for a in alerts]),
        "median_rr": _median([_rr(a) for a in alerts]),
        "median_sa": _median([float(a.sources_agree) for a in alerts]),
    }


def _signal_surface(snaps: list[dict[str, Any]]) -> tuple[int, int, float]:
    bulls = 0
    bears = 0
    strengths: list[float] = []
    for snap in snaps:
        for sig in snap.get("signals", []):
            st = sig.get("type", "")
            try:
                sc = float(sig.get("score", 0.0))
            except (TypeError, ValueError):
                continue
            if sc > 0 and st in (
                "technical_trend", "sentiment_bull", "options_flow", "relative_strength",
                "price_forecast", "insider_activity", "catalyst_event",
            ):
                bulls += 1
                strengths.append(min(abs(sc) / 3.0, 1.0))
            elif sc < 0 and st in (
                "technical_trend", "options_flow", "relative_strength",
                "price_forecast", "insider_activity", "catalyst_event",
            ):
                bears += 1
                strengths.append(min(abs(sc) / 3.0, 1.0))
            elif st in ("sentiment_bear", "macro_risk_off") and sc > 0:
                bears += 1
                strengths.append(min(abs(sc) / 3.0, 1.0))
            elif st == "short_interest" and sc > 0:
                # High short interest amplifies existing bulls (squeeze potential)
                bulls += 1
                strengths.append(min(abs(sc) / 3.0, 1.0))
    trend_strength = sum(strengths) / len(strengths) if strengths else 0.0
    return bulls, bears, trend_strength


def _classify_regime(vix: float, risk_off: bool, bulls: int, bears: int, trend_strength: float) -> str:
    total = bulls + bears
    bull_ratio = (bulls / total) if total else 0.5
    if vix > 30:
        return "extreme"
    if risk_off and vix >= 25:
        return "risk_off_high_vix"
    if trend_strength < 0.35 or (0.45 <= bull_ratio <= 0.55):
        return "choppy"
    if bull_ratio > 0.55 and not risk_off:
        return "trending_up"
    if bull_ratio < 0.45 and risk_off:
        return "trending_down"
    return "neutral"


def _dynamic_gates(base_ep: float, base_sa: int, base_conf: float, timeframe: str, regime: str) -> tuple[float, int, float]:
    ep = base_ep
    sa = base_sa
    conf = base_conf
    if not _DYNAMIC_GATES_ENABLED:
        return ep, sa, conf
    if regime == "choppy":
        ep += _REGIME_CHOPPY_EP_BUMP
        sa += _REGIME_CHOPPY_SA_BUMP
        conf += _REGIME_CHOPPY_CONF_BUMP
    elif regime == "risk_off_high_vix":
        # Previously unhandled — gates fell through unchanged.
        # Now applies a matching tightening: elevated conviction required
        # when VIX is 25–30 and macro is risk-off.
        ep += _REGIME_RISK_OFF_HIGH_VIX_EP_BUMP
        sa += _REGIME_RISK_OFF_HIGH_VIX_SA_BUMP
        conf += _REGIME_RISK_OFF_HIGH_VIX_CONF_BUMP
    elif regime in ("trending_up", "trending_down"):
        ep -= _REGIME_TRENDING_EP_REDUCE
        conf -= _REGIME_TRENDING_CONF_REDUCE

    if timeframe == "15m":
        ep += _TF_EP_OFFSET_15M
        conf += _TF_CONF_OFFSET_15M
    elif timeframe == "1h":
        ep += _TF_EP_OFFSET_1H
        conf += _TF_CONF_OFFSET_1H

    ep = min(max(ep, 0.50), 0.95)
    sa = max(sa, 1)
    conf = min(max(conf, 0.50), 0.99)
    return ep, sa, conf


def _watch_max_for_regime(regime: str) -> int:
    """Return the maximum number of WATCH alerts to emit for a given regime.

    Stressed regimes (extreme, risk_off_high_vix) are capped tight.
    Neutral/choppy regimes allow 2.  Clear trending regimes allow 3.
    All values are env-var overridable via WATCH_MAX_STRESSED /
    WATCH_MAX_NEUTRAL / WATCH_MAX_TRENDING.
    """
    if regime in ("extreme", "risk_off_high_vix"):
        return _WATCH_MAX_STRESSED
    if regime in ("choppy", "neutral"):
        return _WATCH_MAX_NEUTRAL
    return _WATCH_MAX_TRENDING  # trending_up, trending_down


# ── WATCH cycle-decay helpers ────────────────────────────────────

def _watch_decay_key(symbol: str, timeframe: str) -> str:
    return f"watch:decay:{timeframe}:{symbol}"


def _get_watch_cycles(symbol: str, timeframe: str) -> int:
    """Return the number of consecutive pipeline cycles a WATCH has persisted."""
    try:
        val = get_redis().hget(_watch_decay_key(symbol, timeframe), "cycles")
        return int(val) if val else 0
    except Exception:  # noqa: BLE001
        return 0


def _incr_watch_cycles(symbol: str, timeframe: str, ep: float, conf: float) -> int:
    """Increment the watch cycle counter. Returns the new cycle count."""
    try:
        r = get_redis()
        key = _watch_decay_key(symbol, timeframe)
        pipe = r.pipeline()
        pipe.hincrby(key, "cycles", 1)
        pipe.hset(key, mapping={"last_ep": str(ep), "last_conf": str(conf)})
        pipe.expire(key, _WATCH_DECAY_TTL_SECONDS)
        results = pipe.execute()
        return int(results[0])
    except Exception:  # noqa: BLE001
        return 0


def _reset_watch_cycles(symbols: list[str], timeframe: str) -> None:
    """Delete watch-cycle state for symbols that graduated to a directional alert."""
    try:
        r = get_redis()
        for sym in symbols:
            r.delete(_watch_decay_key(sym, timeframe))
    except Exception:  # noqa: BLE001
        pass


def _get_watch_prev_state(symbol: str, timeframe: str) -> dict[str, str] | None:
    """Return the Redis watch-cycle state dict for symbol, or None if absent."""
    try:
        r = get_redis()
        state = r.hgetall(_watch_decay_key(symbol, timeframe))
        if state:
            return {k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v for k, v in state.items()}
        return None
    except Exception:  # noqa: BLE001
        return None


def _watch_is_improving(
    symbol: str,
    current_ep: float,
    prev_states: dict[str, dict[str, str] | None],
) -> bool:
    """Return True if the symbol's current EP exceeds its recorded prev EP.

    Used to boost improving WATCH setups in the sort ranking and to
    add a [\u2191 STRENGTHENING] prefix to the thesis after min cycles.
    Returns False if no prior state exists (new WATCH, never seen before).
    """
    state = prev_states.get(symbol)
    if not state:
        return False
    try:
        return current_ep > float(state.get("last_ep", 0.0))
    except (TypeError, ValueError):
        return False


def _session_stats_key(timeframe: str, now: datetime | None = None) -> str:
    now_utc = now or datetime.now(timezone.utc)
    session_date = now_utc.astimezone(_ET).date().isoformat()
    return f"session:stats:{session_date}:{timeframe}"


def _record_session_gate_metrics(
    timeframe: str,
    llm_candidates: int,
    directional_passed: int,
    watch_kept: int,
    directional_rejections: list[tuple[str, GateRejection]],
    watch_rejections: list[tuple[str, GateRejection]],
) -> None:
    try:
        redis_client = get_redis()
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
    status = get_market_hours_status(now)
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
    session_bucket = _market_session_bucket(now)
    if not _MARKET_HOURS_GATES_ENABLED:
        return ep_gate, sa_gate, conf_gate, session_bucket

    if timeframe == "15m" and session_bucket in {"pre", "after"}:
        return (
            min(ep_gate + _SESSION_PREPOST_EP_BUMP, 0.95),
            sa_gate + _SESSION_PREPOST_SA_BUMP,
            min(conf_gate + _SESSION_PREPOST_CONF_BUMP, 0.99),
            session_bucket,
        )
    return ep_gate, sa_gate, conf_gate, session_bucket


def _parse_snapshots(snapshots_json: str) -> list[dict[str, Any]]:
    """Parse snapshot JSON once for reuse by downstream helpers.

    Args:
        snapshots_json: JSON string of Snapshot dicts.

    Returns:
        Parsed list of snapshot dicts, or empty list on error.
    """
    try:
        parsed = json.loads(snapshots_json)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _build_snap_type_index(snaps: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Build per-symbol signal-type index from parsed snapshots.

    Args:
        snaps: List of parsed Snapshot dicts.

    Returns:
        Mapping of symbol → set of distinct signal type strings.
    """
    snap_types: dict[str, set[str]] = {}
    for s in snaps:
        sym = s.get("symbol", "")
        for sig in s.get("signals", []):
            snap_types.setdefault(sym, set()).add(sig.get("type", ""))
    return snap_types


def _signal_directional_score(sig_type: str, score: float) -> float:
    if sig_type == "sentiment_bear":
        # sentiment_bear scores are always positive; negate so they count for SHORT.
        return -abs(score)
    if sig_type == "macro_risk_off":
        # macro_risk_off uses SIGNED scores: positive = risk-off (bearish),
        # negative = risk-on (bullish).  Negate score (not abs) to preserve
        # this convention: risk-on (-1.0) → +1.0 (counts for LONG);
        # risk-off (+2.5) → -2.5 (counts for SHORT).  Using -abs() was a bug
        # that treated all macro signals as bearish regardless of regime.
        return -score
    return score


def _build_symbol_family_scores(snaps: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    family_accum: dict[str, dict[str, list[float]]] = {}
    for snap in snaps:
        symbol = str(snap.get("symbol", ""))
        if not symbol:
            continue
        for sig in snap.get("signals", []):
            if not isinstance(sig, dict):
                continue
            sig_type = str(sig.get("type", ""))
            family = _TYPE_FAMILY.get(sig_type)
            if family is None:
                continue
            try:
                raw_score = float(sig.get("score", 0.0))
            except (TypeError, ValueError):
                continue
            score = _signal_directional_score(sig_type, raw_score)
            family_accum.setdefault(symbol, {}).setdefault(family, []).append(score)

    family_means: dict[str, dict[str, float]] = {}
    for symbol, fam_map in family_accum.items():
        family_means[symbol] = {
            family: (sum(scores) / len(scores))
            for family, scores in fam_map.items()
            if scores
        }
    return family_means


def _aligned_family_count(family_scores: dict[str, float], direction: str) -> int:
    if direction == "LONG":
        return sum(1 for score in family_scores.values() if score >= _SA_FAMILY_MIN_SCORE)
    if direction == "SHORT":
        return sum(1 for score in family_scores.values() if score <= -_SA_FAMILY_MIN_SCORE)
    long_count = sum(1 for score in family_scores.values() if score >= _SA_FAMILY_MIN_SCORE)
    short_count = sum(1 for score in family_scores.values() if score <= -_SA_FAMILY_MIN_SCORE)
    return max(long_count, short_count)


def _get_forecast_scores(snaps: list[dict[str, Any]]) -> dict[str, float]:
    """Extract per-symbol price_forecast scores from parsed snapshots.

    Args:
        snaps: List of parsed Snapshot dicts.

    Returns:
        Mapping of symbol → highest absolute price_forecast score.
    """
    scores: dict[str, float] = {}
    for s in snaps:
        sym = s.get("symbol", "")
        for sig in s.get("signals", []):
            if sig.get("type") == "price_forecast":
                try:
                    val = float(sig.get("score", 0))
                    if sym not in scores or abs(val) > abs(scores[sym]):
                        scores[sym] = val
                except (TypeError, ValueError):
                    pass
    return scores


def _get_macro_risk_off_score(snaps: list[dict[str, Any]]) -> float:
    """Extract max macro_risk_off score from parsed snapshot signals.

    Used by the 1h macro veto gate.

    Args:
        snaps: List of parsed Snapshot dicts.

    Returns:
        Maximum absolute macro_risk_off score, or 0.0 if none found.
    """
    score = 0.0
    for s in snaps:
        for sig in s.get("signals", []):
            if sig.get("type") == "macro_risk_off":
                try:
                    score = max(score, abs(float(sig.get("score", 0))))
                except (TypeError, ValueError):
                    pass
    return score


def _is_macro_stale(snaps: list[dict[str, Any]]) -> bool:
    """Check if the most recent macro snapshot is older than the staleness threshold.

    Args:
        snaps: List of parsed Snapshot dicts.

    Returns:
        True if macro data is stale or absent.
    """
    now = datetime.now(timezone.utc)
    newest_macro_ts: datetime | None = None
    for s in snaps:
        for sig in s.get("signals", []):
            if sig.get("type") == "macro_risk_off":
                ts_str = s.get("timestamp", "")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if newest_macro_ts is None or ts > newest_macro_ts:
                        newest_macro_ts = ts
                except (ValueError, TypeError):
                    continue
    if newest_macro_ts is None:
        return True
    age = (now - newest_macro_ts).total_seconds()
    if age > _MACRO_STALE_SECONDS:
        logger.warning("Macro data is stale (%.0fs old, threshold=%ds)", age, _MACRO_STALE_SECONDS)
        return True
    return False


def _get_volume_spike_scores(snaps: list[dict[str, Any]]) -> dict[str, float]:
    """Extract per-symbol max volume_spike score from parsed snapshots.

    Args:
        snaps: List of parsed Snapshot dicts.

    Returns:
        Mapping of symbol → highest volume_spike score.
    """
    scores: dict[str, float] = {}
    for s in snaps:
        sym = s.get("symbol", "")
        for sig in s.get("signals", []):
            if sig.get("type") == "volume_spike":
                try:
                    val = float(sig.get("score", 0))
                    if sym not in scores or val > scores[sym]:
                        scores[sym] = val
                except (TypeError, ValueError):
                    pass
    return scores


def _get_reference_prices(snaps: list[dict[str, Any]]) -> dict[str, float]:
    """Extract per-symbol latest reference prices from snapshot signal raw payloads.

    Priority order (0 = highest):
      0) technical_trend  — live TradingView quote (most reliable real-time price)
      1) volume_spike     — live Polygon trade print
      2) options_flow     — Polygon/Alpaca options last price
      3) insider_activity — EDGAR trade execution price
      4) catalyst_event   — ROT/SpamShield reference price
      5) short_interest   — short interest data price
      6) price_forecast   — TimesFM model input feature (stale training price, lowest priority)
    """
    prices: dict[str, float] = {}
    candidates: dict[str, tuple[int, int, float]] = {}
    type_priority = {
        "technical_trend": 0,
        "volume_spike": 1,
        "options_flow": 2,
        "insider_activity": 3,
        "catalyst_event": 4,
        "short_interest": 5,
        "price_forecast": 6,
    }
    key_priority = {
        "current_price": 0,
        "last": 1,
        "last_price": 2,
        "price": 3,
        "close": 4,
    }

    for s in snaps:
        sym = s.get("symbol", "")
        if not sym:
            continue
        for sig in s.get("signals", []):
            raw = sig.get("raw") or {}
            if not isinstance(raw, dict):
                continue

            sig_type = str(sig.get("type", ""))
            sig_rank = type_priority.get(sig_type, 99)
            for k in ("current_price", "last", "last_price", "price", "close"):
                try:
                    v = float(raw.get(k, 0.0))
                    if v > 0:
                        key_rank = key_priority.get(k, 99)
                        existing = candidates.get(sym)
                        proposal = (sig_rank, key_rank, v)
                        if existing is None or proposal < existing:
                            candidates[sym] = proposal
                        break
                except (TypeError, ValueError):
                    continue

    for sym, (_, _, px) in candidates.items():
        prices[sym] = px
    return prices


def validate_and_filter(
    llm_response: Any,
    snapshots_json: str,
    macro: dict[str, Any],
    vix: float,
    timeframe: str,
    *,
    add_score_fn: Any | None = None,
    trace_id: str | None = None,
) -> tuple[list[PlaybookAlert], str]:
    """Validate LLM output and apply server-side gate filters.

    Parses the LLM JSON response, validates each item as a
    PlaybookAlert, and applies server-side gates while collecting
    all failed reasons per candidate:

    1. **VIX hard gate** (universal): VIX > 30 rejects LONG/SHORT
    2. **Symbol-hallucination check**: symbol must exist in snapshots
    3. **EP ceiling**: caps edge_probability by actual source count
    4. **Gate thresholds**: EP, sources_agree, confidence minimums
    5. **R:R gate**: reward ≥ 2.5× risk for LONG/SHORT
    6. **VIX + risk-off soft gate**: VIX > 25 suppresses weak longs
    7. **Macro veto** (1h): strong macro_risk_off discounts longs

    Args:
        llm_response: Raw JSON string from the LLM decision engine.
        snapshots_json: JSON string of merged Snapshot dicts.
        macro: Macro regime dict (must contain ``risk_on`` key).
        vix: Current VIX level (0.0 if unavailable).
        timeframe: Pipeline timeframe (``"15m"`` or ``"1h"``).
        add_score_fn: Optional Langfuse ``add_score`` callback.
        trace_id: Optional Langfuse trace ID for scoring.

    Returns:
        Tuple of (list of passing PlaybookAlerts, JSON string of alerts).
    """
    # ── Parse LLM JSON ───────────────────────────────────────────
    # Some providers return fenced markdown (```json ... ```) or wrap
    # JSON in explanatory text. Extract the array payload defensively.
    def _extract_json_array_text(payload: Any) -> str:
        if isinstance(payload, str):
            text = payload.strip()
        elif isinstance(payload, list):
            return json.dumps(payload)
        elif isinstance(payload, dict):
            content = payload.get("content")
            if isinstance(content, str):
                text = content.strip()
            else:
                return json.dumps(payload)
        else:
            text = str(payload or "").strip()

        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        if not text.startswith("["):
            start = text.find("[")
            end = text.rfind("]")
            if start != -1 and end > start:
                text = text[start : end + 1]

        return text

    parse_used_repair = False

    def _json_loads_with_repairs(text: str) -> Any:
        """Parse JSON with light deterministic repairs for common LLM artifacts."""
        nonlocal parse_used_repair
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Repair trailing commas before ] or }.
        repaired = re.sub(r",(\s*[\]}])", r"\1", text)
        parse_used_repair = True
        return json.loads(repaired)

    # ── Detect API-level errors before attempting JSON parse ─────
    # When the pipeline runner exhausts all retries, step_results["ensemble-decide"]
    # is set to None. Also, litellm wraps provider errors as strings like:
    # "litellm.InternalServerError: AnthropicError - {...overloaded_error...}"
    # Both cases are infrastructure failures, not prompt compliance issues.
    if llm_response is None or llm_response == "":
        logger.error("LLM response is None/empty — all retries exhausted (API overload or timeout)")
        if add_score_fn and trace_id:
            add_score_fn(trace_id, "llm_api_error", 1.0, comment="LLM returned None — all retries exhausted")
        return [], "[]"

    _llm_resp_str = str(llm_response)
    _API_ERROR_MARKERS = (
        "InternalServerError",
        "overloaded_error",
        "RateLimitError",
        "ServiceUnavailableError",
        "APIConnectionError",
        "APIStatusError",
        "AnthropicError",
    )
    if any(m in _llm_resp_str for m in _API_ERROR_MARKERS):
        logger.error("LLM API error detected (not a prompt compliance issue): %s", _llm_resp_str[:300])
        if add_score_fn and trace_id:
            add_score_fn(trace_id, "llm_api_error", 1.0, comment="LLM backend error — not a JSON compliance failure")
        return [], "[]"

    try:
        llm_json_text = _extract_json_array_text(llm_response)
        raw = _json_loads_with_repairs(llm_json_text)
        # Accept common wrapped shapes produced by LLMs, e.g.
        # {"alerts": [...]} or {"result": [...]}.
        if isinstance(raw, dict):
            for key in ("alerts", "result", "results", "data"):
                candidate = raw.get(key)
                if isinstance(candidate, list):
                    raw = candidate
                    break
        if not isinstance(raw, list):
            raise ValueError(f"Expected list, got {type(raw).__name__}")
    except Exception as e:
        logger.error("Decision engine JSON parse error: %s", e)
        logger.error("Raw response: %s", str(llm_response)[:500])
        if add_score_fn and trace_id:
            add_score_fn(trace_id, "llm_json_valid", 0.0, comment="JSON parse failed")
        return [], "[]"

    if add_score_fn and trace_id:
        add_score_fn(trace_id, "llm_json_valid", 1.0, comment="valid JSON array")
        add_score_fn(
            trace_id,
            "llm_json_repaired",
            1.0 if parse_used_repair else 0.0,
            comment="1 if lightweight parser repair was required",
        )

    # ── Parse snapshots once ──────────────────────────────────────
    parsed_snaps = _parse_snapshots(snapshots_json)

    # ── Build snapshot signal-type index ─────────────────────────
    snap_types = _build_snap_type_index(parsed_snaps)
    family_scores_index = _build_symbol_family_scores(parsed_snaps)

    # ── Build per-symbol forecast score index ────────────────────
    forecast_scores = _get_forecast_scores(parsed_snaps)
    # ── Build per-symbol volume spike index ───────────────────
    volume_scores = _get_volume_spike_scores(parsed_snaps)
    # ── Build per-symbol reference price index ────────────────
    ref_prices = _get_reference_prices(parsed_snaps)
    # Macro regime is sourced from the macro:regime Redis key (set by collector-macro),
    # NOT from per-symbol snapshot signals.  The __GLOBAL_MACRO__ snapshots are stripped
    # by merger.py before reaching here, so _get_macro_risk_off_score always returns 0.0.
    # Use the reliable macro dict + vix param for the 1h veto gate instead.
    risk_off = not macro.get("risk_on", True)
    base_ep_gate = _GATE_EP.get(timeframe, 0.70)
    base_sa_gate = _GATE_SA
    base_conf_gate = _GATE_CONF

    # ── VIX safety: treat NaN/Inf as conservative high-VIX ──────
    if not math.isfinite(vix):
        logger.warning("VIX value non-finite (%.4f), treating as 35.0", vix)
        vix = 35.0

    bulls, bears, trend_strength = _signal_surface(parsed_snaps)
    regime = _classify_regime(vix, risk_off, bulls, bears, trend_strength)
    ep_gate, sa_gate, conf_gate = _dynamic_gates(
        base_ep_gate,
        base_sa_gate,
        base_conf_gate,
        timeframe,
        regime,
    )
    ep_gate, sa_gate, conf_gate, market_session = _apply_market_session_gate_overlays(
        ep_gate,
        sa_gate,
        conf_gate,
        timeframe,
    )

    alerts: list[PlaybookAlert] = []
    candidates: list[PlaybookAlert] = []
    rejections: list[tuple[str, GateRejection]] = []
    directional_rejections: list[tuple[str, GateRejection]] = []
    watch_rejections: list[tuple[str, GateRejection]] = []

    for item in raw:
        try:
            alert = PlaybookAlert(**item)
        except Exception as e:
            logger.warning("PlaybookAlert validation failed for %s: %s", item, e)
            continue

        # ── Timeframe validation gate ─────────────────────────────
        alert_tf = getattr(alert, "timeframe", timeframe)
        if alert_tf not in _VALID_TIMEFRAMES:
            logger.warning(
                "Timeframe invalid: %s timeframe=%s not in %s",
                alert.symbol,
                alert_tf,
                _VALID_TIMEFRAMES,
            )
            rejections.append((alert.symbol, GateRejection.TIMEFRAME_INVALID))
            continue

        candidates.append(alert)

        reason_list: list[GateRejection] = []
        reason_set: set[GateRejection] = set()

        def _add_reason(reason: GateRejection) -> None:
            if reason not in reason_set:
                reason_set.add(reason)
                reason_list.append(reason)

        directional = alert.direction in ("LONG", "SHORT")

        # Deterministic server-side sources_agree from aligned independent families.
        actual_sources = len(snap_types.get(alert.symbol, set()))
        if actual_sources == 0:
            logger.warning("SYMBOL_HALLUCINATION: %s not present in merged snapshots", alert.symbol)
            if add_score_fn and trace_id:
                add_score_fn(
                    trace_id,
                    "symbol_hallucination",
                    1.0,
                    comment=f"{alert.symbol}: not in snapshot data",
                )
            _add_reason(GateRejection.SOURCE_HALLUCINATION)

        family_scores = dict(family_scores_index.get(alert.symbol, {}))
        # merger.py strips __GLOBAL_MACRO__ snapshots before this stage, so many
        # symbols have no "macro" family despite macro context being available.
        # Inject a deterministic macro family score from macro regime to avoid
        # structurally capping sources_agree below full family coverage.
        if _SA_INCLUDE_MACRO_CONTEXT and "macro" not in family_scores:
            family_scores["macro"] = -abs(_SA_MACRO_CONTEXT_SCORE) if risk_off else abs(_SA_MACRO_CONTEXT_SCORE)
        llm_sources_agree = alert.sources_agree
        deterministic_sources_agree = _aligned_family_count(family_scores, alert.direction)
        if _SA_FORECAST_CONFIRM_BONUS_ENABLED:
            trend_score = float(family_scores.get("trend", 0.0))
            fc_score = forecast_scores.get(alert.symbol)
            # Guard: only award the bonus when at least one non-price_forecast
            # signal exists in the trend family.  price_forecast is already a
            # trend family member (earns 1 SA vote); using it as both the
            # trend evidence and the confirming forecast would double-count a
            # single source.  technical_trend or relative_strength must also
            # be present to earn the bonus.
            sym_types = snap_types.get(alert.symbol, set())
            trend_has_non_fc = bool(sym_types & {"technical_trend", "relative_strength"})
            if fc_score is not None and trend_has_non_fc:
                if (
                    alert.direction == "LONG"
                    and trend_score >= _SA_FAMILY_MIN_SCORE
                    and fc_score >= _SA_FORECAST_BONUS_THRESHOLD
                ):
                    deterministic_sources_agree += 1
                elif (
                    alert.direction == "SHORT"
                    and trend_score <= -_SA_FAMILY_MIN_SCORE
                    and fc_score <= -_SA_FORECAST_BONUS_THRESHOLD
                ):
                    deterministic_sources_agree += 1
        deterministic_sources_agree = min(deterministic_sources_agree, 7)
        if llm_sources_agree != deterministic_sources_agree:
            logger.info(
                "sources_agree server override: %s llm=%d server=%d",
                alert.symbol,
                llm_sources_agree,
                deterministic_sources_agree,
            )
            if add_score_fn and trace_id:
                add_score_fn(
                    trace_id,
                    "sources_agree_override",
                    float(abs(llm_sources_agree - deterministic_sources_agree)),
                    comment=f"{alert.symbol}: llm {llm_sources_agree}, server {deterministic_sources_agree}",
                )
        alert.sources_agree = deterministic_sources_agree

        # Cap edge_probability by evidence depth (actual distinct types in snapshot).
        ceiling = 0.50 if actual_sources == 0 else EP_CEILING.get(min(actual_sources, 11), 0.99)
        if alert.edge_probability > ceiling:
            original_ep = alert.edge_probability
            alert.edge_probability = ceiling
            logger.warning(
                "EP_CAPPED_%d_SOURCES: %s EP=%.2f capped to %.2f (sources=%d)",
                actual_sources,
                alert.symbol,
                original_ep,
                ceiling,
                actual_sources,
            )

        # ── Entry-order validation (Gate 0) ──────────────────────
        if directional:
            _price_invalid = False
            for _pk in ("stop", "level", "target"):
                if alert.entry[_pk] <= 0:
                    logger.warning(
                        "Invalid price: %s %s %s=%.4f (must be > 0)",
                        alert.symbol,
                        alert.direction,
                        _pk,
                        alert.entry[_pk],
                    )
                    _price_invalid = True
                    break
            if _price_invalid:
                _add_reason(GateRejection.ENTRY_ORDER_INVALID)

        if alert.direction == "LONG":
            if not (alert.entry["stop"] < alert.entry["level"] < alert.entry["target"]):
                logger.warning(
                    "Entry order invalid: %s LONG stop=%.2f level=%.2f target=%.2f",
                    alert.symbol,
                    alert.entry["stop"],
                    alert.entry["level"],
                    alert.entry["target"],
                )
                _add_reason(GateRejection.ENTRY_ORDER_INVALID)
        elif alert.direction == "SHORT":
            if not (alert.entry["target"] < alert.entry["level"] < alert.entry["stop"]):
                logger.warning(
                    "Entry order invalid: %s SHORT target=%.2f level=%.2f stop=%.2f",
                    alert.symbol,
                    alert.entry["target"],
                    alert.entry["level"],
                    alert.entry["stop"],
                )
                _add_reason(GateRejection.ENTRY_ORDER_INVALID)

        # ── Entry-vs-market drift gate ─────────────────────────
        # Reject LONG/SHORT alerts whose proposed entry is too far
        # from latest reference price from snapshots.
        if directional and GateRejection.ENTRY_ORDER_INVALID not in reason_set:
            ref_price = ref_prices.get(alert.symbol)
            if ref_price and ref_price > 0:
                drift_pct = abs(alert.entry["level"] - ref_price) / ref_price
                drift_gate = _ENTRY_MARKET_DRIFT_MAX_PCT
                if vix >= _VIX_SOFT_THRESHOLD:
                    drift_gate += _ENTRY_MARKET_DRIFT_VIX_BUMP
                if vix >= _ENTRY_MARKET_DRIFT_VIX_HIGH_THRESHOLD:
                    drift_gate += _ENTRY_MARKET_DRIFT_VIX_HIGH_BUMP
                if market_session in {"pre", "after"}:
                    drift_gate += _ENTRY_MARKET_DRIFT_PREPOST_BUMP
                drift_gate = min(drift_gate, _ENTRY_MARKET_DRIFT_CAP_PCT)
                if drift_pct > drift_gate:
                    logger.info(
                        "Entry drift filtered: %s %s entry=%.2f ref=%.2f drift=%.1f%% > max=%.1f%%",
                        alert.symbol,
                        alert.direction,
                        alert.entry["level"],
                        ref_price,
                        drift_pct * 100.0,
                        drift_gate * 100.0,
                    )
                    _add_reason(GateRejection.ENTRY_MARKET_DRIFT)

        # ── Market-session server-side gate ─────────────────────
        if directional and market_session == "closed" and _MARKET_HOURS_GATES_ENABLED:
            logger.info(
                "Market-session gate: %s %s rejected (session=%s)",
                alert.symbol,
                alert.direction,
                market_session,
            )
            _add_reason(GateRejection.MARKET_SESSION_CLOSED)

        # ── VIX universal hard gate ──────────────────────────────
        # When VIX > 30 the market is in extreme stress.  Reject ALL
        # directional signals (LONG/SHORT) regardless of risk_on flag
        # or timeframe.  WATCH alerts are allowed through.
        if vix > 30.0 and directional:
            logger.warning(
                "VIX hard gate: %s %s rejected (VIX=%.1f > 30.0)",
                alert.symbol,
                alert.direction,
                vix,
            )
            _add_reason(GateRejection.VIX_HARD)

        # ── Gate thresholds (per-timeframe) ──────────────────────
        if alert.direction == "WATCH":
            watch_ep_gate = max(ep_gate - _WATCH_EP_DELTA, 0.50)
            if alert.edge_probability < watch_ep_gate:
                logger.info(
                    "WATCH filtered (EP): %s ep=%.2f < watch_gate=%.2f",
                    alert.symbol,
                    alert.edge_probability,
                    watch_ep_gate,
                )
                _add_reason(GateRejection.WATCH_EP_THRESHOLD)
            # WATCH SA and CONF scale with regime-adjusted directional gates,
            # staying 1 SA family and 0.10 conf below the directional bar to
            # preserve a consistent gap across sessions and regime overlays.
            watch_sa_gate = max(_WATCH_SA_MIN, sa_gate - 1)
            watch_conf_gate = max(_WATCH_CONF_MIN, conf_gate - 0.10)
            if alert.sources_agree < watch_sa_gate:
                logger.info(
                    "WATCH filtered (SA): %s sa=%d < watch_gate=%d",
                    alert.symbol,
                    alert.sources_agree,
                    watch_sa_gate,
                )
                _add_reason(GateRejection.WATCH_SA_THRESHOLD)
            if alert.confidence < watch_conf_gate:
                logger.info(
                    "WATCH filtered (CONF): %s conf=%.2f < watch_gate=%.2f",
                    alert.symbol,
                    alert.confidence,
                    watch_conf_gate,
                )
                _add_reason(GateRejection.WATCH_CONF_THRESHOLD)
        else:
            if alert.edge_probability < ep_gate:
                logger.info(
                    "Alert filtered (EP): %s ep=%.2f < gate=%.2f",
                    alert.symbol,
                    alert.edge_probability,
                    ep_gate,
                )
                _add_reason(GateRejection.EP_THRESHOLD)
            if alert.sources_agree < sa_gate:
                logger.info(
                    "Alert filtered (SA): %s sa=%d < gate=%d",
                    alert.symbol,
                    alert.sources_agree,
                    sa_gate,
                )
                _add_reason(GateRejection.SA_THRESHOLD)
            if (
                alert.confidence >= _HIGH_CONFIDENCE_MIN
                and alert.sources_agree < _HIGH_CONFIDENCE_MIN_SA
            ):
                logger.info(
                    "Alert filtered (HIGH_CONFIDENCE_ALIGNMENT): %s conf=%.2f >= %.2f but sa=%d < %d",
                    alert.symbol,
                    alert.confidence,
                    _HIGH_CONFIDENCE_MIN,
                    alert.sources_agree,
                    _HIGH_CONFIDENCE_MIN_SA,
                )
                _add_reason(GateRejection.HIGH_CONFIDENCE_ALIGNMENT)
            if alert.confidence < conf_gate:
                logger.info(
                    "Alert filtered (CONF): %s conf=%.2f < gate=%.2f",
                    alert.symbol,
                    alert.confidence,
                    conf_gate,
                )
                _add_reason(GateRejection.CONF_THRESHOLD)

        # ── R:R gate: reward must be ≥ N× risk (timeframe-tiered) ─
        # 15m: 2.0:1 minimum (break-even at 33% win-rate)
        # 1h:  2.5:1 minimum (break-even at 29% win-rate, longer holds need better payoff)
        risk = abs(alert.entry["level"] - alert.entry["stop"])
        reward = abs(alert.entry["target"] - alert.entry["level"])
        if directional and GateRejection.ENTRY_ORDER_INVALID not in reason_set:
            if risk == 0:
                logger.warning(
                    "R:R zero-risk rejected: %s stop==level (%.2f)",
                    alert.symbol,
                    alert.entry["level"],
                )
                _add_reason(GateRejection.RR_ZERO_RISK)
            # Reject unrealistic risk: stop so close to entry that R:R
            # is meaningless.  Use price-normalized floor to handle
            # penny stocks ($0.05 min) and higher-priced equities.
            micro_risk_floor = max(alert.entry["level"] * 0.001, 0.05)
            if risk < micro_risk_floor:
                logger.warning(
                    "R:R micro-risk rejected: %s risk=%.4f < floor=%.4f (level=%.2f)",
                    alert.symbol,
                    risk,
                    micro_risk_floor,
                    alert.entry["level"],
                )
                _add_reason(GateRejection.RR_ZERO_RISK)
            _rr_min = _GATE_RR.get(timeframe, 2.0)
            if risk > 0 and reward / risk < _rr_min:
                logger.info(
                    "Alert filtered: %s R:R %.2f:1 below %.1f:1 minimum (%s)",
                    alert.symbol,
                    reward / risk,
                    _rr_min,
                    timeframe,
                )
                _add_reason(GateRejection.RR_MINIMUM)

        # ── Forecast contradiction gate ──────────────────────────
        # If a price_forecast signal exists for this symbol and
        # contradicts the alert direction, reject.
        # Non-blocking: if no forecast signal → pass through.
        # High-conviction bypass: SA >= 5 AND EP >= 0.85.
        if directional:
            _fc_score = forecast_scores.get(alert.symbol)
            if _fc_score is not None:
                _fc_contradicts = False
                if alert.direction == "LONG" and _fc_score < -_FORECAST_GATE_SCORE_THRESHOLD:
                    _fc_contradicts = True
                elif alert.direction == "SHORT" and _fc_score > _FORECAST_GATE_SCORE_THRESHOLD:
                    _fc_contradicts = True

                _fc_high_conviction = (
                    alert.sources_agree >= _FORECAST_GATE_SA and alert.edge_probability >= _FORECAST_GATE_EP
                )
                if _fc_contradicts and not _fc_high_conviction:
                    logger.info(
                        "Forecast contradiction: %s %s rejected "
                        "(forecast_score=%.2f, threshold=%.2f, sa=%d, ep=%.2f)",
                        alert.symbol,
                        alert.direction,
                        _fc_score,
                        _FORECAST_GATE_SCORE_THRESHOLD,
                        alert.sources_agree,
                        alert.edge_probability,
                    )
                    _add_reason(GateRejection.FORECAST_CONTRADICTS)

        # ── 1h macro veto: risk-off regime blocks longs ─────────
        # Uses macro regime dict (risk_on = False) + VIX ≥ 25 as the trigger.
        # This is reliable because macro:regime is always populated from
        # collector-macro via the Redis key, independent of per-symbol snapshots.
        # High-conviction bypass: SA >= 6 AND EP >= 0.90 overrides macro veto.
        _macro_high_conviction = (
            alert.sources_agree >= _MACRO_VETO_SA and alert.edge_probability >= _MACRO_VETO_EP
        )
        if (
            timeframe == "1h"
            and alert.direction == "LONG"
            and risk_off
            and vix >= 25.0
            and not _macro_high_conviction
        ):
            logger.info(
                "Macro veto: %s LONG rejected (risk_off=True, VIX=%.1f)",
                alert.symbol,
                vix,
            )
            _add_reason(GateRejection.MACRO_VETO)

        # ── VIX + risk-off soft gate (both timeframes) ───────────
        # Suppress weak LONGs when VIX elevated + risk-off.
        # Suppress weak SHORTs when VIX elevated + risk-on  (short
        # squeezes are common in volatile risk-on rebounds).
        # High-conviction setups (SA >= 4 AND EP >= 0.80) pass even
        # in elevated-VIX environments.
        # Exception: when regime == "risk_off_high_vix", the dynamic
        # gate overlay (Step 1) already raised EP/SA/conf thresholds.
        # An alert that survived those elevated gates must not be
        # binary-rejected here too — that would be double-penalising.
        _high_conviction = alert.sources_agree >= _VIX_SOFT_SA and alert.edge_probability >= _VIX_SOFT_EP
        _vix_soft_triggered = False
        if vix > _VIX_SOFT_THRESHOLD and not _high_conviction:
            if risk_off and alert.direction == "LONG" and regime != "risk_off_high_vix":
                _vix_soft_triggered = True
            elif not risk_off and alert.direction == "SHORT":
                _vix_soft_triggered = True
        if _vix_soft_triggered:
            # VIX soft gate: elevated volatility + directional mismatch.
            # These are borderline — log with REVIEW flag for manual inspection.
            logger.info(
                "VIX gate [REVIEW]: %s %s suppressed (VIX=%.1f, risk_off=%s, sa=%d, ep=%.2f)",
                alert.symbol,
                alert.direction,
                vix,
                risk_off,
                alert.sources_agree,
                alert.edge_probability,
            )
            _add_reason(GateRejection.VIX_SOFT)

        # ── Volume confirmation gate ───────────────────────────────
        # Directional alerts without volume confirmation get a confidence
        # downgrade — thin-volume breakouts are unreliable.
        # In choppy / risk_off_high_vix regimes the penalty is doubled:
        # false breakouts are significantly more common in those conditions.
        if directional:
            _vol_score = volume_scores.get(alert.symbol, 0.0)
            if _vol_score < _VOLUME_CONFIRM_SCORE:
                _vol_penalty = (
                    _VOLUME_CONFIRM_PENALTY_CHOPPY
                    if regime in ("choppy", "risk_off_high_vix")
                    else _VOLUME_CONFIRM_PENALTY
                )
                alert.confidence = max(alert.confidence - _vol_penalty, 0.0)
                # Re-check confidence gate after downgrade
                if alert.confidence < conf_gate:
                    logger.info(
                        "Volume unconfirmed: %s %s (vol_score=%.2f < %.2f) "
                        "conf downgraded by %.2f to %.2f < gate %.2f (regime=%s)",
                        alert.symbol,
                        alert.direction,
                        _vol_score,
                        _VOLUME_CONFIRM_SCORE,
                        _vol_penalty,
                        alert.confidence,
                        conf_gate,
                        regime,
                    )
                    _add_reason(GateRejection.VOLUME_UNCONFIRMED)

        if reason_list:
            rejected_rows = [(alert.symbol, reason) for reason in reason_list]
            rejections.extend(rejected_rows)
            if directional:
                directional_rejections.extend(rejected_rows)
            else:
                watch_rejections.extend(rejected_rows)
        else:
            alerts.append(alert)

    # Keep WATCH output intentionally limited:
    # - If directional alerts exist, drop all WATCH alerts for this run.
    # - Otherwise rank WATCH candidates by composite score (ep × conf),
    #   boosted for setups with improving EP across cycles, then apply
    #   stale-cycle decay filter and cap by regime via _watch_max_for_regime.
    directional_alerts = [a for a in alerts if a.direction in ("LONG", "SHORT")]
    watch_alerts = [a for a in alerts if a.direction == "WATCH"]

    # Pre-fetch previous Redis state for all WATCH candidates before any
    # filtering so prev_states is available for sort, decay, and promotion.
    prev_states: dict[str, dict[str, str] | None] = {
        w.symbol: _get_watch_prev_state(w.symbol, timeframe) for w in watch_alerts
    }

    if directional_alerts and watch_alerts:
        for w in watch_alerts:
            row = (w.symbol, GateRejection.WATCH_DROPPED_DIRECTIONAL_PRESENT)
            rejections.append(row)
            watch_rejections.append(row)
        watch_alerts = []
        alerts = directional_alerts
    else:
        # Sort by composite quality score (ep × conf), with a bonus multiplier
        # applied to setups whose EP has improved since the previous cycle.
        # This promotes strengthening setups to the top of the ranked queue.
        watch_alerts.sort(
            key=lambda a: a.edge_probability * a.confidence * (
                _WATCH_PROMOTION_BONUS_MULT
                if _watch_is_improving(a.symbol, a.edge_probability, prev_states)
                else 1.0
            ),
            reverse=True,
        )

        # ── Log full ranked WATCH queue for observability ──────────
        if watch_alerts:
            ranked_lines = " | ".join(
                f"#{i + 1} {a.symbol} ep={a.edge_probability:.2f} "
                f"conf={a.confidence:.2f} score={a.edge_probability * a.confidence:.3f}"
                f"{'↑' if _watch_is_improving(a.symbol, a.edge_probability, prev_states) else ''}"
                for i, a in enumerate(watch_alerts)
            )
            logger.info(
                "Decision-%s WATCH ranked queue (%d): %s",
                timeframe,
                len(watch_alerts),
                ranked_lines,
            )

        # ── Stale-cycle decay filter ───────────────────────────────
        # Drop any WATCH that has been kept unresolved for >= N cycles
        # so stale setups do not crowd out fresh borderline candidates.
        decay_kept: list[PlaybookAlert] = []
        for w in watch_alerts:
            cycles = _get_watch_cycles(w.symbol, timeframe)
            if cycles >= _WATCH_DECAY_CYCLES:
                logger.info(
                    "WATCH_DECAY: %s stale across %d cycles "
                    "(ep=%.2f conf=%.2f) – dropping",
                    w.symbol,
                    cycles,
                    w.edge_probability,
                    w.confidence,
                )
                row = (w.symbol, GateRejection.WATCH_DECAY)
                rejections.append(row)
                watch_rejections.append(row)
            else:
                decay_kept.append(w)
        watch_alerts = decay_kept

        # ── Cap by regime via _watch_max_for_regime ────────────────
        # Stressed regimes (extreme, risk_off_high_vix) → 1 WATCH max.
        # Neutral/choppy regimes → 2.  Clear trending regimes → 3.
        # All limits are env-var overridable.
        _effective_watch_max = _watch_max_for_regime(regime)
        if len(watch_alerts) > _effective_watch_max:
            for w in watch_alerts[_effective_watch_max:]:
                row = (w.symbol, GateRejection.WATCH_CAP)
                rejections.append(row)
                watch_rejections.append(row)
            watch_alerts = watch_alerts[:_effective_watch_max]

        alerts = directional_alerts + watch_alerts

    # ── Update Redis WATCH-cycle state ────────────────────────────
    # Increment cycle count for each kept WATCH alert, then check for
    # strengthening setups to prefix the thesis with [↑ STRENGTHENING ×N].
    for w in [a for a in alerts if a.direction == "WATCH"]:
        new_cycles = _incr_watch_cycles(w.symbol, timeframe, w.edge_probability, w.confidence)
        logger.debug("WATCH_CYCLE_INCR: %s cycles=%d", w.symbol, new_cycles)
        if (
            new_cycles >= _WATCH_PROMOTION_MIN_CYCLES
            and _watch_is_improving(w.symbol, w.edge_probability, prev_states)
        ):
            w.thesis = f"[\u2191 STRENGTHENING \u00d7{new_cycles}] {w.thesis}"
            logger.info(
                "WATCH_PROMOTION: %s strengthening across %d cycles (ep=%.2f prev_ep=%s)",
                w.symbol,
                new_cycles,
                w.edge_probability,
                (prev_states.get(w.symbol) or {}).get("last_ep", "n/a"),
            )
    # Reset cycle state for symbols that graduated to a directional alert.
    directional_symbols = [a.symbol for a in alerts if a.direction in ("LONG", "SHORT")]
    if directional_symbols:
        _reset_watch_cycles(directional_symbols, timeframe)
        logger.debug("WATCH_CYCLE_RESET: %s", ", ".join(directional_symbols))

    pre_dist = _candidate_distribution(candidates)
    post_dist = _candidate_distribution(alerts)

    logger.info(
        "Decision-%s gate summary: llm_candidates=%d parsed_candidates=%d passed_total=%d "
        "passed_directional=%d passed_watch=%d rejected_total=%d rejected_directional=%d rejected_watch=%d "
        "regime=%s market_session=%s trend_strength=%.2f breadth=%d/%d "
        "ep_gate=%.2f(base=%.2f) sa_gate=%d(base=%d) conf_gate=%.2f(base=%.2f)",
        timeframe,
        len(raw),
        len(candidates),
        len(alerts),
        len(directional_alerts),
        len(watch_alerts),
        len(rejections),
        len(directional_rejections),
        len(watch_rejections),
        regime,
        market_session,
        trend_strength,
        bulls,
        bears,
        ep_gate,
        base_ep_gate,
        sa_gate,
        base_sa_gate,
        conf_gate,
        base_conf_gate,
    )
    logger.info(
        "Decision-%s candidate quality pre-gates: median_ep=%.2f median_conf=%.2f median_rr=%.2f median_sa=%.1f",
        timeframe,
        pre_dist["median_ep"],
        pre_dist["median_conf"],
        pre_dist["median_rr"],
        pre_dist["median_sa"],
    )
    logger.info(
        "Decision-%s candidate quality post-gates: median_ep=%.2f median_conf=%.2f median_rr=%.2f median_sa=%.1f",
        timeframe,
        post_dist["median_ep"],
        post_dist["median_conf"],
        post_dist["median_rr"],
        post_dist["median_sa"],
    )
    logger.info("Decision-%s: %d alerts passed gates", timeframe, len(alerts))

    # ── Log sample rejections per gate for debugging ──────────────
    gate_samples: dict[str, list[str]] = {}
    for sym, gate in rejections:
        gate_samples.setdefault(gate.value, []).append(sym)
    for gate_name, symbols in gate_samples.items():
        sample = symbols[:3]  # cap at 3 examples per gate
        logger.info(
            "Gate %s rejected %d alerts (sample: %s)",
            gate_name,
            len(symbols),
            ", ".join(sample),
        )
    if gate_samples:
        logger.info(
            "Decision-%s rejection counts: %s",
            timeframe,
            ", ".join(
                f"{gate_name}={len(symbols)}" for gate_name, symbols in sorted(gate_samples.items())
            ),
        )

    if len(alerts) == 0:
        if len(raw) == 0:
            no_alert_reason = "llm_zero_candidates"
        elif len(candidates) == 0:
            no_alert_reason = "all_candidates_invalid"
        elif gate_samples:
            top_gate = sorted(gate_samples.items(), key=lambda kv: (-len(kv[1]), kv[0]))[0]
            no_alert_reason = f"gate_filtered:{top_gate[0]}"
        else:
            no_alert_reason = "no_actionable_candidates"
        logger.info(
            "Decision-%s no-alert summary: reason=%s parsed_candidates=%d llm_candidates=%d",
            timeframe,
            no_alert_reason,
            len(candidates),
            len(raw),
        )

    watch_kept = sum(1 for a in alerts if a.direction == "WATCH")
    _record_session_gate_metrics(
        timeframe,
        len(raw),
        len(directional_alerts),
        watch_kept,
        directional_rejections,
        watch_rejections,
    )

    # ── Structured gate telemetry ─────────────────────────────────
    if add_score_fn and trace_id:
        total = max(len(raw), 1)
        pass_rate = len(alerts) / total
        rejection_rate = len(rejections) / total
        add_score_fn(
            trace_id,
            "alert_pass_rate",
            pass_rate,
            comment=f"{len(alerts)}/{len(raw)} passed gates",
        )
        add_score_fn(
            trace_id,
            "alerts_fired",
            float(len(alerts)),
            comment=f"{len(alerts)} alerts",
        )
        add_score_fn(
            trace_id,
            "candidate_median_ep_pre",
            pre_dist["median_ep"],
            comment="median edge_probability before gates",
        )
        add_score_fn(
            trace_id,
            "candidate_median_conf_pre",
            pre_dist["median_conf"],
            comment="median confidence before gates",
        )
        add_score_fn(
            trace_id,
            "candidate_median_rr_pre",
            pre_dist["median_rr"],
            comment="median R:R before gates",
        )
        add_score_fn(
            trace_id,
            "candidate_median_ep_post",
            post_dist["median_ep"],
            comment="median edge_probability after gates",
        )
        add_score_fn(
            trace_id,
            "candidate_median_conf_post",
            post_dist["median_conf"],
            comment="median confidence after gates",
        )
        add_score_fn(
            trace_id,
            "candidate_median_rr_post",
            post_dist["median_rr"],
            comment="median R:R after gates",
        )
        add_score_fn(
            trace_id,
            "gate_rejection_rate",
            rejection_rate,
            comment=f"{len(rejections)}/{len(raw)} rejected",
        )
        if rejection_rate > 0.9 and len(raw) >= 3:
            logger.warning(
                "Gate rejection rate %.0f%% (%d/%d) exceeds 90%% threshold — "
                "LLM output quality may have degraded",
                rejection_rate * 100,
                len(rejections),
                len(raw),
            )
        # Per-gate rejection counts for observability
        gate_counts: dict[str, int] = {}
        for _sym, gate in rejections:
            gate_counts[gate.value] = gate_counts.get(gate.value, 0) + 1
        for gate_name, count in gate_counts.items():
            add_score_fn(
                trace_id,
                f"gate_reject_{gate_name}",
                float(count),
                comment=f"{count} alerts rejected by {gate_name}",
            )

    # Prometheus gate counters (always emitted, independent of Langfuse).
    _gate_counts: dict[str, int] = {}
    for _sym, _gate in rejections:
        _gate_counts[_gate.value] = _gate_counts.get(_gate.value, 0) + 1
    for _gate_name, _count in _gate_counts.items():
        GATE_REJECTIONS.labels(gate=_gate_name).inc(_count)

    ALERTS_PER_CYCLE.labels(timeframe=timeframe).observe(len(alerts))
    alerts_json = json.dumps([a.model_dump() for a in alerts])
    return alerts, alerts_json
