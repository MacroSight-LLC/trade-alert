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
import threading
import time
from typing import Any

from models import PlaybookAlert

logger = logging.getLogger(__name__)

# Cached calibration data (refreshed per pipeline run)
_calibration_cache: dict[tuple[str, float], float] | None = None
_calibration_ts: float = 0.0
_calibration_lock = threading.Lock()
_CALIBRATION_TTL_SECS: float = 300.0  # 5-minute TTL

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

# Equity-relevant technical terms used for thesis quality scoring.
# Each term found in thesis adds +0.1 to quality (max +0.25).
_TECHNICAL_TERMS: frozenset[str] = frozenset(
    {
        # Classic TA
        "bollinger",
        "rsi",
        "macd",
        "volume",
        "squeeze",
        "breakout",
        "imbalance",
        "support",
        "resistance",
        "divergence",
        "momentum",
        "consolidation",
        "accumulation",
        "fibonacci",
        "ema",
        "sma",
        "vwap",
        "atr",
        "gap",
        "reversal",
        "trend",
        # Price action patterns
        "doji",
        "engulfing",
        "hammer",
        "channel",
        # Options / flow
        "sweep",
        "iv",
        "vix",
        "premium",
        "gamma",
        "delta",
        "oi",
        "dark pool",
        "block trade",
        # Sector / fundamental
        "rotation",
        "sector",
        "relative strength",
        "outperformance",
        "earnings",
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

    # Contains specific technical terms (module-level constant)
    # Use word boundaries to avoid substring false positives (e.g. "rsi" in "risk")
    thesis_lower = thesis.lower()
    term_count = sum(1 for t in _TECHNICAL_TERMS if re.search(rf'\b{re.escape(t)}\b', thesis_lower))
    score += min(term_count * 0.1, 0.25)

    # Penalize vague phrases
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


def _load_calibration_cache() -> dict[tuple[str, float], float]:
    """Load calibration gap data from Postgres (cached with TTL).

    Uses double-checked locking to avoid DB stampede when multiple
    threads find the cache expired simultaneously.

    Returns:
        Mapping of (direction, ep_bucket) to gap (predicted EP - actual WR).
        Positive gap means EP is over-predicting.
    """
    global _calibration_cache, _calibration_ts  # noqa: PLW0603
    # Fast path: check without lock
    now = time.monotonic()
    if _calibration_cache is not None and (now - _calibration_ts) < _CALIBRATION_TTL_SECS:
        return _calibration_cache

    with _calibration_lock:
        # Re-check inside lock (another thread may have refreshed)
        now = time.monotonic()
        if _calibration_cache is not None and (now - _calibration_ts) < _CALIBRATION_TTL_SECS:
            return _calibration_cache
        try:
            from db import get_calibration_accuracy

            rows = get_calibration_accuracy(days=60)
            _calibration_cache = {(str(r["direction"]), float(r["ep_bucket"])): float(r["gap"]) for r in rows}
        except Exception as exc:
            logger.debug("Calibration cache load failed (expected in tests): %s", exc)
            _calibration_cache = {}
        _calibration_ts = now
        return _calibration_cache


def score_historical_accuracy(
    direction: str,
    edge_probability: float,
) -> float:
    """Score based on historical EP-vs-actual-winrate calibration gap.

    Penalizes alerts in (direction, EP bucket) combos where the LLM
    has historically over-predicted the edge probability by >10%.

    Args:
        direction: LONG, SHORT, or WATCH.
        edge_probability: Claimed edge probability.

    Returns:
        Score from 0.0 (severely miscalibrated) to 1.0 (well-calibrated).
    """
    if direction == "WATCH":
        return 0.75
    cache = _load_calibration_cache()
    if not cache:
        return 0.75  # neutral when no data available
    bucket = round(edge_probability, 1)
    gap = cache.get((direction, bucket))
    if gap is None:
        return 0.75  # no data for this bucket
    # gap > 0 means over-prediction; penalize proportionally
    if gap > 0.15:
        return 0.2
    if gap > 0.10:
        return 0.4
    if gap > 0.05:
        return 0.6
    if gap < -0.05:
        return 0.9  # conservative under-prediction is good
    return 1.0


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
    """Score based on number of independent signal families aligned.

    Calibrated against the 11 recognised signal types (and up to 10
    discrete family-aligned sources after server-side SA override).

    Args:
        sources_agree: Count of aligned signal families (server-computed).

    Returns:
        Score from 0.0 to 1.0.
    """
    if sources_agree >= 5:
        return 1.0
    if sources_agree >= 4:
        return 0.85
    if sources_agree >= 3:
        return 0.6
    if sources_agree >= 2:
        return 0.4
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
    # EP > 0.90 with < 4 families aligned is suspicious
    if edge_probability > 0.90 and sources_agree < 4:
        return 0.3
    # EP > 0.85 with < 4 families is slightly suspicious
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


def score_signal_consistency(alert: PlaybookAlert) -> float:
    """Score whether the alert's signals are internally consistent.

    Detects contradictions like a LONG alert with bearish sentiment
    or a SHORT with bullish options flow. Uses the alert's
    ``sentiment_context`` and ``unusual_activity`` fields as proxies.

    Args:
        alert: Validated PlaybookAlert.

    Returns:
        Score from 0.0 (contradictions detected) to 1.0 (consistent).
    """
    score = 1.0
    direction = alert.direction

    if direction == "WATCH":
        return 0.75  # WATCH inherently has mixed signals

    sentiment = (alert.sentiment_context or "").lower()
    thesis = alert.thesis.lower()

    # Check for bull/bear contradiction in sentiment
    has_bull = "bull" in sentiment or "positive" in sentiment
    has_bear = "bear" in sentiment or "negative" in sentiment
    if has_bull and has_bear:
        score -= 0.30  # contradicting sentiment signals present

    # Direction consistency: LONG with bearish signals or SHORT with bullish
    if direction == "LONG" and ("bear" in thesis or "risk_off" in thesis or "risk-off" in thesis):
        score -= 0.20
    if direction == "SHORT" and "bull" in thesis:
        score -= 0.20

    # Macro inconsistency: LONG during "risk-off" in macro_regime
    macro = (alert.macro_regime or "").lower()
    if direction == "LONG" and "risk-off" in macro:
        score -= 0.15
    if direction == "SHORT" and "risk-on" in macro and "no headwind" in macro:
        score -= 0.15

    return max(0.0, min(1.0, score))


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
        "signal_consistency": score_signal_consistency(alert),
        "historical_accuracy": score_historical_accuracy(alert.direction, alert.edge_probability),
    }
    # Composite quality score: weighted average
    weights = {
        "thesis_quality": 0.15,
        "rr_ratio": 0.25,
        "signal_coverage": 0.15,
        "confidence_calibration": 0.15,
        "signal_consistency": 0.15,
        "historical_accuracy": 0.15,
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
        return {
            "batch_diversity": 0.0,
            "batch_concentration": 0.0,
            "batch_avg_quality": 0.0,
            "overlapping_entries": 0,
        }

    # Direction diversity — single direction with many alerts is penalised more
    directions = {a.direction for a in alerts}
    if len(directions) >= 2:
        direction_score = 1.0
    elif len(alerts) <= 2:
        direction_score = 0.5
    else:
        direction_score = 0.3

    # Symbol concentration (alerts should be spread across symbols)
    symbols = [a.symbol for a in alerts]
    unique_ratio = len(set(symbols)) / len(symbols)

    # Overlapping entry zones — flag when 2+ entries are within 2% of each other
    # Deduplicate by (symbol, level) to avoid counting the same alert twice
    seen_entry_pairs: set[tuple[str, float]] = set()
    deduped_entries: list[float] = []
    for a in alerts:
        if a.direction != "WATCH":
            lvl = a.entry.get("level", 0)
            if lvl > 0:
                pair = (a.symbol, lvl)
                if pair not in seen_entry_pairs:
                    seen_entry_pairs.add(pair)
                    deduped_entries.append(lvl)
    actionable_entries = sorted(deduped_entries)
    overlap_count = sum(
        1
        for i in range(1, len(actionable_entries))
        if actionable_entries[i - 1] > 0
        and abs(actionable_entries[i] - actionable_entries[i - 1]) / actionable_entries[i - 1] < 0.02
    )
    overlap_penalty = min(overlap_count * 0.15, 0.30)

    # Average per-alert quality
    per_alert_scores = [score_alert(a)["composite_quality"] for a in alerts]
    avg_quality = sum(per_alert_scores) / len(per_alert_scores)

    batch_diversity = max(0.0, min((direction_score + unique_ratio) / 2.0, 1.0) - overlap_penalty)

    return {
        "batch_diversity": batch_diversity,
        "batch_concentration": 1.0 - unique_ratio,
        "batch_avg_quality": avg_quality,
        "overlapping_entries": overlap_count,
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
        if add_score is not None and trace_id:
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
    if add_score is not None and trace_id:
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
