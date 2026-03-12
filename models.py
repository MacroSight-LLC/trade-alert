"""Core Pydantic v2 data models for the trade-alert system.

Defines Signal, Snapshot, and PlaybookAlert per SSOT §4.
All normalizers, workflows, and the notifier import from this module.
"""

from __future__ import annotations

from typing import Dict, List, Literal

from pydantic import BaseModel, Field, field_validator


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
        "order_imbalance_long",
        "order_imbalance_short",
        "macro_risk_off",
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
        timestamp: ISO 8601 UTC timestamp of when the snapshot was created.
        signals: One or more Signal objects aggregated for this symbol/timeframe.
    """

    symbol: str
    timeframe: Literal["5m", "15m", "1h", "4h", "1D"]
    timestamp: str
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
        entry: Dict with keys ``level``, ``stop``, ``target`` (all floats).
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

    @field_validator("entry")
    @classmethod
    def validate_entry_keys(cls, v: dict[str, float]) -> dict[str, float]:
        """SSOT §4: entry must contain level, stop, target."""
        required = {"level", "stop", "target"}
        missing = required - v.keys()
        if missing:
            raise ValueError(f"entry missing required keys: {missing}")
        return v


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
        prompt_version: Version tag of the prompts used.
        timestamp: ISO 8601 UTC timestamp of the analysis.
    """

    trace_id: str
    is_healthy: bool
    issues: List[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    latency_s: float = 0.0
    llm_calls: int = 0
    total_tokens: int = 0
    prompt_version: str = "v1.0"
    timestamp: str = ""


if __name__ == "__main__":
    # Smoke test — create one of each model with valid data and print
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
        timestamp="2026-03-06T00:00:00Z",
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
    print("Signal:", s.model_dump())
    print("Snapshot:", snap.model_dump())
    print("Alert:", alert.model_dump())
    print("All models valid ✅")
