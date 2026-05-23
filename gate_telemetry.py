"""Gate-run logging, Langfuse scores, and Prometheus counters.

Extracted from validate_and_filter (SSOT audit #22).
"""

from __future__ import annotations

import logging
from typing import Any

from metrics import ALERTS_PER_CYCLE, GATE_REJECTIONS

logger = logging.getLogger(__name__)


def log_decision_gate_summary(
    *,
    timeframe: str,
    raw_count: int,
    candidates_count: int,
    alerts: list[Any],
    directional_alerts: list[Any],
    watch_alerts: list[Any],
    rejections: list[tuple[str, Any]],
    directional_rejections: list[tuple[str, Any]],
    watch_rejections: list[tuple[str, Any]],
    regime: str,
    market_session: str,
    trend_strength: float,
    bulls: int,
    bears: int,
    ep_gate: float,
    base_ep_gate: float,
    sa_gate: int,
    base_sa_gate: int,
    conf_gate: float,
    base_conf_gate: float,
    pre_dist: dict[str, float],
    post_dist: dict[str, float],
) -> str | None:
    """Emit structured gate summary logs; return no-alert reason when applicable."""
    logger.info(
        "Decision-%s gate summary: llm_candidates=%d parsed_candidates=%d passed_total=%d "
        "passed_directional=%d passed_watch=%d rejected_total=%d rejected_directional=%d rejected_watch=%d "
        "regime=%s market_session=%s trend_strength=%.2f breadth=%d/%d "
        "ep_gate=%.2f(base=%.2f) sa_gate=%d(base=%d) conf_gate=%.2f(base=%.2f)",
        timeframe,
        raw_count,
        candidates_count,
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

    gate_samples: dict[str, list[str]] = {}
    for sym, gate in rejections:
        gate_samples.setdefault(gate.value, []).append(sym)
    for gate_name, symbols in gate_samples.items():
        sample = symbols[:3]
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
            ", ".join(f"{gate_name}={len(symbols)}" for gate_name, symbols in sorted(gate_samples.items())),
        )

    if len(alerts) == 0:
        if raw_count == 0:
            no_alert_reason = "llm_zero_candidates"
        elif candidates_count == 0:
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
            candidates_count,
            raw_count,
        )
        return no_alert_reason
    return None


def record_langfuse_gate_scores(
    *,
    add_score_fn: Any,
    trace_id: str,
    raw_count: int,
    alerts: list[Any],
    rejections: list[tuple[str, Any]],
    pre_dist: dict[str, float],
    post_dist: dict[str, float],
) -> None:
    """Push gate pass/rejection metrics to Langfuse."""
    total = max(raw_count, 1)
    pass_rate = len(alerts) / total
    rejection_rate = len(rejections) / total
    add_score_fn(
        trace_id,
        "alert_pass_rate",
        pass_rate,
        comment=f"{len(alerts)}/{raw_count} passed gates",
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
        comment=f"{len(rejections)}/{raw_count} rejected",
    )
    if rejection_rate > 0.9 and raw_count >= 3:
        logger.warning(
            "Gate rejection rate %.0f%% (%d/%d) exceeds 90%% threshold — "
            "LLM output quality may have degraded",
            rejection_rate * 100,
            len(rejections),
            raw_count,
        )
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


def record_prometheus_gate_metrics(
    *,
    timeframe: str,
    alerts: list[Any],
    rejections: list[tuple[str, Any]],
) -> None:
    """Increment Prometheus gate rejection counters and observe alerts per cycle."""
    gate_counts: dict[str, int] = {}
    for _sym, gate in rejections:
        gate_counts[gate.value] = gate_counts.get(gate.value, 0) + 1
    for gate_name, count in gate_counts.items():
        GATE_REJECTIONS.labels(gate=gate_name).inc(count)
    ALERTS_PER_CYCLE.labels(timeframe=timeframe).observe(len(alerts))
