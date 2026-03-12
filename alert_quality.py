"""Per-alert quality scoring for Langfuse observability.

Computes granular quality metrics for each PlaybookAlert that the
decision engine produces, posts them to Langfuse as scores on the
pipeline trace, and returns a structured quality report.

Used by the validate-and-filter step in decision workflows to:
1. Score individual alert quality (thesis, R:R, signal coverage)
2. Score batch-level quality (diversity, concentration)
3. Feed Langfuse datasets for continuous prompt improvement
"""

from __future__ import annotations

import logging
import re
from typing import Any

from models import PlaybookAlert

logger = logging.getLogger(__name__)

# Minimum thresholds for quality sub-scores
_MIN_THESIS_WORDS = 15
_MIN_RR_RATIO = 2.0
_VAGUE_PHRASES = frozenset(
    {
        "strong signals",
        "multiple sources",
        "signals suggest",
        "indicators point",
        "positive outlook",
        "bearish outlook",
        "looks good",
        "appears strong",
    }
)


def score_thesis_quality(thesis: str) -> float:
    """Score the specificity and quality of an alert thesis.

    Args:
        thesis: The thesis string from a PlaybookAlert.

    Returns:
        Score from 0.0 (vague/generic) to 1.0 (specific/causal).
    """
    score = 0.0
    words = thesis.split()
    word_count = len(words)

    # Length: longer theses tend to be more specific
    if word_count >= _MIN_THESIS_WORDS:
        score += 0.25
    elif word_count >= 10:
        score += 0.15

    # Contains numbers (actual signal values, not just words)
    if re.search(r'\d+\.?\d*[x%]|\d+\.\d+', thesis):
        score += 0.25

    # Contains specific technical terms
    technical_terms = {
        "bollinger",
        "rsi",
        "macd",
        "volume",
        "squeeze",
        "breakout",
        "imbalance",
        "sweep",
        "iv",
        "vix",
        "support",
        "resistance",
        "divergence",
        "momentum",
        "consolidation",
        "accumulation",
    }
    term_count = sum(1 for t in technical_terms if t in thesis.lower())
    score += min(term_count * 0.1, 0.25)

    # Penalize vague phrases
    thesis_lower = thesis.lower()
    vague_count = sum(1 for p in _VAGUE_PHRASES if p in thesis_lower)
    score -= vague_count * 0.15

    # Contains causal language (because, due to, as a result, leading to)
    causal_patterns = [
        "because",
        "due to",
        "as a result",
        "leading to",
        "driven by",
        "confirmed by",
        "supported by",
        "with",
    ]
    if any(p in thesis_lower for p in causal_patterns):
        score += 0.25

    return max(0.0, min(1.0, score))


def score_rr_ratio(entry: dict[str, float], direction: str) -> float:
    """Score the reward:risk ratio of an entry setup.

    Args:
        entry: Entry dict with level, stop, target keys.
        direction: LONG, SHORT, or WATCH.

    Returns:
        Score from 0.0 (bad R:R) to 1.0 (excellent R:R >= 3:1).
    """
    if direction == "WATCH":
        return 0.5  # WATCH doesn't need R:R

    try:
        level = entry["level"]
        stop = entry["stop"]
        target = entry["target"]

        risk = abs(level - stop)
        if risk == 0:
            return 0.0
        reward = abs(target - level)
        rr = reward / risk

        if rr >= 3.0:
            return 1.0
        if rr >= _MIN_RR_RATIO:
            return 0.5 + (rr - _MIN_RR_RATIO) * 0.5
        return max(0.0, rr / _MIN_RR_RATIO * 0.5)
    except (KeyError, TypeError, ZeroDivisionError):
        return 0.0


def score_signal_coverage(sources_agree: int) -> float:
    """Score based on number of independent signal sources.

    Args:
        sources_agree: Count of distinct signal types aligned.

    Returns:
        Score from 0.0 to 1.0.
    """
    if sources_agree >= 5:
        return 1.0
    if sources_agree >= 4:
        return 0.85
    if sources_agree >= 3:
        return 0.6
    return max(0.0, sources_agree * 0.2)


def score_confidence_calibration(
    edge_probability: float,
    confidence: float,
    sources_agree: int,
) -> float:
    """Score whether edge_probability is well-calibrated to the evidence.

    Flags suspiciously high EP with few sources or low confidence.

    Args:
        edge_probability: Claimed edge probability.
        confidence: Signal confidence.
        sources_agree: Number of agreeing sources.

    Returns:
        Score from 0.0 (miscalibrated) to 1.0 (well-calibrated).
    """
    # EP > 0.90 with < 4 sources is suspicious
    if edge_probability > 0.90 and sources_agree < 4:
        return 0.3
    # EP > 0.85 with < 4 sources is slightly suspicious
    if edge_probability > 0.85 and sources_agree < 4:
        return 0.5
    # Low confidence but high EP
    if confidence < 0.75 and edge_probability > 0.80:
        return 0.4
    # Well-calibrated: higher sources → higher EP allowed
    max_reasonable_ep = min(0.70 + sources_agree * 0.05, 0.95)
    if edge_probability <= max_reasonable_ep:
        return 1.0
    # Slightly over-confident but not egregious
    return 0.7


def score_alert(alert: PlaybookAlert) -> dict[str, float]:
    """Compute all quality sub-scores for a single alert.

    Args:
        alert: Validated PlaybookAlert.

    Returns:
        Dict of score_name → score_value (all 0.0–1.0).
    """
    scores = {
        "thesis_quality": score_thesis_quality(alert.thesis),
        "rr_ratio": score_rr_ratio(alert.entry, alert.direction),
        "signal_coverage": score_signal_coverage(alert.sources_agree),
        "confidence_calibration": score_confidence_calibration(
            alert.edge_probability,
            alert.confidence,
            alert.sources_agree,
        ),
    }
    # Composite quality score: weighted average
    weights = {
        "thesis_quality": 0.25,
        "rr_ratio": 0.30,
        "signal_coverage": 0.20,
        "confidence_calibration": 0.25,
    }
    scores["composite_quality"] = sum(scores[k] * weights[k] for k in weights)
    return scores


def score_batch(alerts: list[PlaybookAlert]) -> dict[str, float]:
    """Score batch-level quality metrics for a set of alerts.

    Args:
        alerts: List of PlaybookAlert instances from one decision run.

    Returns:
        Dict of batch-level metrics.
    """
    if not alerts:
        return {"batch_diversity": 1.0, "batch_concentration": 0.0, "batch_avg_quality": 0.0}

    # Direction diversity (not all same direction)
    directions = {a.direction for a in alerts}
    direction_diversity = len(directions) / 3.0  # max 3 directions

    # Symbol concentration (alerts should be spread across symbols)
    symbols = [a.symbol for a in alerts]
    unique_ratio = len(set(symbols)) / len(symbols)

    # Average per-alert quality
    per_alert_scores = [score_alert(a)["composite_quality"] for a in alerts]
    avg_quality = sum(per_alert_scores) / len(per_alert_scores)

    return {
        "batch_diversity": min(direction_diversity + unique_ratio, 1.0) / 2.0,
        "batch_concentration": 1.0 - unique_ratio,
        "batch_avg_quality": avg_quality,
    }


def post_quality_scores(
    trace_id: str | None,
    alerts: list[PlaybookAlert],
) -> dict[str, Any]:
    """Score all alerts and post results to Langfuse.

    Args:
        trace_id: Langfuse trace ID (from pipeline trace).
        alerts: Validated PlaybookAlert list.

    Returns:
        Summary dict with per-alert and batch scores.
    """
    if not alerts:
        return {"per_alert": [], "batch": score_batch([])}

    try:
        from pipeline_tracing import add_score
    except ImportError:
        add_score = None  # type: ignore[assignment]

    per_alert_results = []
    for alert in alerts:
        scores = score_alert(alert)
        per_alert_results.append({"symbol": alert.symbol, "scores": scores})

        # Post per-alert quality score to Langfuse
        if add_score and trace_id:
            add_score(
                trace_id,
                f"alert_quality_{alert.symbol}",
                scores["composite_quality"],
                comment=(
                    f"thesis={scores['thesis_quality']:.2f} "
                    f"rr={scores['rr_ratio']:.2f} "
                    f"coverage={scores['signal_coverage']:.2f} "
                    f"calibration={scores['confidence_calibration']:.2f}"
                ),
            )

    batch = score_batch(alerts)

    # Post batch-level scores
    if add_score and trace_id:
        add_score(
            trace_id,
            "batch_avg_quality",
            batch["batch_avg_quality"],
            comment=f"avg quality across {len(alerts)} alerts",
        )
        add_score(
            trace_id,
            "batch_concentration",
            batch["batch_concentration"],
            comment="0.0=fully diverse, 1.0=all same symbol",
        )

    return {"per_alert": per_alert_results, "batch": batch}
