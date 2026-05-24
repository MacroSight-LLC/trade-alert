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
import sys
from typing import Any

from constants import get_market_hours_status
from gate_config import (
    EXTENDED_HOURS_ALERTS_ENABLED,
    EXTENDED_HOURS_CONFIDENCE_PENALTY,
    GATE_CONF,
    GATE_EP,
    GATE_RR,
    GATE_SA,
    classify_regime,
)
from gate_telemetry import (
    log_decision_gate_summary,
    record_langfuse_gate_scores,
    record_prometheus_gate_metrics,
)
from gates.candidate import CandidateGateConfig, _evaluate_candidate
from gates.dedup import _dedup_key, _reset_dedup_keys, _try_dedup_set
from gates.reconciliation import (
    _aligned_family_count,
    _build_snap_type_index,
    _build_symbol_family_scores,
    _parse_snapshots,
    _signal_directional_score,
)
from gates.redis_circuit import (
    _check_redis_circuit as _rc_check_redis_circuit,
    _record_redis_failure as _rc_record_redis_failure,
    is_redis_circuit_open as _rc_is_redis_circuit_open,
    mark_circuit_warned,
    reset_circuit_warned_flag,
)
import gates.redis_circuit as _redis_circuit
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
from gates.types import GateRejection
from gates.watch import (
    _get_watch_cycles,
    _get_watch_prev_state,
    _incr_watch_cycles,
    _reset_watch_cycles,
    _watch_decay_key,
    _watch_is_improving,
    _watch_max_for_regime,
)
from llm_response_parser import parse_llm_alerts
from metrics import GATE_REJECTIONS, REDIS_CIRCUIT_OPEN, WATCH_DECAY_SKIPPED
from models import PlaybookAlert
from redis_client import get_redis

_classify_regime = classify_regime  # backward-compatible alias for tests

logger = logging.getLogger(__name__)

# Re-export gate thresholds from gate_config SSOT (backward compatible names).
_GATE_EP: dict[str, float] = GATE_EP
_GATE_SA: int = GATE_SA
_GATE_CONF: float = GATE_CONF
_GATE_RR: dict[str, float] = GATE_RR

# WATCH policy constants (orchestration path; also referenced by tests via this module).
_WATCH_MAX_PER_RUN: int = int(os.environ.get("WATCH_MAX_PER_RUN", "1"))
_WATCH_DECAY_CYCLES: int = int(os.environ.get("WATCH_DECAY_CYCLES", "4"))
_WATCH_MAX_STRESSED: int = int(os.environ.get("WATCH_MAX_STRESSED", str(_WATCH_MAX_PER_RUN)))
_WATCH_MAX_NEUTRAL: int = int(os.environ.get("WATCH_MAX_NEUTRAL", "2"))
_WATCH_MAX_TRENDING: int = int(os.environ.get("WATCH_MAX_TRENDING", "3"))
_WATCH_PROMOTION_BONUS_MULT: float = float(os.environ.get("WATCH_PROMOTION_BONUS_MULT", "1.15"))
_WATCH_PROMOTION_MIN_CYCLES: int = int(os.environ.get("WATCH_PROMOTION_MIN_CYCLES", "2"))

# Dynamic gate controls (regime overlays — patched by tests via this module).
_DYNAMIC_GATES_ENABLED: bool = os.environ.get("DYNAMIC_GATES_ENABLED", "1") == "1"

# Reconciliation constants (test-patchable via this module).
_SA_FAMILY_MIN_SCORE: float = float(os.environ.get("SA_FAMILY_MIN_SCORE", "0.25"))
_SA_INCLUDE_MACRO_CONTEXT: bool = os.environ.get("SA_INCLUDE_MACRO_CONTEXT", "1") == "1"
_SA_MACRO_CONTEXT_SCORE: float = float(os.environ.get("SA_MACRO_CONTEXT_SCORE", "0.50"))
_SA_FORECAST_CONFIRM_BONUS_ENABLED: bool = os.environ.get("SA_FORECAST_CONFIRM_BONUS_ENABLED", "1") == "1"
_SA_FORECAST_BONUS_THRESHOLD: float = float(os.environ.get("SA_FORECAST_BONUS_THRESHOLD", "0.80"))

# Per-candidate gate constants (test-patchable via this module).
_HIGH_CONFIDENCE_MIN: float = float(os.environ.get("HIGH_CONFIDENCE_MIN", "0.85"))
_HIGH_CONFIDENCE_MIN_SA: int = int(os.environ.get("HIGH_CONFIDENCE_MIN_SA", "5"))
_MACRO_VETO_SA: int = int(os.environ.get("MACRO_VETO_SA", "6"))
_MACRO_VETO_EP: float = float(os.environ.get("MACRO_VETO_EP", "0.90"))
_VIX_SOFT_THRESHOLD: float = float(os.environ.get("VIX_SOFT_THRESHOLD", "25.0"))
_VIX_SOFT_SA: int = int(os.environ.get("VIX_SOFT_SA", "3"))
_VIX_SOFT_EP: float = float(os.environ.get("VIX_SOFT_EP", "0.72"))
_WATCH_SA_MIN: int = int(os.environ.get("WATCH_SA_MIN", "2"))
_WATCH_CONF_MIN: float = float(os.environ.get("WATCH_CONF_MIN", "0.60"))
_WATCH_EP_DELTA: float = float(os.environ.get("WATCH_EP_DELTA", "0.05"))
_FORECAST_GATE_SCORE_THRESHOLD: float = float(os.environ.get("FORECAST_GATE_SCORE_THRESHOLD", "0.8"))
_FORECAST_GATE_SA: int = int(os.environ.get("FORECAST_GATE_SA", "5"))
_FORECAST_GATE_EP: float = float(os.environ.get("FORECAST_GATE_EP", "0.85"))
_VOLUME_CONFIRM_SCORE: float = float(os.environ.get("VOLUME_CONFIRM_SCORE", "1.5"))
_VOLUME_CONFIRM_PENALTY: float = float(os.environ.get("VOLUME_CONFIRM_PENALTY", "0.05"))
_VOLUME_CONFIRM_PENALTY_CHOPPY: float = float(os.environ.get("VOLUME_CONFIRM_PENALTY_CHOPPY", "0.10"))
_ENTRY_MARKET_DRIFT_MAX_PCT: float = float(os.environ.get("ENTRY_MARKET_DRIFT_MAX_PCT", "0.03"))
_ENTRY_MARKET_DRIFT_VIX_BUMP: float = float(os.environ.get("ENTRY_MARKET_DRIFT_VIX_BUMP", "0.01"))
_ENTRY_MARKET_DRIFT_PREPOST_BUMP: float = float(os.environ.get("ENTRY_MARKET_DRIFT_PREPOST_BUMP", "0.01"))
_ENTRY_MARKET_DRIFT_CAP_PCT: float = float(os.environ.get("ENTRY_MARKET_DRIFT_CAP_PCT", "0.08"))
_ENTRY_MARKET_DRIFT_VIX_HIGH_THRESHOLD: float = float(
    os.environ.get("ENTRY_MARKET_DRIFT_VIX_HIGH_THRESHOLD", "30.0")
)
_ENTRY_MARKET_DRIFT_VIX_HIGH_BUMP: float = float(os.environ.get("ENTRY_MARKET_DRIFT_VIX_HIGH_BUMP", "0.02"))

# Session gate constants re-exported for gates.session lazy imports and tests.
_MARKET_HOURS_GATES_ENABLED: bool = os.environ.get("MARKET_HOURS_GATES_ENABLED", "1") == "1"
_SESSION_PREPOST_EP_BUMP: float = float(os.environ.get("SESSION_PREPOST_EP_BUMP", "0.03"))
_SESSION_PREPOST_CONF_BUMP: float = float(os.environ.get("SESSION_PREPOST_CONF_BUMP", "0.05"))
_SESSION_PREPOST_SA_BUMP: int = int(os.environ.get("SESSION_PREPOST_SA_BUMP", "2"))

_REDIS_STATE_ATTRS = (
    "_REDIS_FAILURE_COUNT",
    "_REDIS_FAILURE_THRESHOLD",
    "_REDIS_FAILURE_WINDOW_SECONDS",
    "_redis_last_failure_ts",
    "_redis_circuit_open",
    "_redis_circuit_warned_this_cycle",
)


def _sync_redis_state_to_module() -> None:
    mod = sys.modules[__name__]
    for name in _REDIS_STATE_ATTRS:
        setattr(mod, name, getattr(_redis_circuit, name))


def _sync_redis_state_from_module() -> None:
    mod = sys.modules[__name__]
    for name in _REDIS_STATE_ATTRS:
        if hasattr(mod, name):
            setattr(_redis_circuit, name, getattr(mod, name))


def _check_redis_circuit() -> bool:
    _sync_redis_state_from_module()
    result = _rc_check_redis_circuit()
    _sync_redis_state_to_module()
    return result


def _record_redis_failure() -> None:
    _sync_redis_state_from_module()
    _rc_record_redis_failure()
    _sync_redis_state_to_module()


def is_redis_circuit_open() -> bool:
    _sync_redis_state_from_module()
    result = _rc_is_redis_circuit_open()
    _sync_redis_state_to_module()
    return result


_sync_redis_state_to_module()


def _build_candidate_gate_config() -> CandidateGateConfig:
    """Build per-candidate config from this module (supports test monkeypatch/reload)."""
    mod = sys.modules[__name__]
    return CandidateGateConfig(
        sa_family_min_score=mod._SA_FAMILY_MIN_SCORE,
        sa_include_macro_context=mod._SA_INCLUDE_MACRO_CONTEXT,
        sa_macro_context_score=mod._SA_MACRO_CONTEXT_SCORE,
        sa_forecast_confirm_bonus_enabled=mod._SA_FORECAST_CONFIRM_BONUS_ENABLED,
        sa_forecast_bonus_threshold=mod._SA_FORECAST_BONUS_THRESHOLD,
        high_confidence_min=mod._HIGH_CONFIDENCE_MIN,
        high_confidence_min_sa=mod._HIGH_CONFIDENCE_MIN_SA,
        macro_veto_sa=mod._MACRO_VETO_SA,
        macro_veto_ep=mod._MACRO_VETO_EP,
        vix_soft_threshold=mod._VIX_SOFT_THRESHOLD,
        vix_soft_sa=mod._VIX_SOFT_SA,
        vix_soft_ep=mod._VIX_SOFT_EP,
        watch_sa_min=mod._WATCH_SA_MIN,
        watch_conf_min=mod._WATCH_CONF_MIN,
        watch_ep_delta=mod._WATCH_EP_DELTA,
        forecast_gate_score_threshold=mod._FORECAST_GATE_SCORE_THRESHOLD,
        forecast_gate_sa=mod._FORECAST_GATE_SA,
        forecast_gate_ep=mod._FORECAST_GATE_EP,
        volume_confirm_score=mod._VOLUME_CONFIRM_SCORE,
        volume_confirm_penalty=mod._VOLUME_CONFIRM_PENALTY,
        volume_confirm_penalty_choppy=mod._VOLUME_CONFIRM_PENALTY_CHOPPY,
        entry_market_drift_max_pct=mod._ENTRY_MARKET_DRIFT_MAX_PCT,
        entry_market_drift_vix_bump=mod._ENTRY_MARKET_DRIFT_VIX_BUMP,
        entry_market_drift_prepost_bump=mod._ENTRY_MARKET_DRIFT_PREPOST_BUMP,
        entry_market_drift_cap_pct=mod._ENTRY_MARKET_DRIFT_CAP_PCT,
        entry_market_drift_vix_high_threshold=mod._ENTRY_MARKET_DRIFT_VIX_HIGH_THRESHOLD,
        entry_market_drift_vix_high_bump=mod._ENTRY_MARKET_DRIFT_VIX_HIGH_BUMP,
        market_hours_gates_enabled=mod._MARKET_HOURS_GATES_ENABLED,
    )


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
    reset_circuit_warned_flag()
    if _check_redis_circuit():
        logger.warning(
            "Redis circuit open — WATCH decay disabled; alerts may repeat",
        )
        mark_circuit_warned()

    raw, _parse_used_repair = parse_llm_alerts(
        llm_response,
        add_score_fn=add_score_fn,
        trace_id=trace_id,
    )
    if raw is None:
        return [], "[]"

    parsed_snaps = _parse_snapshots(snapshots_json)
    snap_types = _build_snap_type_index(parsed_snaps)
    family_scores_index = _build_symbol_family_scores(parsed_snaps)
    forecast_scores = _get_forecast_scores(parsed_snaps)
    volume_scores = _get_volume_spike_scores(parsed_snaps)
    ref_prices = _get_reference_prices(parsed_snaps)
    risk_off = not macro.get("risk_on", True)
    base_ep_gate = _GATE_EP.get(timeframe, 0.70)
    base_sa_gate = _GATE_SA
    base_conf_gate = _GATE_CONF

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
    candidate_config = _build_candidate_gate_config()

    for item in raw:
        outcome = _evaluate_candidate(
            item,
            config=candidate_config,
            playbook_alert_cls=PlaybookAlert,
            timeframe=timeframe,
            snap_types=snap_types,
            family_scores_index=family_scores_index,
            forecast_scores=forecast_scores,
            volume_scores=volume_scores,
            ref_prices=ref_prices,
            risk_off=risk_off,
            vix=vix,
            regime=regime,
            ep_gate=ep_gate,
            sa_gate=sa_gate,
            conf_gate=conf_gate,
            market_session=market_session,
            add_score_fn=add_score_fn,
            trace_id=trace_id,
        )
        if outcome.status == "parse_failed":
            continue
        if outcome.status == "timeframe_rejected":
            assert outcome.alert is not None
            rejections.append((outcome.alert.symbol, GateRejection.TIMEFRAME_INVALID))
            continue

        assert outcome.alert is not None
        alert = outcome.alert
        candidates.append(alert)
        directional = alert.direction in ("LONG", "SHORT")

        if outcome.status == "rejected":
            rejected_rows = [(alert.symbol, reason) for reason in outcome.reasons]
            rejections.extend(rejected_rows)
            if directional:
                directional_rejections.extend(rejected_rows)
            else:
                watch_rejections.extend(rejected_rows)
        elif outcome.status == "dedup_suppressed":
            row = (alert.symbol, GateRejection.DEDUP_SUPPRESSED)
            rejections.append(row)
            dedup_suppressed_count += 1
            if directional:
                directional_rejections.append(row)
            else:
                watch_rejections.append(row)
        else:
            alerts.append(alert)

    directional_alerts = [a for a in alerts if a.direction in ("LONG", "SHORT")]
    watch_alerts = [a for a in alerts if a.direction == "WATCH"]

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
