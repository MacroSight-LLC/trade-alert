"""Backward-compatible shim — canonical code in telemetry/datasets.py."""

from telemetry.datasets import (  # noqa: F401
    DATASET_NAME,
    GOLDEN_DATASET_NAME,
    LANGFUSE_DATASET_CAPTURE_ENABLED,
    LANGFUSE_DATASET_RETENTION_DAYS,
    auto_promote_to_golden,
    capture_decision_run,
    get_golden_examples,
    promote_to_golden,
)

__all__ = [
    "DATASET_NAME",
    "GOLDEN_DATASET_NAME",
    "LANGFUSE_DATASET_CAPTURE_ENABLED",
    "LANGFUSE_DATASET_RETENTION_DAYS",
    "auto_promote_to_golden",
    "capture_decision_run",
    "get_golden_examples",
    "promote_to_golden",
]
