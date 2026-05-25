"""Compatibility shim — deprecated; import from ``telemetry.tracing`` instead.

.. deprecated::
    Use ``from telemetry import score, span_step, trace`` in new code.
    This module remains for existing callers until migration completes.
"""

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
