"""Unified telemetry package for Langfuse tracing and gate metrics."""

from telemetry.client import (
    get_client,
    get_langfuse_client,
    is_langfuse_auth_error,
    register_langfuse_failure,
    reset_client,
)
from telemetry.context import TelemetryContext, TraceContext
from telemetry.gate_metrics import (
    log_decision_gate_summary,
    log_gate_summary,
    record_gate_scores,
    record_langfuse_gate_scores,
    record_prometheus_gate_metrics,
)
from telemetry.tracing import (
    add_score,
    create_pipeline_trace,
    end_pipeline_trace,
    score,
    span_step,
    tag,
    tag_trace,
    trace,
)

__all__ = [
    "TelemetryContext",
    "TraceContext",
    "add_score",
    "create_pipeline_trace",
    "end_pipeline_trace",
    "get_client",
    "get_langfuse_client",
    "is_langfuse_auth_error",
    "log_decision_gate_summary",
    "log_gate_summary",
    "record_gate_scores",
    "record_langfuse_gate_scores",
    "record_prometheus_gate_metrics",
    "register_langfuse_failure",
    "reset_client",
    "score",
    "span_step",
    "tag",
    "tag_trace",
    "trace",
]
