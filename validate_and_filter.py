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

import json
import logging
import math
import os
import time
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from constants import MACRO_STALE_SECONDS as _MACRO_STALE_SECONDS
from constants import get_market_hours_status
from metrics import (
    GATE_REJECTIONS,
    REDIS_CIRCUIT_OPEN,
    WATCH_DECAY_SKIPPED,
)
from models import PlaybookAlert
from redis_client import get_redis

from gate_config import (
    EXTENDED_HOURS_ALERTS_ENABLED,
    EXTENDED_HOURS_CONFIDENCE_PENALTY,
    GATE_CONF,
    GATE_EP,
    GATE_RR,
    GATE_SA,
    classify_regime,
)
from gates.dedup import _dedup_key, _reset_dedup_keys, _try_dedup_set
from gates.regime import (
    EP_CEILING,
    _dynamic_gates,
    _load_ep_ceiling,
    _signal_surface,
)
from gates.rr_volume import (
    _candidate_distribution,
    _get_forecast_scores,
    _get_macro_risk_off_score,
    _get_reference_prices,
    _get_volume_spike_scores,
    _is_macro_stale,
    _median,
    _rr,
)
from gates.session import (
    _apply_market_session_gate_overlays,
    _market_session_bucket,
    _record_session_gate_metrics,
    _session_stats_key,
)
from gates.watch import (
    _get_watch_cycles,
    _get_watch_prev_state,
    _incr_watch_cycles,
    _reset_watch_cycles,
    _watch_decay_key,
    _watch_is_improving,
    _watch_max_for_regime,
)
from gate_telemetry import (
    log_decision_gate_summary,
    record_langfuse_gate_scores,
    record_prometheus_gate_metrics,
)
from llm_response_parser import parse_llm_alerts

_classify_regime = classify_regime  # backward-compatible alias for tests

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
    DEDUP_SUPPRESSED = "dedup_suppressed"


# Valid alert timeframes
_VALID_TIMEFRAMES = {"5m", "15m", "1h", "4h", "1D"}

# Re-export gate thresholds from gate_config SSOT (backward compatible names).
_GATE_EP: dict[str, float] = GATE_EP
_GATE_SA: int = GATE_SA
_GATE_CONF: float = GATE_CONF
_GATE_RR: dict[str, float] = GATE_RR
_HIGH_CONFIDENCE_MIN: float = float(os.environ.get("HIGH_CONFIDENCE_MIN", "0.85"))
_HIGH_CONFIDENCE_MIN_SA: int = int(os.environ.get("HIGH_CONFIDENCE_MIN_SA", "5"))

# Per-timeframe R:R minimums (reward must be >= N × risk to be actionable).
# 15m setups are shorter-lived so 2:1 is sufficient; 1h setups warrant 2.5:1.
# Values loaded from gate_config.GATE_RR above.


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


# Macro veto bypass thresholds (configurable)
_MACRO_VETO_SA: int = int(os.environ.get("MACRO_VETO_SA", "6"))
_MACRO_VETO_EP: float = float(os.environ.get("MACRO_VETO_EP", "0.90"))

# VIX soft-gate bypass thresholds (configurable)
_VIX_SOFT_THRESHOLD: float = float(os.environ.get("VIX_SOFT_THRESHOLD", "25.0"))
_VIX_SOFT_SA: int = int(os.environ.get("VIX_SOFT_SA", "3"))
_VIX_SOFT_EP: float = float(os.environ.get("VIX_SOFT_EP", "0.72"))

# Limited WATCH policy (borderline-only, conservative)
_WATCH_MAX_PER_RUN: int = int(os.environ.get("WATCH_MAX_PER_RUN", "1"))
_WATCH_SA_MIN: int = int(os.environ.get("WATCH_SA_MIN", "2"))
_WATCH_CONF_MIN: float = float(os.environ.get("WATCH_CONF_MIN", "0.60"))
_WATCH_EP_DELTA: float = float(os.environ.get("WATCH_EP_DELTA", "0.05"))
_WATCH_DECAY_CYCLES: int = int(os.environ.get("WATCH_DECAY_CYCLES", "4"))
_WATCH_MAX_STRESSED: int = int(os.environ.get("WATCH_MAX_STRESSED", str(_WATCH_MAX_PER_RUN)))
_WATCH_MAX_NEUTRAL: int = int(os.environ.get("WATCH_MAX_NEUTRAL", "2"))
_WATCH_MAX_TRENDING: int = int(os.environ.get("WATCH_MAX_TRENDING", "3"))
_WATCH_PROMOTION_BONUS_MULT: float = float(os.environ.get("WATCH_PROMOTION_BONUS_MULT", "1.15"))
_WATCH_PROMOTION_MIN_CYCLES: int = int(os.environ.get("WATCH_PROMOTION_MIN_CYCLES", "2"))

# Forecast contradiction gate thresholds (configurable)
_FORECAST_GATE_SCORE_THRESHOLD: float = float(os.environ.get("FORECAST_GATE_SCORE_THRESHOLD", "0.8"))
_FORECAST_GATE_SA: int = int(os.environ.get("FORECAST_GATE_SA", "5"))
_FORECAST_GATE_EP: float = float(os.environ.get("FORECAST_GATE_EP", "0.85"))

# Volume confirmation penalties
_VOLUME_CONFIRM_SCORE: float = float(os.environ.get("VOLUME_CONFIRM_SCORE", "1.5"))
_VOLUME_CONFIRM_PENALTY: float = float(os.environ.get("VOLUME_CONFIRM_PENALTY", "0.05"))
_VOLUME_CONFIRM_PENALTY_CHOPPY: float = float(os.environ.get("VOLUME_CONFIRM_PENALTY_CHOPPY", "0.10"))

# Entry market drift gates
_ENTRY_MARKET_DRIFT_MAX_PCT: float = float(os.environ.get("ENTRY_MARKET_DRIFT_MAX_PCT", "0.03"))
_ENTRY_MARKET_DRIFT_VIX_BUMP: float = float(os.environ.get("ENTRY_MARKET_DRIFT_VIX_BUMP", "0.01"))
_ENTRY_MARKET_DRIFT_PREPOST_BUMP: float = float(os.environ.get("ENTRY_MARKET_DRIFT_PREPOST_BUMP", "0.01"))
_ENTRY_MARKET_DRIFT_CAP_PCT: float = float(os.environ.get("ENTRY_MARKET_DRIFT_CAP_PCT", "0.08"))
_ENTRY_MARKET_DRIFT_VIX_HIGH_THRESHOLD: float = float(
    os.environ.get("ENTRY_MARKET_DRIFT_VIX_HIGH_THRESHOLD", "30.0")
)
_ENTRY_MARKET_DRIFT_VIX_HIGH_BUMP: float = float(os.environ.get("ENTRY_MARKET_DRIFT_VIX_HIGH_BUMP", "0.02"))

# Server-side market-session gating controls (also in gates.session)
_MARKET_HOURS_GATES_ENABLED: bool = os.environ.get("MARKET_HOURS_GATES_ENABLED", "1") == "1"
_SESSION_PREPOST_EP_BUMP: float = float(os.environ.get("SESSION_PREPOST_EP_BUMP", "0.03"))
_SESSION_PREPOST_CONF_BUMP: float = float(os.environ.get("SESSION_PREPOST_CONF_BUMP", "0.05"))
_SESSION_PREPOST_SA_BUMP: int = int(os.environ.get("SESSION_PREPOST_SA_BUMP", "2"))

# Dynamic gate controls (regime overlays — patched by tests via this module)
_DYNAMIC_GATES_ENABLED: bool = os.environ.get("DYNAMIC_GATES_ENABLED", "1") == "1"


# Redis circuit breaker for WATCH decay path
_REDIS_FAILURE_COUNT = 0
_REDIS_FAILURE_THRESHOLD = int(os.environ.get("REDIS_FAILURE_THRESHOLD", "3"))
_REDIS_FAILURE_WINDOW_SECONDS = int(os.environ.get("REDIS_FAILURE_WINDOW_SECONDS", "60"))
_redis_last_failure_ts: float = 0.0
_redis_circuit_open: bool = False
_redis_circuit_warned_this_cycle: bool = False

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


def _check_redis_circuit() -> bool:
    """Return True when the circuit is open (skip Redis calls).

    Lazy-resets the circuit after ``_REDIS_FAILURE_WINDOW_SECONDS`` with no
    new failures.
    """
    global _redis_circuit_open, _REDIS_FAILURE_COUNT  # noqa: PLW0603

    now = time.monotonic()
    if _redis_circuit_open and (now - _redis_last_failure_ts) >= _REDIS_FAILURE_WINDOW_SECONDS:
        _redis_circuit_open = False
        _REDIS_FAILURE_COUNT = 0
        REDIS_CIRCUIT_OPEN.set(0)
        logger.info("Redis circuit breaker reset — WATCH decay re-enabled")

    return _redis_circuit_open


def _record_redis_failure() -> None:
    """Increment failure counter and open circuit if threshold exceeded."""
    global _redis_circuit_open, _REDIS_FAILURE_COUNT, _redis_last_failure_ts  # noqa: PLW0603

    now = time.monotonic()
    if _redis_last_failure_ts and (now - _redis_last_failure_ts) >= _REDIS_FAILURE_WINDOW_SECONDS:
        _REDIS_FAILURE_COUNT = 0

    _REDIS_FAILURE_COUNT += 1
    _redis_last_failure_ts = now

    if _REDIS_FAILURE_COUNT >= _REDIS_FAILURE_THRESHOLD and not _redis_circuit_open:
        _redis_circuit_open = True
        REDIS_CIRCUIT_OPEN.set(1)
        GATE_REJECTIONS.labels(gate="redis_circuit_open").inc()
        logger.warning(
            "Redis circuit breaker OPEN after %d failures in %ds window",
            _REDIS_FAILURE_COUNT,
            _REDIS_FAILURE_WINDOW_SECONDS,
        )


def is_redis_circuit_open() -> bool:
    """Public accessor for healthcheck / dashboard."""
    return _check_redis_circuit()


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
            family: (sum(scores) / len(scores)) for family, scores in fam_map.items() if scores
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
    global _redis_circuit_warned_this_cycle  # noqa: PLW0603
    _redis_circuit_warned_this_cycle = False
    if _check_redis_circuit():
        logger.warning(
            "Redis circuit open — WATCH decay disabled; alerts may repeat",
        )
        _redis_circuit_warned_this_cycle = True

    # ── Parse LLM JSON ───────────────────────────────────────────
    raw, _parse_used_repair = parse_llm_alerts(
        llm_response,
        add_score_fn=add_score_fn,
        trace_id=trace_id,
    )
    if raw is None:
        return [], "[]"

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
    regime = classify_regime(vix, risk_off, bulls, bears, trend_strength)
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
    dedup_suppressed_count = 0

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

        # TESTING NOTE: alert.sources_agree from the LLM is overwritten here with
        # _aligned_family_count() + macro injection + optional forecast bonus.
        # Tests must derive expected SA from snapshot fixtures, not hardcode LLM values.
        # See CONTRIBUTING.md § Reconciliation pipeline and _aligned_family_count().
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
            family_scores["macro"] = (
                -abs(_SA_MACRO_CONTEXT_SCORE) if risk_off else abs(_SA_MACRO_CONTEXT_SCORE)
            )
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

        # ── Extended-hours confidence penalty (optional) ─────────
        if directional and EXTENDED_HOURS_ALERTS_ENABLED and market_session in {"pre", "after"}:
            before = alert.confidence
            alert.confidence = max(0.0, alert.confidence + EXTENDED_HOURS_CONFIDENCE_PENALTY)
            logger.info(
                "Extended-hours penalty: %s session=%s conf %.2f -> %.2f",
                alert.symbol,
                market_session,
                before,
                alert.confidence,
            )

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
            # TESTING NOTE: when confidence >= HIGH_CONFIDENCE_MIN (default 0.85),
            # sources_agree must also be >= HIGH_CONFIDENCE_MIN_SA (default 5).
            # High confidence alone does not bypass SA — set both fields consistently in fixtures.
            if alert.confidence >= _HIGH_CONFIDENCE_MIN and alert.sources_agree < _HIGH_CONFIDENCE_MIN_SA:
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
        rr_ratio = _rr(alert)
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
            if risk > 0 and rr_ratio < _rr_min:
                logger.info(
                    "Alert filtered: %s R:R %.2f:1 below %.1f:1 minimum (%s)",
                    alert.symbol,
                    rr_ratio,
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
        elif _try_dedup_set(alert.symbol, alert.direction, timeframe):
            row = (alert.symbol, GateRejection.DEDUP_SUPPRESSED)
            rejections.append(row)
            dedup_suppressed_count += 1
            if directional:
                directional_rejections.append(row)
            else:
                watch_rejections.append(row)
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
            key=lambda a: a.edge_probability
            * a.confidence
            * (
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
                    "WATCH_DECAY: %s stale across %d cycles (ep=%.2f conf=%.2f) – dropping",
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
        if _check_redis_circuit():
            _effective_watch_max = min(_effective_watch_max, _WATCH_MAX_STRESSED)
        if len(watch_alerts) > _effective_watch_max:
            for w in watch_alerts[_effective_watch_max:]:
                row = (w.symbol, GateRejection.WATCH_CAP)
                rejections.append(row)
                watch_rejections.append(row)
            watch_alerts = watch_alerts[:_effective_watch_max]

        alerts = directional_alerts + watch_alerts
        if _check_redis_circuit():
            for _w in watch_alerts:
                WATCH_DECAY_SKIPPED.inc()

    # ── Update Redis WATCH-cycle state ────────────────────────────
    # Increment cycle count for each kept WATCH alert, then check for
    # strengthening setups to prefix the thesis with [↑ STRENGTHENING ×N].
    for w in [a for a in alerts if a.direction == "WATCH"]:
        new_cycles = _incr_watch_cycles(w.symbol, timeframe, w.edge_probability, w.confidence)
        logger.debug("WATCH_CYCLE_INCR: %s cycles=%d", w.symbol, new_cycles)
        if new_cycles >= _WATCH_PROMOTION_MIN_CYCLES and _watch_is_improving(
            w.symbol, w.edge_probability, prev_states
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
        _reset_dedup_keys(directional_symbols, timeframe)
        logger.debug("WATCH_CYCLE_RESET: %s", ", ".join(directional_symbols))

    pre_dist = _candidate_distribution(candidates)
    post_dist = _candidate_distribution(alerts)

    log_decision_gate_summary(
        timeframe=timeframe,
        raw_count=len(raw),
        candidates_count=len(candidates),
        alerts=alerts,
        directional_alerts=directional_alerts,
        watch_alerts=watch_alerts,
        rejections=rejections,
        directional_rejections=directional_rejections,
        watch_rejections=watch_rejections,
        regime=regime,
        market_session=market_session,
        trend_strength=trend_strength,
        bulls=bulls,
        bears=bears,
        ep_gate=ep_gate,
        base_ep_gate=base_ep_gate,
        sa_gate=sa_gate,
        base_sa_gate=base_sa_gate,
        conf_gate=conf_gate,
        base_conf_gate=base_conf_gate,
        pre_dist=pre_dist,
        post_dist=post_dist,
    )

    watch_kept = sum(1 for a in alerts if a.direction == "WATCH")
    _record_session_gate_metrics(
        timeframe,
        len(raw),
        len(directional_alerts),
        watch_kept,
        directional_rejections,
        watch_rejections,
        dedup_suppressed_count,
    )

    if add_score_fn and trace_id:
        record_langfuse_gate_scores(
            add_score_fn=add_score_fn,
            trace_id=trace_id,
            raw_count=len(raw),
            alerts=alerts,
            rejections=rejections,
            pre_dist=pre_dist,
            post_dist=post_dist,
        )

    record_prometheus_gate_metrics(
        timeframe=timeframe,
        alerts=alerts,
        rejections=rejections,
    )
    alerts_json = json.dumps([a.model_dump() for a in alerts])
    return alerts, alerts_json
