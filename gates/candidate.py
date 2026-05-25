"""Per-candidate gate evaluation extracted from validate_and_filter."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from gates.candidate_pipeline import DEFAULT_PIPELINE, CandidateContext
from gates.types import GateRejection
from models import PlaybookAlert

logger = logging.getLogger(__name__)

_VALID_TIMEFRAMES = {"5m", "15m", "1h", "4h", "1D"}


@dataclass(frozen=True)
class CandidateGateConfig:
    """Runtime gate thresholds passed from validate_and_filter (test-patchable)."""

    sa_family_min_score: float
    sa_include_macro_context: bool
    sa_macro_context_score: float
    sa_forecast_confirm_bonus_enabled: bool
    sa_forecast_bonus_threshold: float
    high_confidence_min: float
    high_confidence_min_sa: int
    macro_veto_sa: int
    macro_veto_ep: float
    vix_soft_threshold: float
    vix_soft_sa: int
    vix_soft_ep: float
    watch_sa_min: int
    watch_conf_min: float
    watch_ep_delta: float
    forecast_gate_score_threshold: float
    forecast_gate_sa: int
    forecast_gate_ep: float
    volume_confirm_score: float
    volume_confirm_penalty: float
    volume_confirm_penalty_choppy: float
    entry_market_drift_max_pct: float
    entry_market_drift_vix_bump: float
    entry_market_drift_prepost_bump: float
    entry_market_drift_cap_pct: float
    entry_market_drift_vix_high_threshold: float
    entry_market_drift_vix_high_bump: float
    market_hours_gates_enabled: bool


@dataclass
class CandidateOutcome:
    """Result of evaluating one LLM candidate through per-alert gates."""

    status: Literal["parse_failed", "timeframe_rejected", "rejected", "dedup_suppressed", "accepted"]
    alert: PlaybookAlert | None = None
    reasons: list[GateRejection] = field(default_factory=list)


def _evaluate_candidate(
    item: dict[str, Any],
    *,
    config: CandidateGateConfig,
    playbook_alert_cls: type[PlaybookAlert],
    timeframe: str,
    snap_types: dict[str, set[str]],
    family_scores_index: dict[str, dict[str, float]],
    forecast_scores: dict[str, float],
    volume_scores: dict[str, float],
    ref_prices: dict[str, float],
    risk_off: bool,
    vix: float,
    regime: str,
    ep_gate: float,
    sa_gate: int,
    conf_gate: float,
    market_session: str,
    add_score_fn: Callable[..., Any] | None = None,
    trace_id: str | None = None,
) -> CandidateOutcome:
    """Parse and evaluate one LLM candidate through all per-alert gates."""
    try:
        alert = playbook_alert_cls(**item)
    except Exception as e:
        logger.warning("PlaybookAlert validation failed for %s: %s", item, e)
        return CandidateOutcome(status="parse_failed")

    alert_tf = getattr(alert, "timeframe", timeframe)
    if alert_tf not in _VALID_TIMEFRAMES:
        logger.warning(
            "Timeframe invalid: %s timeframe=%s not in %s",
            alert.symbol,
            alert_tf,
            _VALID_TIMEFRAMES,
        )
        return CandidateOutcome(
            status="timeframe_rejected",
            alert=alert,
            reasons=[GateRejection.TIMEFRAME_INVALID],
        )

    ctx = CandidateContext(
        alert=alert,
        config=config,
        timeframe=timeframe,
        regime=regime,
        vix=vix,
        risk_off=risk_off,
        ep_gate=ep_gate,
        sa_gate=sa_gate,
        conf_gate=conf_gate,
        market_session=market_session,
        snap_types=snap_types,
        family_scores_index=family_scores_index,
        forecast_scores=forecast_scores,
        volume_scores=volume_scores,
        ref_prices=ref_prices,
        add_score_fn=add_score_fn,
        trace_id=trace_id,
    )
    return DEFAULT_PIPELINE.run(ctx)
