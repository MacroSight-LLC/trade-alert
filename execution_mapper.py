"""Translator from PlaybookAlert to ExecutionTriggerV1.

This is the sole mapping layer between trade-alert's internal model
and the versioned external wire contract.  All conviction interpretation
and normalization lives here so that trade-execute never needs to
recompute edge-quality logic from raw fields.

See README.md §Downstream Execution Integration.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from constants import TRADE_EXECUTE_EXPIRY_SECONDS
from execution_trigger import AlertClass, ConvictionBand, EntryV1, ExecutionTriggerV1
from models import PlaybookAlert


def _compute_risk_reward(alert: PlaybookAlert) -> float:
    """Compute the reward:risk ratio for the alert entry dict.

    Returns 0.0 for WATCH alerts or when the stop equals the entry price.
    """
    if alert.direction == "WATCH":
        return 0.0
    try:
        level = alert.entry["level"]
        stop = alert.entry["stop"]
        target = alert.entry["target"]
        if alert.direction == "LONG":
            risk = level - stop
            reward = target - level
        else:  # SHORT
            risk = stop - level
            reward = level - target
        if risk <= 0:
            return 0.0
        return round(reward / risk, 4)
    except (KeyError, ZeroDivisionError, TypeError):
        return 0.0


def _conviction_band(score: float, direction: str) -> ConvictionBand:
    """Map a composite conviction score to a categorical band.

    Conviction bands are derived from edge_probability × confidence so
    that downstream consumers can act on a stable category without
    recomputing raw fields.

    Args:
        score: edge_probability × confidence, [0, 1].
        direction: "LONG", "SHORT", or "WATCH".

    Returns:
        ConvictionBand literal.
    """
    if direction == "WATCH":
        return "watch"
    if score >= 0.70:
        return "extreme"
    if score >= 0.60:
        return "high"
    if score >= 0.50:
        return "base"
    return "low"


def _alert_class(direction: str) -> AlertClass:
    """Map direction to alert_class.

    WATCH alerts are explicitly non-executable; LONG and SHORT are
    execute-class.
    """
    if direction == "WATCH":
        return "watch"
    return "execute"


def map_to_execution_trigger(
    alert: PlaybookAlert,
    expiry_seconds: int = TRADE_EXECUTE_EXPIRY_SECONDS,
) -> ExecutionTriggerV1:
    """Translate a validated PlaybookAlert to an ExecutionTriggerV1.

    The event_id is a fresh UUID4 on each call (unique per delivery
    attempt).  The correlation_id is a deterministic UUID5 derived from
    alert content so that retries of the same logical alert share a
    stable identifier for downstream deduplication.

    Args:
        alert: A fully-validated PlaybookAlert from the decision engine.
        expiry_seconds: Seconds until the trigger expires.  Defaults to
            TRADE_EXECUTE_EXPIRY_SECONDS (900s, matching the dedup window).

    Returns:
        ExecutionTriggerV1 ready for serialisation and outbound delivery.
    """
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=expiry_seconds)

    # Stable correlation key from alert content.  Safe retry dedup anchor.
    _corr_content = f"{alert.symbol}:{alert.direction}:{alert.timeframe}:{alert.thesis[:100]}"
    correlation_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, _corr_content))

    conviction_score = round(alert.edge_probability * alert.confidence, 4)

    return ExecutionTriggerV1(
        event_id=str(uuid.uuid4()),
        correlation_id=correlation_id,
        generated_at=now.isoformat(),
        expires_at=expires.isoformat(),
        symbol=alert.symbol,
        direction=alert.direction,
        alert_class=_alert_class(alert.direction),
        entry=EntryV1(
            price=alert.entry["level"],
            stop=alert.entry["stop"],
            target=alert.entry["target"],
            risk_reward=_compute_risk_reward(alert),
        ),
        timeframe=alert.timeframe,
        strategy_id=f"cuga-playbook-{alert.timeframe}",
        conviction_score=conviction_score,
        conviction_band=_conviction_band(conviction_score, alert.direction),
        thesis_summary=alert.thesis,
        metadata={
            "sources_agree": alert.sources_agree,
            "macro_regime": alert.macro_regime,
            "sentiment_context": alert.sentiment_context,
            "unusual_activity": alert.unusual_activity,
            "timeframe_rationale": alert.timeframe_rationale,
        },
    )
