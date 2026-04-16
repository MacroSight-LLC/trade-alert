"""External wire contract for downstream execution integration.

Defines the versioned ExecutionTriggerV1 payload that trade-alert sends
to trade-execute via outbound webhook.  This is the stable external wire
format — internal PlaybookAlert fields are never exposed directly.

Use execution_mapper.map_to_execution_trigger() to derive an
ExecutionTriggerV1 from a validated PlaybookAlert.

See README.md §Downstream Execution Integration.
"""

from __future__ import annotations

from typing import Any, Dict, Literal

from pydantic import BaseModel, Field

ConvictionBand = Literal["watch", "low", "base", "high", "extreme"]
AlertClass = Literal["execute", "watch", "info"]


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
    metadata: Dict[str, Any] = Field(default_factory=dict)
