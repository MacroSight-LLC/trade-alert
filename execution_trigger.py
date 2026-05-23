"""External wire contract for downstream execution integration.

Defines the versioned ExecutionTriggerV1 payload that trade-alert sends
to trade-execute via outbound webhook.  This is the stable external wire
format — internal PlaybookAlert fields are never exposed directly.

Use execution_mapper.map_to_execution_trigger() to derive an
ExecutionTriggerV1 from a validated PlaybookAlert.

See README.md §Downstream Execution Integration.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ConvictionBand = Literal["watch", "low", "base", "high", "extreme"]
AlertClass = Literal["execute", "watch", "info"]

# Timeframe-aware execution TTL defaults (seconds).
_EXECUTION_EXPIRY_BY_TIMEFRAME: dict[str, int] = {
    "5m": 180,
    "15m": 240,  # 4 minutes
    "1h": 900,  # 15 minutes
    "4h": 1800,
    "1D": 3600,
}
_DEFAULT_EXECUTION_EXPIRY_SECONDS = 900


def execution_expiry_seconds_for_timeframe(timeframe: str) -> int:
    """Return execution payload TTL in seconds for *timeframe*."""
    return _EXECUTION_EXPIRY_BY_TIMEFRAME.get(timeframe, _DEFAULT_EXECUTION_EXPIRY_SECONDS)


def is_execution_expired(expires_at: str, *, now: datetime | None = None) -> bool:
    """Return True when *expires_at* (ISO 8601 UTC) is in the past."""
    reference = now if now is not None else datetime.now(UTC)
    expiry = datetime.fromisoformat(expires_at)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return reference > expiry


def should_dispatch_execution(
    *,
    expires_at: str,
    idempotency_key: str,
    already_dispatched: bool = False,
) -> bool:
    """Return False when dispatch should be skipped (expired or duplicate).

    Logs the skip reason at INFO level.
    """
    if already_dispatched:
        logger.info(
            "Duplicate execution dispatch suppressed: idempotency_key=%s",
            idempotency_key,
        )
        return False
    if is_execution_expired(expires_at):
        logger.info(
            "Alert expired, not dispatching: idempotency_key=%s expires_at=%s",
            idempotency_key,
            expires_at,
        )
        return False
    return True


class EntryV1(BaseModel):
    """Normalized execution-relevant pricing fields for downstream consumers."""

    price: float
    stop: float
    target: float
    risk_reward: float = Field(
        description="Reward:Risk ratio. 0.0 for WATCH alerts or when risk is zero."
    )


class ExecutionTriggerV1(BaseModel):
    """Versioned outbound event contract from trade-alert to trade-execute.

    This is the stable external wire format.  Do not add raw internal
    PlaybookAlert fields here.  Bump the version literal when making
    breaking changes.

    Attributes:
        version: Fixed at "v1" for this schema generation.
        event_id: UUID4 string, unique per trigger instance.  Use for dedup.
        correlation_id: UUID5 string, deterministic per alert content.  Stable
            across retries of the same logical alert.
        source: Fixed at "trade-alert" — identifies the upstream producer.
        generated_at: ISO 8601 UTC timestamp when this trigger was created.
        expires_at: ISO 8601 UTC timestamp after which this trigger is stale.
        symbol: Ticker or asset identifier (e.g. "AAPL", "BTC-USD").
        direction: Trade direction recommendation.
        alert_class: Execution classification. "execute" is actionable;
            "watch" is non-executable context.
        entry: Normalized execution pricing fields.
        timeframe: Analysis timeframe (e.g. "15m", "1h").
        strategy_id: Human-readable strategy identifier derived from timeframe.
        conviction_score: Composite quality score (edge_probability × confidence),
            normalized to [0.0, 1.0].
        conviction_band: Categorical conviction tier derived from conviction_score.
        thesis_summary: Plain-English trade thesis from the upstream LLM decision.
        metadata: Non-breaking additional context for downstream consumers.
    """

    version: Literal["v1"] = "v1"
    event_id: str
    correlation_id: str
    source: Literal["trade-alert"] = "trade-alert"
    generated_at: str
    expires_at: str
    symbol: str
    direction: Literal["LONG", "SHORT", "WATCH"]
    alert_class: AlertClass
    entry: EntryV1
    timeframe: str
    strategy_id: str
    conviction_score: float = Field(ge=0.0, le=1.0)
    conviction_band: ConvictionBand
    thesis_summary: str
    metadata: dict[str, Any] = Field(default_factory=dict)
