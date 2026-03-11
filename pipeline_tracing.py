"""Pipeline-level Langfuse tracing for non-LLM steps.

Creates a root trace per orchestrator run and wraps collector,
merger, and notifier steps in named spans so the full pipeline
is visible in the Langfuse timeline — not just the LLM calls.

Also fixes the session_id linkage: the root trace is tagged with
``session_id = "orchestrator-{timeframe}"`` so
:func:`trace_analyzer.fetch_latest_trace` can locate it.

All functions degrade to no-ops when Langfuse is not configured.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

from langfuse_client import get_langfuse_client

logger = logging.getLogger(__name__)


def create_pipeline_trace(
    timeframe: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> str | None:
    """Create a root Langfuse trace for one orchestrator run.

    Args:
        timeframe: Pipeline timeframe (``"15m"`` or ``"1h"``).
        metadata: Extra metadata dict attached to the trace.

    Returns:
        The Langfuse trace ID, or ``None`` if Langfuse is unavailable.
    """
    lf = get_langfuse_client()
    if lf is None:
        return None

    try:
        trace = lf.trace(
            name=f"pipeline-{timeframe}",
            session_id=f"orchestrator-{timeframe}",
            metadata=metadata or {},
        )
        trace_id: str = trace.id
        logger.info(
            "Created pipeline trace %s (session=orchestrator-%s)",
            trace_id,
            timeframe,
        )
        return trace_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to create pipeline trace: %s", exc)
        return None


@contextmanager
def span_step(
    trace_id: str | None,
    name: str,
    *,
    input_data: Any = None,
) -> Generator[dict[str, Any], None, None]:
    """Context manager that wraps a pipeline step in a Langfuse span.

    Usage::

        with span_step(trace_id, "run-collectors") as ctx:
            # ... do work ...
            ctx["output"] = {"symbols": 42}

    Args:
        trace_id: Parent trace ID (from :func:`create_pipeline_trace`).
            If ``None`` the block executes without tracing.
        name: Human-readable step name shown in the Langfuse timeline.
        input_data: Optional input payload recorded on the span.

    Yields:
        A mutable dict where callers can set ``output`` and
        ``status_message`` before the span closes.
    """
    ctx: dict[str, Any] = {"output": None, "status_message": "ok"}

    if trace_id is None:
        yield ctx
        return

    lf = get_langfuse_client()
    if lf is None:
        yield ctx
        return

    start = time.monotonic()
    start_ts = datetime.now(tz=timezone.utc)
    span = None
    try:
        span = lf.trace(id=trace_id).span(
            name=name,
            start_time=start_ts,
            input=input_data,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to open span '%s': %s", name, exc)

    try:
        yield ctx
    finally:
        elapsed = time.monotonic() - start
        if span is not None:
            try:
                span.end(
                    end_time=datetime.now(tz=timezone.utc),
                    output=ctx.get("output"),
                    status_message=ctx.get("status_message", "ok"),
                    metadata={"duration_s": round(elapsed, 3)},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to close span '%s': %s", name, exc)


def end_pipeline_trace(
    trace_id: str | None,
    *,
    output: Any = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Finalise a pipeline trace with output and optional metadata.

    Args:
        trace_id: The trace ID to update (from :func:`create_pipeline_trace`).
        output: Final pipeline output (e.g. alert count, health status).
        metadata: Additional metadata to merge onto the trace.
    """
    if trace_id is None:
        return

    lf = get_langfuse_client()
    if lf is None:
        return

    try:
        lf.trace(id=trace_id).update(
            output=output,
            metadata=metadata or {},
        )
        lf.flush()
        logger.info("Finalised pipeline trace %s", trace_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to finalise pipeline trace: %s", exc)
