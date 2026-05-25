"""Telemetry context objects for pipeline and gate scoring."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from telemetry.tracing import create_pipeline_trace, end_pipeline_trace, score, tag

ScoreFn = Callable[[str, str, float, str], None]


@dataclass
class TelemetryContext:
    """Per-run Langfuse score/tag helper bound to a trace ID."""

    trace_id: str | None = None
    _score_fn: ScoreFn | None = field(default=None, repr=False)

    @property
    def enabled(self) -> bool:
        return self.trace_id is not None

    def score(self, name: str, value: float, *, comment: str = "") -> None:
        if not self.trace_id:
            return
        if self._score_fn is not None:
            self._score_fn(self.trace_id, name, value, comment)
            return
        score(self.trace_id, name, value, comment=comment)

    def tag(self, tags: list[str]) -> None:
        tag(self.trace_id, tags)

    def as_score_fn(self) -> ScoreFn | None:
        """Legacy callback shape for ``parse_llm_alerts`` and similar callers."""
        if not self.enabled:
            return None
        tid = self.trace_id
        assert tid is not None

        def _add_score(_trace_id: str, name: str, value: float, comment: str = "") -> None:
            self.score(name, value, comment=comment)

        return _add_score

    @classmethod
    def noop(cls) -> TelemetryContext:
        return cls(trace_id=None)

    @classmethod
    def for_trace(cls, trace_id: str | None) -> TelemetryContext:
        return cls(trace_id=trace_id)

    @classmethod
    def with_callback(
        cls,
        trace_id: str | None,
        add_score_fn: ScoreFn | None,
    ) -> TelemetryContext:
        """Bind an explicit score callback (used by tests and legacy callers)."""
        return cls(trace_id=trace_id, _score_fn=add_score_fn)


@dataclass
class TraceContext:
    """Root pipeline trace lifecycle (create → work → finalise)."""

    trace_id: str | None = None
    timeframe: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    @contextmanager
    def pipeline(
        cls,
        timeframe: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Generator[TraceContext, None, None]:
        meta = metadata or {}
        trace_id = create_pipeline_trace(timeframe, metadata=meta)
        ctx = cls(trace_id=trace_id, timeframe=timeframe, metadata=meta)
        try:
            yield ctx
        finally:
            end_pipeline_trace(trace_id, metadata=ctx.metadata)

    @property
    def telemetry(self) -> TelemetryContext:
        return TelemetryContext.for_trace(self.trace_id)
