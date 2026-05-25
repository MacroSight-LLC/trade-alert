"""Compatibility shim — use ``telemetry.gate_metrics`` for new code."""

from telemetry.gate_metrics import (  # noqa: F401
    log_decision_gate_summary,
    log_gate_summary,
    record_gate_scores,
    record_langfuse_gate_scores,
    record_prometheus_gate_metrics,
)

__all__ = [
    "log_decision_gate_summary",
    "log_gate_summary",
    "record_gate_scores",
    "record_langfuse_gate_scores",
    "record_prometheus_gate_metrics",
]
