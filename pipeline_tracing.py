"""Compatibility shim — use ``telemetry.tracing`` for new code."""

from telemetry.tracing import (  # noqa: F401
    add_score,
    create_pipeline_trace,
    end_pipeline_trace,
    get_langfuse_client,
    score,
    span_step,
    tag,
    tag_trace,
    trace,
)

__all__ = [
    "add_score",
    "create_pipeline_trace",
    "end_pipeline_trace",
    "get_langfuse_client",
    "score",
    "span_step",
    "tag",
    "tag_trace",
    "trace",
]
