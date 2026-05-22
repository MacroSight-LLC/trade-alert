"""Core Pydantic v2 data models for the trade-alert system.

Defines Signal, Snapshot, PlaybookAlert, and TraceAnalysis per SSOT §4.
All normalizers, workflows, and the notifier import from this module.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Dict, List, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    field_validator,
    model_validator,
)


class Signal(BaseModel):
    """A single scored signal from one data source.

    Attributes:
        source: MCP or data provider that produced this signal.
        type: Canonical signal category.
        score: Directional strength from -3.0 (strong negative) to +3.0 (strong positive).
        confidence: Quality / reliability estimate from 0.0 to 1.0.
        reason: Human-readable explanation of why the signal fired.
        raw: Optional raw payload from the upstream source.
    """

    source: str
    type: Literal[
        "technical_trend",
        "volume_spike",
        "sentiment_bull",
        "sentiment_bear",
        "options_flow",
        "insider_activity",
        "relative_strength",
        "macro_risk_off",
        "catalyst_event",
        "short_interest",
        "price_forecast",
    ]
    score: float
    confidence: float
    reason: str
    raw: Dict = Field(default_factory=dict)

    @field_validator("score")
    @classmethod
    def validate_score(cls, v: float) -> float:
        """Enforce score within [-3.0, +3.0]."""
        if not -3.0 <= v <= 3.0:
            raise ValueError(f"score must be between -3.0 and +3.0, got {v}")
        return v

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        """Enforce confidence within [0.0, 1.0]."""
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"confidence must be between 0.0 and 1.0, got {v}")
        return v


class Snapshot(BaseModel):
    """A collection of signals for one symbol at one timeframe.

    Attributes:
        symbol: Ticker or asset identifier (e.g. "AAPL", "BTC-USD").
        timeframe: Candle / analysis timeframe.
        timestamp: Timezone-aware datetime; serialised as ISO 8601 UTC. ISO 8601
            strings produced by ``datetime.now(timezone.utc).isoformat()`` are
            coerced by Pydantic into ``datetime`` objects automatically.
        signals: One or more Signal objects aggregated for this symbol/timeframe.
    """

    symbol: str
    timeframe: Literal["5m", "15m", "1h", "4h", "1D"]
    timestamp: AwareDatetime
    signals: List[Signal]

    @field_validator("signals")
    @classmethod
    def validate_signals_non_empty(cls, v: list[Signal]) -> list[Signal]:
        """SSOT §4: Every Snapshot MUST contain at least one Signal."""
        if not v:
            raise ValueError("Snapshot must have at least one Signal")
        return v


class PlaybookAlert(BaseModel):
    """Final structured alert produced by the LLM decision engine.

    LLM JSON outputs MUST be validated against this model before
    sending to Discord or writing to Postgres (SSOT §4 guardrail).

    Attributes:
        symbol: Ticker or asset identifier.
        direction: Trade direction recommendation.
        edge_probability: Estimated probability of the edge (0-1).
        confidence: Overall confidence in the alert (0-1).
        timeframe: Analysis timeframe (e.g. "15m").
        thesis: Plain-English trade thesis.
        entry: Dict with keys ``level``, ``stop``, ``target`` (all finite floats).
        timeframe_rationale: Why this timeframe was chosen.
        sentiment_context: Summary of sentiment landscape.
        unusual_activity: Notable flow or activity observations.
        macro_regime: Current macro environment description.
        sources_agree: Number of independent signal types aligned.
    """

    symbol: str
    direction: Literal["LONG", "SHORT", "WATCH"]
    edge_probability: float
    confidence: float
    timeframe: str
    thesis: str
    entry: Dict[str, float]
    timeframe_rationale: str
    sentiment_context: str
    unusual_activity: List[str]
    macro_regime: str
    sources_agree: int

    @field_validator("edge_probability")
    @classmethod
    def validate_edge_probability(cls, v: float) -> float:
        """Enforce edge_probability within [0.0, 1.0]."""
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"edge_probability must be between 0.0 and 1.0, got {v}")
        return v

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        """Enforce confidence within [0.0, 1.0]."""
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"confidence must be between 0.0 and 1.0, got {v}")
        return v

    @field_validator("sources_agree")
    @classmethod
    def validate_sources_agree(cls, v: int) -> int:
        """Enforce sources_agree is non-negative."""
        if v < 0:
            raise ValueError(f"sources_agree must be >= 0, got {v}")
        return v

    @model_validator(mode="after")
    def validate_entry(self) -> "PlaybookAlert":
        """SSOT §4: entry must contain finite level/stop/target with direction-correct ordering.

        - LONG requires ``stop < level < target``.
        - SHORT requires ``target < level < stop``.
        - WATCH skips the directional ordering check.
        """
        required = {"level", "stop", "target"}
        missing = required - self.entry.keys()
        if missing:
            raise ValueError(f"entry missing required keys: {missing}")
        for key, val in self.entry.items():
            if not isinstance(val, (int, float)) or not math.isfinite(float(val)):
                raise ValueError(f"entry[{key!r}] must be a finite number, got {val!r}")
        if self.direction == "WATCH":
            return self
        level = float(self.entry["level"])
        stop = float(self.entry["stop"])
        target = float(self.entry["target"])
        if self.direction == "LONG" and not (stop < level < target):
            raise ValueError(
                f"LONG entry requires stop < level < target, got stop={stop}, level={level}, target={target}"
            )
        if self.direction == "SHORT" and not (target < level < stop):
            raise ValueError(
                f"SHORT entry requires target < level < stop, got stop={stop}, level={level}, target={target}"
            )
        return self

    @model_validator(mode="after")
    def validate_edge_vs_confidence(self) -> "PlaybookAlert":
        # Reject the logically inconsistent combination of "very confident edge"
        # paired with "almost no overall confidence". A model that returns this
        # has typically misaligned its outputs and the alert should be discarded.
        if self.edge_probability > 0.85 and self.confidence < 0.15:
            raise ValueError("edge_probability > 0.85 with confidence < 0.15 is logically inconsistent")
        return self


class TraceAnalysis(BaseModel):
    """Result of post-execution trace analysis for self-healing.

    Attributes:
        trace_id: Langfuse trace identifier.
        is_healthy: Whether the pipeline run passed all checks.
        issues: List of issues detected (empty when healthy).
        cost_usd: Total LLM cost for this trace in USD.
        latency_s: Total pipeline duration in seconds.
        llm_calls: Number of LLM invocations in the trace.
        total_tokens: Total tokens consumed across all LLM calls.
        prompt_version: Version tag of the prompts used (None when unknown).
        timestamp: Timezone-aware datetime of the analysis (None when not set).
    """

    trace_id: str
    is_healthy: bool
    issues: List[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    latency_s: float = 0.0
    llm_calls: int = 0
    total_tokens: int = 0
    prompt_version: str | None = None
    timestamp: AwareDatetime | None = None


if __name__ == "__main__":
    s = Signal(
        source="test",
        type="technical_trend",
        score=1.5,
        confidence=0.8,
        reason="BB squeeze detected",
    )
    snap = Snapshot(
        symbol="AAPL",
        timeframe="15m",
        timestamp=datetime.now(timezone.utc),
        signals=[s],
    )
    alert = PlaybookAlert(
        symbol="AAPL",
        direction="LONG",
        edge_probability=0.75,
        confidence=0.80,
        timeframe="15m",
        thesis="Bollinger Band squeeze with volume confirmation.",
        entry={"level": 185.0, "stop": 182.0, "target": 192.0},
        timeframe_rationale="15m trend aligning with 1h structure.",
        sentiment_context="Retail bullish, institutional neutral.",
        unusual_activity=["IV spike 2x avg", "options sweep $190c"],
        macro_regime="Risk-on, VIX 14, curve normal.",
        sources_agree=4,
    )
    trace = TraceAnalysis(
        trace_id="lf-abc-123",
        is_healthy=True,
        cost_usd=0.012,
        latency_s=4.1,
        llm_calls=2,
        total_tokens=8421,
        prompt_version="decision-v3",
        timestamp=datetime.now(timezone.utc),
    )
    print("Signal:", s.model_dump())
    print("Snapshot:", snap.model_dump())
    print("Alert:", alert.model_dump())
    print("TraceAnalysis:", trace.model_dump())
    print("All models valid ✅")
