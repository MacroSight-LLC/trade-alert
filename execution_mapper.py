"""Translator from PlaybookAlert to execution wire contracts.

This is the sole mapping layer between trade-alert's internal model
and the versioned external wire contract.  All conviction interpretation
and normalization lives here so that trade-execute never needs to
recompute edge-quality logic from raw fields.

See README.md §Downstream Execution Integration.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from constants import TRADE_EXECUTE_EXPIRY_SECONDS
from execution_trigger import (
    AlertClass,
    ConvictionBand,
    EntryV1,
    ExecutionTriggerV1,
    execution_expiry_seconds_for_timeframe,
)
from models import PlaybookAlert


class ExecutionPayload(BaseModel):
    """Versioned execution payload for trade-execute (schema 1.0).

    Frozen after construction — safe to pass across dispatch boundaries.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    idempotency_key: str
    alert_id: str
    symbol: str
    direction: Literal["LONG", "SHORT"]
    timeframe: str
    entry_level: float
    stop_level: float
    target_level: float
    edge_probability: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    sources_agree: int = Field(ge=0)
    regime: str
    vix: float = Field(ge=0.0)
    created_at: str
    expires_at: str
    pipeline_trace_id: str | None = None

    @model_validator(mode="after")
    def validate_price_ordering(self) -> ExecutionPayload:
        """Enforce direction-correct stop/entry/target ordering."""
        if self.direction == "LONG" and not (self.stop_level < self.entry_level < self.target_level):
            raise ValueError(
                f"LONG requires stop < entry < target, got "
                f"stop={self.stop_level}, entry={self.entry_level}, target={self.target_level}"
            )
        if self.direction == "SHORT" and not (self.target_level < self.entry_level < self.stop_level):
            raise ValueError(
                f"SHORT requires target < entry < stop, got "
                f"stop={self.stop_level}, entry={self.entry_level}, target={self.target_level}"
            )
        return self


_VIX_PATTERN = re.compile(r"VIX\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def _parse_vix_from_macro(macro_regime: str) -> float:
    """Extract VIX level from macro_regime text; return 0.0 when absent."""
    match = _VIX_PATTERN.search(macro_regime or "")
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:
        return 0.0


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


def _stable_correlation_content(alert: PlaybookAlert) -> str:
    """Return deterministic content string for correlation / idempotency keys."""
    return f"{alert.symbol}:{alert.direction}:{alert.timeframe}:{alert.thesis[:100]}"


def map_to_execution_payload(
    alert: PlaybookAlert,
    *,
    alert_id: str,
    idempotency_key: str,
    pipeline_trace_id: str | None = None,
    expiry_seconds: int | None = None,
    vix: float | None = None,
) -> ExecutionPayload:
    """Translate a validated PlaybookAlert to an ExecutionPayload.

    Only LONG and SHORT alerts are executable; WATCH raises ValueError.

    Args:
        alert: A fully-validated PlaybookAlert from the decision engine.
        alert_id: Postgres alert row id as string.
        idempotency_key: UUID persisted on the alert row for dedup.
        pipeline_trace_id: Optional Langfuse trace id.
        expiry_seconds: Override TTL; defaults to timeframe-aware value.
        vix: Optional VIX override; parsed from macro_regime when omitted.

    Returns:
        ExecutionPayload ready for serialisation and outbound delivery.
    """
    if alert.direction == "WATCH":
        raise ValueError("WATCH alerts are not executable")

    now = datetime.now(UTC)
    ttl = (
        expiry_seconds
        if expiry_seconds is not None
        else execution_expiry_seconds_for_timeframe(alert.timeframe)
    )
    expires = now + timedelta(seconds=ttl)

    return ExecutionPayload(
        idempotency_key=idempotency_key,
        alert_id=alert_id,
        symbol=alert.symbol,
        direction=alert.direction,
        timeframe=alert.timeframe,
        entry_level=float(alert.entry["level"]),
        stop_level=float(alert.entry["stop"]),
        target_level=float(alert.entry["target"]),
        edge_probability=alert.edge_probability,
        confidence=alert.confidence,
        sources_agree=alert.sources_agree,
        regime=alert.macro_regime,
        vix=vix if vix is not None else _parse_vix_from_macro(alert.macro_regime),
        created_at=now.isoformat(),
        expires_at=expires.isoformat(),
        pipeline_trace_id=pipeline_trace_id,
    )


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
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=expiry_seconds)

    correlation_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, _stable_correlation_content(alert)))

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
