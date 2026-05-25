"""Pipeline-level Langfuse tracing for all pipeline steps."""

from __future__ import annotations

import functools
import hashlib
import logging
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, TypeVar

from telemetry.client import get_client, register_langfuse_failure

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., Any])


def create_pipeline_trace(
    timeframe: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> str | None:
    """Create a root Langfuse trace for one orchestrator run."""
    lf = get_client()
    if lf is None:
        return None

    try:
        trace = lf.trace(
            name=f"pipeline-{timeframe}",
            session_id=f"orchestrator-{timeframe}",
            metadata=metadata or {},
            tags=[f"timeframe:{timeframe}", "pipeline"],
        )
        trace_id: str = trace.id
        logger.info(
            "Created pipeline trace %s (session=orchestrator-%s)",
            trace_id,
            timeframe,
        )
        return trace_id
    except (ConnectionError, OSError, ValueError, RuntimeError) as exc:
        register_langfuse_failure(exc)
        return None


@contextmanager
def span_step(
    trace_id: str | None,
    name: str,
    *,
    input_data: Any = None,
    level: str = "DEFAULT",
) -> Generator[dict[str, Any], None, None]:
    """Context manager that wraps a pipeline step in a Langfuse span."""
    ctx: dict[str, Any] = {"output": None, "status_message": "ok", "level": level}

    if trace_id is None:
        yield ctx
        return

    lf = get_client()
    if lf is None:
        yield ctx
        return

    start = time.monotonic()
    start_ts = datetime.now(tz=UTC)
    span = None
    try:
        span = lf.trace(id=trace_id).span(
            name=name,
            start_time=start_ts,
            input=input_data,
            level=level,
        )
    except (ConnectionError, OSError, ValueError, RuntimeError) as exc:
        register_langfuse_failure(exc)
        logger.warning("Failed to open span '%s': %s", name, exc)

    try:
        yield ctx
    finally:
        elapsed = time.monotonic() - start
        if span is not None:
            try:
                span.end(
                    end_time=datetime.now(tz=UTC),
                    output=ctx.get("output"),
                    status_message=ctx.get("status_message", "ok"),
                    level=ctx.get("level", level),
                    metadata={"duration_s": round(elapsed, 3)},
                )
            except (ConnectionError, OSError, ValueError, RuntimeError) as exc:
                register_langfuse_failure(exc)
                logger.warning("Failed to close span '%s': %s", name, exc)

        if trace_id is not None and name.startswith("run-collector"):
            try:
                collector_name = name.replace("run-collectors-", "").replace("run-collector-", "")
                score(
                    trace_id,
                    f"collector_latency_{collector_name}",
                    round(elapsed, 3),
                    comment=f"{collector_name} completed in {elapsed:.3f}s",
                )
            except (ConnectionError, OSError, RuntimeError):
                pass


def score(
    trace_id: str | None,
    name: str,
    value: float,
    *,
    comment: str = "",
) -> None:
    """Post a numeric score to a Langfuse trace."""
    if trace_id is None:
        return
    lf = get_client()
    if lf is None:
        return
    try:
        score_id = hashlib.sha256(f"{trace_id}:{name}".encode()).hexdigest()[:32]
        lf.score(id=score_id, trace_id=trace_id, name=name, value=value, comment=comment)
    except (ConnectionError, OSError, ValueError, RuntimeError) as exc:
        register_langfuse_failure(exc)
        logger.warning("Failed to post score '%s' to trace %s: %s", name, trace_id, exc)


def tag(trace_id: str | None, tags: list[str]) -> None:
    """Append tags to an existing trace."""
    if trace_id is None:
        return
    lf = get_client()
    if lf is None:
        return
    try:
        lf.trace(id=trace_id, tags=tags)
    except (ConnectionError, OSError, ValueError, RuntimeError) as exc:
        register_langfuse_failure(exc)
        logger.warning("Failed to tag trace %s: %s", trace_id, exc)


def end_pipeline_trace(
    trace_id: str | None,
    *,
    output: Any = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Finalise a pipeline trace with output and optional metadata."""
    if trace_id is None:
        return

    lf = get_client()
    if lf is None:
        return

    try:
        lf.trace(id=trace_id).update(
            output=output,
            metadata=metadata or {},
        )
        lf.flush()
        logger.info("Finalised pipeline trace %s", trace_id)
    except (ConnectionError, OSError, ValueError, RuntimeError) as exc:
        register_langfuse_failure(exc)
        logger.warning("Failed to finalise pipeline trace: %s", exc)


def trace(name: str | None = None) -> Callable[[_F], _F]:
    """Decorator wrapping a callable in a Langfuse span when ``trace_id`` is passed."""

    def decorator(fn: _F) -> _F:
        step_name = name or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            trace_id = kwargs.get("trace_id")
            with span_step(trace_id, step_name) as ctx:
                result = fn(*args, **kwargs)
                ctx["output"] = result
                return result

        return wrapper  # type: ignore[return-value]

    return decorator


# Backward-compatible aliases
add_score = score
tag_trace = tag
get_langfuse_client = get_client
