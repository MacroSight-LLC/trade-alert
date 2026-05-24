"""Per-candidate gate evaluation extracted from validate_and_filter."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import gate_config
from gates.dedup import _try_dedup_set
from gates.reconciliation import _aligned_family_count
from gates.regime import EP_CEILING
from gates.rr_volume import _rr
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
    if config.sa_include_macro_context and "macro" not in family_scores:
        family_scores["macro"] = (
            -abs(config.sa_macro_context_score) if risk_off else abs(config.sa_macro_context_score)
        )
    llm_sources_agree = alert.sources_agree
    deterministic_sources_agree = _aligned_family_count(family_scores, alert.direction)
    if config.sa_forecast_confirm_bonus_enabled:
        trend_score = float(family_scores.get("trend", 0.0))
        fc_score = forecast_scores.get(alert.symbol)
        sym_types = snap_types.get(alert.symbol, set())
        trend_has_non_fc = bool(sym_types & {"technical_trend", "relative_strength"})
        if fc_score is not None and trend_has_non_fc:
            if (
                alert.direction == "LONG"
                and trend_score >= config.sa_family_min_score
                and fc_score >= config.sa_forecast_bonus_threshold
            ):
                deterministic_sources_agree += 1
            elif (
                alert.direction == "SHORT"
                and trend_score <= -config.sa_family_min_score
                and fc_score <= -config.sa_forecast_bonus_threshold
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

    if directional and GateRejection.ENTRY_ORDER_INVALID not in reason_set:
        ref_price = ref_prices.get(alert.symbol)
        if ref_price and ref_price > 0:
            drift_pct = abs(alert.entry["level"] - ref_price) / ref_price
            drift_gate = config.entry_market_drift_max_pct
            if vix >= config.vix_soft_threshold:
                drift_gate += config.entry_market_drift_vix_bump
            if vix >= config.entry_market_drift_vix_high_threshold:
                drift_gate += config.entry_market_drift_vix_high_bump
            if market_session in {"pre", "after"}:
                drift_gate += config.entry_market_drift_prepost_bump
            drift_gate = min(drift_gate, config.entry_market_drift_cap_pct)
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

    if directional and gate_config.EXTENDED_HOURS_ALERTS_ENABLED and market_session in {"pre", "after"}:
        before = alert.confidence
        alert.confidence = max(0.0, alert.confidence + gate_config.EXTENDED_HOURS_CONFIDENCE_PENALTY)
        logger.info(
            "Extended-hours penalty: %s session=%s conf %.2f -> %.2f",
            alert.symbol,
            market_session,
            before,
            alert.confidence,
        )

    if directional and market_session == "closed" and config.market_hours_gates_enabled:
        logger.info(
            "Market-session gate: %s %s rejected (session=%s)",
            alert.symbol,
            alert.direction,
            market_session,
        )
        _add_reason(GateRejection.MARKET_SESSION_CLOSED)

    if vix > 30.0 and directional:
        logger.warning(
            "VIX hard gate: %s %s rejected (VIX=%.1f > 30.0)",
            alert.symbol,
            alert.direction,
            vix,
        )
        _add_reason(GateRejection.VIX_HARD)

    if alert.direction == "WATCH":
        watch_ep_gate = max(ep_gate - config.watch_ep_delta, 0.50)
        if alert.edge_probability < watch_ep_gate:
            logger.info(
                "WATCH filtered (EP): %s ep=%.2f < watch_gate=%.2f",
                alert.symbol,
                alert.edge_probability,
                watch_ep_gate,
            )
            _add_reason(GateRejection.WATCH_EP_THRESHOLD)
        watch_sa_gate = max(config.watch_sa_min, sa_gate - 1)
        watch_conf_gate = max(config.watch_conf_min, conf_gate - 0.10)
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
        if (
            alert.confidence >= config.high_confidence_min
            and alert.sources_agree < config.high_confidence_min_sa
        ):
            logger.info(
                "Alert filtered (HIGH_CONFIDENCE_ALIGNMENT): %s conf=%.2f >= %.2f but sa=%d < %d",
                alert.symbol,
                alert.confidence,
                config.high_confidence_min,
                alert.sources_agree,
                config.high_confidence_min_sa,
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
        _rr_min = gate_config.GATE_RR.get(timeframe, 2.0)
        if risk > 0 and rr_ratio < _rr_min:
            logger.info(
                "Alert filtered: %s R:R %.2f:1 below %.1f:1 minimum (%s)",
                alert.symbol,
                rr_ratio,
                _rr_min,
                timeframe,
            )
            _add_reason(GateRejection.RR_MINIMUM)

    if directional:
        _fc_score = forecast_scores.get(alert.symbol)
        if _fc_score is not None:
            _fc_contradicts = False
            if alert.direction == "LONG" and _fc_score < -config.forecast_gate_score_threshold:
                _fc_contradicts = True
            elif alert.direction == "SHORT" and _fc_score > config.forecast_gate_score_threshold:
                _fc_contradicts = True

            _fc_high_conviction = (
                alert.sources_agree >= config.forecast_gate_sa
                and alert.edge_probability >= config.forecast_gate_ep
            )
            if _fc_contradicts and not _fc_high_conviction:
                logger.info(
                    "Forecast contradiction: %s %s rejected "
                    "(forecast_score=%.2f, threshold=%.2f, sa=%d, ep=%.2f)",
                    alert.symbol,
                    alert.direction,
                    _fc_score,
                    config.forecast_gate_score_threshold,
                    alert.sources_agree,
                    alert.edge_probability,
                )
                _add_reason(GateRejection.FORECAST_CONTRADICTS)

    _macro_high_conviction = (
        alert.sources_agree >= config.macro_veto_sa and alert.edge_probability >= config.macro_veto_ep
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

    _high_conviction = (
        alert.sources_agree >= config.vix_soft_sa and alert.edge_probability >= config.vix_soft_ep
    )
    _vix_soft_triggered = False
    if vix > config.vix_soft_threshold and not _high_conviction:
        if risk_off and alert.direction == "LONG" and regime != "risk_off_high_vix":
            _vix_soft_triggered = True
        elif not risk_off and alert.direction == "SHORT":
            _vix_soft_triggered = True
    if _vix_soft_triggered:
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

    if directional:
        _vol_score = volume_scores.get(alert.symbol, 0.0)
        if _vol_score < config.volume_confirm_score:
            _vol_penalty = (
                config.volume_confirm_penalty_choppy
                if regime in ("choppy", "risk_off_high_vix")
                else config.volume_confirm_penalty
            )
            alert.confidence = max(alert.confidence - _vol_penalty, 0.0)
            if alert.confidence < conf_gate:
                logger.info(
                    "Volume unconfirmed: %s %s (vol_score=%.2f < %.2f) "
                    "conf downgraded by %.2f to %.2f < gate %.2f (regime=%s)",
                    alert.symbol,
                    alert.direction,
                    _vol_score,
                    config.volume_confirm_score,
                    _vol_penalty,
                    alert.confidence,
                    conf_gate,
                    regime,
                )
                _add_reason(GateRejection.VOLUME_UNCONFIRMED)

    if reason_list:
        return CandidateOutcome(status="rejected", alert=alert, reasons=reason_list)
    if _try_dedup_set(alert.symbol, alert.direction, timeframe):
        return CandidateOutcome(
            status="dedup_suppressed",
            alert=alert,
            reasons=[GateRejection.DEDUP_SUPPRESSED],
        )
    return CandidateOutcome(status="accepted", alert=alert)
