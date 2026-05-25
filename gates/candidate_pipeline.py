"""Ordered gate stages for per-candidate evaluation (SSOT §10 gate order)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import gate_config
from gates.dedup import _try_dedup_set
from gates.reconciliation import _aligned_family_count
from gates.regime import EP_CEILING
from gates.rr_volume import _rr
from gates.types import GateRejection
from models import PlaybookAlert

if TYPE_CHECKING:
    from gates.candidate import CandidateGateConfig, CandidateOutcome

logger = logging.getLogger(__name__)

GateStage = Callable[["CandidateContext"], None]


@dataclass
class ReasonTracker:
    """Deduped rejection reasons accumulated across pipeline stages."""

    _reasons: list[GateRejection] = field(default_factory=list)
    _seen: set[GateRejection] = field(default_factory=set)

    def add(self, reason: GateRejection) -> None:
        if reason not in self._seen:
            self._seen.add(reason)
            self._reasons.append(reason)

    @property
    def reasons(self) -> list[GateRejection]:
        return self._reasons

    @property
    def reason_set(self) -> set[GateRejection]:
        return self._seen


@dataclass
class CandidateContext:
    """Runtime context for one candidate through the gate pipeline."""

    alert: PlaybookAlert
    config: CandidateGateConfig
    timeframe: str
    regime: str
    vix: float
    risk_off: bool
    ep_gate: float
    sa_gate: int
    conf_gate: float
    market_session: str
    snap_types: dict[str, set[str]]
    family_scores_index: dict[str, dict[str, float]]
    forecast_scores: dict[str, float]
    volume_scores: dict[str, float]
    ref_prices: dict[str, float]
    add_score_fn: Callable[..., Any] | None = None
    trace_id: str | None = None
    tracker: ReasonTracker = field(default_factory=ReasonTracker)

    @property
    def directional(self) -> bool:
        return self.alert.direction in ("LONG", "SHORT")

    @property
    def actual_sources(self) -> int:
        return len(self.snap_types.get(self.alert.symbol, set()))


@dataclass
class StageResult:
    """Optional early-exit signal from a stage (reasons live on ``ctx.tracker``)."""

    stop: bool = False


def _stage_source_hallucination(ctx: CandidateContext) -> None:
    if ctx.actual_sources == 0:
        logger.warning("SYMBOL_HALLUCINATION: %s not present in merged snapshots", ctx.alert.symbol)
        if ctx.add_score_fn and ctx.trace_id:
            ctx.add_score_fn(
                ctx.trace_id,
                "symbol_hallucination",
                1.0,
                comment=f"{ctx.alert.symbol}: not in snapshot data",
            )
        ctx.tracker.add(GateRejection.SOURCE_HALLUCINATION)


def _stage_reconciliation(ctx: CandidateContext) -> None:
    # TESTING NOTE: alert.sources_agree from the LLM is overwritten here with
    # _aligned_family_count() + macro injection + optional forecast bonus.
    # Tests must derive expected SA from snapshot fixtures, not hardcode LLM values.
    family_scores = dict(ctx.family_scores_index.get(ctx.alert.symbol, {}))
    if ctx.config.sa_include_macro_context and "macro" not in family_scores:
        family_scores["macro"] = (
            -abs(ctx.config.sa_macro_context_score)
            if ctx.risk_off
            else abs(ctx.config.sa_macro_context_score)
        )
    llm_sources_agree = ctx.alert.sources_agree
    deterministic_sources_agree = _aligned_family_count(family_scores, ctx.alert.direction)
    if ctx.config.sa_forecast_confirm_bonus_enabled:
        trend_score = float(family_scores.get("trend", 0.0))
        fc_score = ctx.forecast_scores.get(ctx.alert.symbol)
        sym_types = ctx.snap_types.get(ctx.alert.symbol, set())
        trend_has_non_fc = bool(sym_types & {"technical_trend", "relative_strength"})
        if fc_score is not None and trend_has_non_fc:
            if (
                ctx.alert.direction == "LONG"
                and trend_score >= ctx.config.sa_family_min_score
                and fc_score >= ctx.config.sa_forecast_bonus_threshold
            ):
                deterministic_sources_agree += 1
            elif (
                ctx.alert.direction == "SHORT"
                and trend_score <= -ctx.config.sa_family_min_score
                and fc_score <= -ctx.config.sa_forecast_bonus_threshold
            ):
                deterministic_sources_agree += 1
    deterministic_sources_agree = min(deterministic_sources_agree, 7)
    if llm_sources_agree != deterministic_sources_agree:
        logger.info(
            "sources_agree server override: %s llm=%d server=%d",
            ctx.alert.symbol,
            llm_sources_agree,
            deterministic_sources_agree,
        )
        if ctx.add_score_fn and ctx.trace_id:
            ctx.add_score_fn(
                ctx.trace_id,
                "sources_agree_override",
                float(abs(llm_sources_agree - deterministic_sources_agree)),
                comment=(
                    f"{ctx.alert.symbol}: llm {llm_sources_agree}, "
                    f"server {deterministic_sources_agree}"
                ),
            )
    ctx.alert.sources_agree = deterministic_sources_agree

    ceiling = (
        0.50 if ctx.actual_sources == 0 else EP_CEILING.get(min(ctx.actual_sources, 11), 0.99)
    )
    if ctx.alert.edge_probability > ceiling:
        original_ep = ctx.alert.edge_probability
        ctx.alert.edge_probability = ceiling
        logger.warning(
            "EP_CAPPED_%d_SOURCES: %s EP=%.2f capped to %.2f (sources=%d)",
            ctx.actual_sources,
            ctx.alert.symbol,
            original_ep,
            ceiling,
            ctx.actual_sources,
        )


def _stage_entry_order(ctx: CandidateContext) -> None:
    if not ctx.directional:
        return

    for _pk in ("stop", "level", "target"):
        if ctx.alert.entry[_pk] <= 0:
            logger.warning(
                "Invalid price: %s %s %s=%.4f (must be > 0)",
                ctx.alert.symbol,
                ctx.alert.direction,
                _pk,
                ctx.alert.entry[_pk],
            )
            ctx.tracker.add(GateRejection.ENTRY_ORDER_INVALID)
            return

    if ctx.alert.direction == "LONG":
        if not (ctx.alert.entry["stop"] < ctx.alert.entry["level"] < ctx.alert.entry["target"]):
            logger.warning(
                "Entry order invalid: %s LONG stop=%.2f level=%.2f target=%.2f",
                ctx.alert.symbol,
                ctx.alert.entry["stop"],
                ctx.alert.entry["level"],
                ctx.alert.entry["target"],
            )
            ctx.tracker.add(GateRejection.ENTRY_ORDER_INVALID)
    elif ctx.alert.direction == "SHORT":
        if not (ctx.alert.entry["target"] < ctx.alert.entry["level"] < ctx.alert.entry["stop"]):
            logger.warning(
                "Entry order invalid: %s SHORT target=%.2f level=%.2f stop=%.2f",
                ctx.alert.symbol,
                ctx.alert.entry["target"],
                ctx.alert.entry["level"],
                ctx.alert.entry["stop"],
            )
            ctx.tracker.add(GateRejection.ENTRY_ORDER_INVALID)


def _stage_entry_market_drift(ctx: CandidateContext) -> None:
    if not ctx.directional or GateRejection.ENTRY_ORDER_INVALID in ctx.tracker.reason_set:
        return

    ref_price = ctx.ref_prices.get(ctx.alert.symbol)
    if not ref_price or ref_price <= 0:
        return

    drift_pct = abs(ctx.alert.entry["level"] - ref_price) / ref_price
    drift_gate = ctx.config.entry_market_drift_max_pct
    if ctx.vix >= ctx.config.vix_soft_threshold:
        drift_gate += ctx.config.entry_market_drift_vix_bump
    if ctx.vix >= ctx.config.entry_market_drift_vix_high_threshold:
        drift_gate += ctx.config.entry_market_drift_vix_high_bump
    if ctx.market_session in {"pre", "after"}:
        drift_gate += ctx.config.entry_market_drift_prepost_bump
    drift_gate = min(drift_gate, ctx.config.entry_market_drift_cap_pct)
    if drift_pct > drift_gate:
        logger.info(
            "Entry drift filtered: %s %s entry=%.2f ref=%.2f drift=%.1f%% > max=%.1f%%",
            ctx.alert.symbol,
            ctx.alert.direction,
            ctx.alert.entry["level"],
            ref_price,
            drift_pct * 100.0,
            drift_gate * 100.0,
        )
        ctx.tracker.add(GateRejection.ENTRY_MARKET_DRIFT)


def _stage_extended_hours_penalty(ctx: CandidateContext) -> None:
    if (
        not ctx.directional
        or not gate_config.EXTENDED_HOURS_ALERTS_ENABLED
        or ctx.market_session not in {"pre", "after"}
    ):
        return

    before = ctx.alert.confidence
    ctx.alert.confidence = max(0.0, ctx.alert.confidence + gate_config.EXTENDED_HOURS_CONFIDENCE_PENALTY)
    logger.info(
        "Extended-hours penalty: %s session=%s conf %.2f -> %.2f",
        ctx.alert.symbol,
        ctx.market_session,
        before,
        ctx.alert.confidence,
    )


def _stage_market_session_closed(ctx: CandidateContext) -> None:
    if (
        ctx.directional
        and ctx.market_session == "closed"
        and ctx.config.market_hours_gates_enabled
    ):
        logger.info(
            "Market-session gate: %s %s rejected (session=%s)",
            ctx.alert.symbol,
            ctx.alert.direction,
            ctx.market_session,
        )
        ctx.tracker.add(GateRejection.MARKET_SESSION_CLOSED)


def _stage_vix_hard(ctx: CandidateContext) -> None:
    if ctx.vix > 30.0 and ctx.directional:
        logger.warning(
            "VIX hard gate: %s %s rejected (VIX=%.1f > 30.0)",
            ctx.alert.symbol,
            ctx.alert.direction,
            ctx.vix,
        )
        ctx.tracker.add(GateRejection.VIX_HARD)


def _stage_threshold_gates(ctx: CandidateContext) -> None:
    if ctx.alert.direction == "WATCH":
        watch_ep_gate = max(ctx.ep_gate - ctx.config.watch_ep_delta, 0.50)
        if ctx.alert.edge_probability < watch_ep_gate:
            logger.info(
                "WATCH filtered (EP): %s ep=%.2f < watch_gate=%.2f",
                ctx.alert.symbol,
                ctx.alert.edge_probability,
                watch_ep_gate,
            )
            ctx.tracker.add(GateRejection.WATCH_EP_THRESHOLD)
        watch_sa_gate = max(ctx.config.watch_sa_min, ctx.sa_gate - 1)
        watch_conf_gate = max(ctx.config.watch_conf_min, ctx.conf_gate - 0.10)
        if ctx.alert.sources_agree < watch_sa_gate:
            logger.info(
                "WATCH filtered (SA): %s sa=%d < watch_gate=%d",
                ctx.alert.symbol,
                ctx.alert.sources_agree,
                watch_sa_gate,
            )
            ctx.tracker.add(GateRejection.WATCH_SA_THRESHOLD)
        if ctx.alert.confidence < watch_conf_gate:
            logger.info(
                "WATCH filtered (CONF): %s conf=%.2f < watch_gate=%.2f",
                ctx.alert.symbol,
                ctx.alert.confidence,
                watch_conf_gate,
            )
            ctx.tracker.add(GateRejection.WATCH_CONF_THRESHOLD)
        return

    if ctx.alert.edge_probability < ctx.ep_gate:
        logger.info(
            "Alert filtered (EP): %s ep=%.2f < gate=%.2f",
            ctx.alert.symbol,
            ctx.alert.edge_probability,
            ctx.ep_gate,
        )
        ctx.tracker.add(GateRejection.EP_THRESHOLD)
    if ctx.alert.sources_agree < ctx.sa_gate:
        logger.info(
            "Alert filtered (SA): %s sa=%d < gate=%d",
            ctx.alert.symbol,
            ctx.alert.sources_agree,
            ctx.sa_gate,
        )
        ctx.tracker.add(GateRejection.SA_THRESHOLD)
    # TESTING NOTE: when confidence >= HIGH_CONFIDENCE_MIN (default 0.85),
    # sources_agree must also be >= HIGH_CONFIDENCE_MIN_SA (default 5).
    if (
        ctx.alert.confidence >= ctx.config.high_confidence_min
        and ctx.alert.sources_agree < ctx.config.high_confidence_min_sa
    ):
        logger.info(
            "Alert filtered (HIGH_CONFIDENCE_ALIGNMENT): %s conf=%.2f >= %.2f but sa=%d < %d",
            ctx.alert.symbol,
            ctx.alert.confidence,
            ctx.config.high_confidence_min,
            ctx.alert.sources_agree,
            ctx.config.high_confidence_min_sa,
        )
        ctx.tracker.add(GateRejection.HIGH_CONFIDENCE_ALIGNMENT)
    if ctx.alert.confidence < ctx.conf_gate:
        logger.info(
            "Alert filtered (CONF): %s conf=%.2f < gate=%.2f",
            ctx.alert.symbol,
            ctx.alert.confidence,
            ctx.conf_gate,
        )
        ctx.tracker.add(GateRejection.CONF_THRESHOLD)


def _stage_rr_gates(ctx: CandidateContext) -> None:
    if not ctx.directional or GateRejection.ENTRY_ORDER_INVALID in ctx.tracker.reason_set:
        return

    risk = abs(ctx.alert.entry["level"] - ctx.alert.entry["stop"])
    rr_ratio = _rr(ctx.alert)
    if risk == 0:
        logger.warning(
            "R:R zero-risk rejected: %s stop==level (%.2f)",
            ctx.alert.symbol,
            ctx.alert.entry["level"],
        )
        ctx.tracker.add(GateRejection.RR_ZERO_RISK)
    micro_risk_floor = max(ctx.alert.entry["level"] * 0.001, 0.05)
    if risk < micro_risk_floor:
        logger.warning(
            "R:R micro-risk rejected: %s risk=%.4f < floor=%.4f (level=%.2f)",
            ctx.alert.symbol,
            risk,
            micro_risk_floor,
            ctx.alert.entry["level"],
        )
        ctx.tracker.add(GateRejection.RR_ZERO_RISK)
    _rr_min = gate_config.GATE_RR.get(ctx.timeframe, 2.0)
    if risk > 0 and rr_ratio < _rr_min:
        logger.info(
            "Alert filtered: %s R:R %.2f:1 below %.1f:1 minimum (%s)",
            ctx.alert.symbol,
            rr_ratio,
            _rr_min,
            ctx.timeframe,
        )
        ctx.tracker.add(GateRejection.RR_MINIMUM)


def _stage_forecast_contradicts(ctx: CandidateContext) -> None:
    if not ctx.directional:
        return

    _fc_score = ctx.forecast_scores.get(ctx.alert.symbol)
    if _fc_score is None:
        return

    _fc_contradicts = False
    if ctx.alert.direction == "LONG" and _fc_score < -ctx.config.forecast_gate_score_threshold:
        _fc_contradicts = True
    elif ctx.alert.direction == "SHORT" and _fc_score > ctx.config.forecast_gate_score_threshold:
        _fc_contradicts = True

    _fc_high_conviction = (
        ctx.alert.sources_agree >= ctx.config.forecast_gate_sa
        and ctx.alert.edge_probability >= ctx.config.forecast_gate_ep
    )
    if _fc_contradicts and not _fc_high_conviction:
        logger.info(
            "Forecast contradiction: %s %s rejected "
            "(forecast_score=%.2f, threshold=%.2f, sa=%d, ep=%.2f)",
            ctx.alert.symbol,
            ctx.alert.direction,
            _fc_score,
            ctx.config.forecast_gate_score_threshold,
            ctx.alert.sources_agree,
            ctx.alert.edge_probability,
        )
        ctx.tracker.add(GateRejection.FORECAST_CONTRADICTS)


def _stage_macro_veto(ctx: CandidateContext) -> None:
    _macro_high_conviction = (
        ctx.alert.sources_agree >= ctx.config.macro_veto_sa
        and ctx.alert.edge_probability >= ctx.config.macro_veto_ep
    )
    if (
        ctx.timeframe == "1h"
        and ctx.alert.direction == "LONG"
        and ctx.risk_off
        and ctx.vix >= 25.0
        and not _macro_high_conviction
    ):
        logger.info(
            "Macro veto: %s LONG rejected (risk_off=True, VIX=%.1f)",
            ctx.alert.symbol,
            ctx.vix,
        )
        ctx.tracker.add(GateRejection.MACRO_VETO)


def _stage_vix_soft(ctx: CandidateContext) -> None:
    _high_conviction = (
        ctx.alert.sources_agree >= ctx.config.vix_soft_sa
        and ctx.alert.edge_probability >= ctx.config.vix_soft_ep
    )
    _vix_soft_triggered = False
    if ctx.vix > ctx.config.vix_soft_threshold and not _high_conviction:
        if ctx.risk_off and ctx.alert.direction == "LONG" and ctx.regime != "risk_off_high_vix":
            _vix_soft_triggered = True
        elif not ctx.risk_off and ctx.alert.direction == "SHORT":
            _vix_soft_triggered = True
    if _vix_soft_triggered:
        logger.info(
            "VIX gate [REVIEW]: %s %s suppressed (VIX=%.1f, risk_off=%s, sa=%d, ep=%.2f)",
            ctx.alert.symbol,
            ctx.alert.direction,
            ctx.vix,
            ctx.risk_off,
            ctx.alert.sources_agree,
            ctx.alert.edge_probability,
        )
        ctx.tracker.add(GateRejection.VIX_SOFT)


def _stage_volume_unconfirmed(ctx: CandidateContext) -> None:
    if not ctx.directional:
        return

    _vol_score = ctx.volume_scores.get(ctx.alert.symbol, 0.0)
    if _vol_score < ctx.config.volume_confirm_score:
        _vol_penalty = (
            ctx.config.volume_confirm_penalty_choppy
            if ctx.regime in ("choppy", "risk_off_high_vix")
            else ctx.config.volume_confirm_penalty
        )
        ctx.alert.confidence = max(ctx.alert.confidence - _vol_penalty, 0.0)
        if ctx.alert.confidence < ctx.conf_gate:
            logger.info(
                "Volume unconfirmed: %s %s (vol_score=%.2f < %.2f) "
                "conf downgraded by %.2f to %.2f < gate %.2f (regime=%s)",
                ctx.alert.symbol,
                ctx.alert.direction,
                _vol_score,
                ctx.config.volume_confirm_score,
                _vol_penalty,
                ctx.alert.confidence,
                ctx.conf_gate,
                ctx.regime,
            )
            ctx.tracker.add(GateRejection.VOLUME_UNCONFIRMED)


DEFAULT_STAGES: list[GateStage] = [
    _stage_source_hallucination,
    _stage_reconciliation,
    _stage_entry_order,
    _stage_entry_market_drift,
    _stage_extended_hours_penalty,
    _stage_market_session_closed,
    _stage_vix_hard,
    _stage_threshold_gates,
    _stage_rr_gates,
    _stage_forecast_contradicts,
    _stage_macro_veto,
    _stage_vix_soft,
    _stage_volume_unconfirmed,
]


class CandidateGatePipeline:
    """Run ordered gate stages; dedup is handled after reason accumulation."""

    def __init__(self, stages: list[GateStage] | None = None) -> None:
        self.stages = list(stages) if stages is not None else list(DEFAULT_STAGES)

    def run(self, ctx: CandidateContext) -> CandidateOutcome:
        from gates.candidate import CandidateOutcome

        for stage in self.stages:
            stage(ctx)

        if ctx.tracker.reasons:
            return CandidateOutcome(
                status="rejected",
                alert=ctx.alert,
                reasons=ctx.tracker.reasons,
            )
        if _try_dedup_set(ctx.alert.symbol, ctx.alert.direction, ctx.timeframe):
            return CandidateOutcome(
                status="dedup_suppressed",
                alert=ctx.alert,
                reasons=[GateRejection.DEDUP_SUPPRESSED],
            )
        return CandidateOutcome(status="accepted", alert=ctx.alert)


DEFAULT_PIPELINE = CandidateGatePipeline()
