"""Langfuse dataset capture for continuous prompt improvement.

Captures every decision engine run (input signals + LLM output) as a
Langfuse dataset item so you can:
1. Build golden evaluation sets from real production data
2. Run prompt regression tests before deploying new versions
3. Identify patterns in high-quality vs. low-quality outputs
4. Track prompt version performance over time

Dataset names:
    ``decision-runs``   — all decision inputs/outputs (auto-captured)
    ``decision-golden`` — manually curated high-quality examples

Usage from decision workflows:
    from langfuse_datasets import capture_decision_run
    capture_decision_run(timeframe, snapshots_json, llm_response, alerts, quality)

Canonical implementation: ``telemetry.datasets``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

from telemetry.client import get_langfuse_client, register_langfuse_failure

logger = logging.getLogger(__name__)

DATASET_NAME = "decision-runs"
GOLDEN_DATASET_NAME = "decision-golden"
LANGFUSE_DATASET_CAPTURE_ENABLED: bool = os.environ.get("LANGFUSE_DATASET_CAPTURE_ENABLED", "1") == "1"
LANGFUSE_DATASET_RETENTION_DAYS: int = int(os.environ.get("LANGFUSE_DATASET_RETENTION_DAYS", "90"))

_REDACT_KEYS = frozenset({"api_key", "token", "secret", "password", "authorization"})
_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")


def _redact_snapshot(data: Any) -> Any:
    """Strip sensitive fields before Langfuse dataset capture."""
    if isinstance(data, dict):
        return {
            k: "[REDACTED]" if k.lower() in _REDACT_KEYS else _redact_snapshot(v) for k, v in data.items()
        }
    if isinstance(data, list):
        return [_redact_snapshot(item) for item in data]
    if isinstance(data, str):
        return _EMAIL_RE.sub("[REDACTED]", data)
    return data


def _list_dataset_names(lf: Any) -> set[str] | None:
    """Return visible dataset names, or ``None`` on API failure."""
    try:
        response = lf.api.datasets.list(page=1, limit=100)
        data = getattr(response, "data", None) or []
        return {str(item.name) for item in data if getattr(item, "name", None)}
    except Exception as exc:  # noqa: BLE001 — Langfuse is optional; callers decide whether to continue
        register_langfuse_failure(exc)
        logger.warning("Failed to list Langfuse datasets: %s", exc)
        return None


def _ensure_dataset(lf: Any, name: str) -> bool:
    """Create the dataset if it doesn't exist yet.

    Args:
        lf: Langfuse client instance.
        name: Dataset name to create.

    Returns:
        True if dataset exists or was created, False on error.
    """
    dataset_names = _list_dataset_names(lf)
    if dataset_names is None:
        return False

    if name in dataset_names:
        return True

    try:
        lf.create_dataset(name=name, description=f"Auto-captured {name} for trade-alert")
        logger.info("Created Langfuse dataset: %s", name)
        return True
    except Exception as exc:  # noqa: BLE001 — Langfuse is optional; any failure must not halt the pipeline
        register_langfuse_failure(exc)
        logger.warning("Failed to create dataset %s: %s", name, exc)
        return False


def capture_decision_run(
    timeframe: str,
    snapshots_json: str,
    llm_response: str,
    alerts_json: str,
    quality_report: dict[str, Any] | None = None,
    *,
    trace_id: str | None = None,
    prompt_version: str = "unknown",
) -> None:
    """Capture a decision engine run as a Langfuse dataset item.

    Called from the decision workflow validate-and-filter step after
    quality scoring. Stores the full context needed to evaluate and
    reproduce the decision.

    Args:
        timeframe: Pipeline timeframe (15m/1h).
        snapshots_json: Raw signal snapshots sent to the LLM.
        llm_response: Raw LLM response string.
        alerts_json: Validated alerts JSON after gate filtering.
        quality_report: Quality scoring results from alert_quality.py.
        trace_id: Langfuse trace ID for linking.
        prompt_version: Prompt version used for this run.
    """
    if not LANGFUSE_DATASET_CAPTURE_ENABLED:
        return

    lf = get_langfuse_client()
    if lf is None:
        return

    if not _ensure_dataset(lf, DATASET_NAME):
        return

    now = datetime.now(tz=UTC)
    item_id = f"{timeframe}-{now.strftime('%Y%m%dT%H%M%S')}"

    try:
        # Parse alerts for the expected output
        if isinstance(alerts_json, list):
            parsed_alerts = alerts_json
        else:
            try:
                parsed_alerts = json.loads(alerts_json)
            except (json.JSONDecodeError, TypeError):
                parsed_alerts = []

        raw_snapshots = json.loads(snapshots_json) if isinstance(snapshots_json, str) else snapshots_json
        input_data = _redact_snapshot(
            {
                "timeframe": timeframe,
                "snapshots": raw_snapshots,
                "prompt_version": prompt_version,
                "timestamp": now.isoformat(),
                "llm_raw_response": llm_response,
            }
        )

        expected_output = {
            "alerts": parsed_alerts,
            "alert_count": len(parsed_alerts),
        }

        metadata: dict[str, Any] = {
            "trace_id": trace_id or "",
            "prompt_version": prompt_version,
            "timeframe": timeframe,
        }
        if quality_report:
            metadata["quality"] = quality_report.get("batch", {})
            metadata["per_alert_quality"] = quality_report.get("per_alert", [])

        lf.create_dataset_item(
            dataset_name=DATASET_NAME,
            input=input_data,
            expected_output=expected_output,
            metadata=metadata,
        )
        lf.flush()
        logger.info(
            "Captured decision run to dataset '%s': %s (%d alerts)",
            DATASET_NAME,
            item_id,
            len(parsed_alerts),
        )
    except Exception as exc:  # noqa: BLE001 — dataset capture is best-effort; never block the pipeline
        register_langfuse_failure(exc)
        logger.warning("Failed to capture dataset item: %s", exc)


def promote_to_golden(
    dataset_item_id: str,
    expected_output: dict[str, Any] | None = None,
) -> None:
    """Copy a decision-runs item to the golden evaluation set.

    Call this from the Langfuse UI or a manual review script when
    you identify a high-quality example to use for regression testing.

    Args:
        dataset_item_id: ID of the source item in decision-runs.
        expected_output: Override expected output (e.g. after human review).
    """
    lf = get_langfuse_client()
    if lf is None:
        return

    try:
        source = lf.get_dataset_item(dataset_item_id)
        _ensure_dataset(lf, GOLDEN_DATASET_NAME)
        lf.create_dataset_item(
            dataset_name=GOLDEN_DATASET_NAME,
            input=source.input,
            expected_output=expected_output or source.expected_output,
            metadata={**(source.metadata or {}), "source_item_id": dataset_item_id},
        )
        logger.info("Promoted item %s to golden dataset", dataset_item_id)
    except Exception as exc:  # noqa: BLE001 — golden-set promotion is advisory; swallow all errors
        register_langfuse_failure(exc)
        logger.warning("Failed to promote to golden dataset: %s", exc)


def get_golden_examples(n: int = 3) -> list[dict[str, Any]]:
    """Fetch recent high-quality examples from the golden dataset.

    Used to inject few-shot examples into the decision prompt so the
    LLM can match the specificity and calibration of production alerts.

    Args:
        n: Maximum number of examples to return.

    Returns:
        List of example dicts with ``input`` and ``expected_output`` keys,
        or empty list if golden dataset is unavailable or empty.
    """
    lf = get_langfuse_client()
    if lf is None:
        return []

    try:
        dataset = lf.get_dataset(GOLDEN_DATASET_NAME)
        items = dataset.items or []
        # Take the most recent N items
        recent = items[-n:] if len(items) > n else items
        examples = []
        for item in recent:
            output = item.expected_output or {}
            alerts = output.get("alerts", [])
            if alerts:
                examples.append(
                    {
                        "input": item.input,
                        "expected_output": output,
                    }
                )
        return examples
    except Exception as exc:  # noqa: BLE001 — few-shot fetch is optional; return empty on any error
        register_langfuse_failure(exc)
        logger.debug("Golden dataset fetch failed (non-blocking): %s", exc)
        return []


# ── Quality-gated golden dataset promotion ───────────────────────

_GOLDEN_MIN_QUALITY: float = 0.75
_GOLDEN_REQUIRE_WIN: bool = True


def auto_promote_to_golden(
    quality_report: dict[str, Any],
    alerts_json: str,
    snapshots_json: str,
    timeframe: str,
    *,
    trace_id: str | None = None,
    prompt_version: str = "unknown",
) -> bool:
    """Auto-promote a decision run to golden dataset if quality is high enough.

    Only promotes runs where:
    1. ``batch_avg_quality >= 0.75``
    2. At least one alert has ``outcome=WIN`` (when outcome data available)

    Called after quality scoring in the decision workflow.

    Args:
        quality_report: Quality scoring results from ``alert_quality.py``.
        alerts_json: Validated alerts JSON string.
        snapshots_json: Raw signal snapshots JSON.
        timeframe: Pipeline timeframe.
        trace_id: Langfuse trace ID.
        prompt_version: Prompt version identifier.

    Returns:
        True if the run was promoted, False otherwise.
    """
    if not quality_report:
        return False

    batch = quality_report.get("batch", {})
    avg_quality = batch.get("batch_avg_quality", 0.0)
    if avg_quality < _GOLDEN_MIN_QUALITY:
        logger.debug(
            "Golden gate: quality %.2f < %.2f — not promoted",
            avg_quality,
            _GOLDEN_MIN_QUALITY,
        )
        return False

    # Check for at least one WIN outcome in per-alert results
    per_alert = quality_report.get("per_alert", [])
    has_win = any(a.get("scores", {}).get("historical_accuracy", 0) >= 0.8 for a in per_alert)
    if _GOLDEN_REQUIRE_WIN and not has_win and per_alert:
        logger.debug("Golden gate: no high-accuracy alerts — not promoted")
        return False

    lf = get_langfuse_client()
    if lf is None:
        return False

    if not _ensure_dataset(lf, GOLDEN_DATASET_NAME):
        return False

    try:
        parsed_alerts = json.loads(alerts_json) if isinstance(alerts_json, str) else alerts_json

        now = datetime.now(tz=UTC)
        lf.create_dataset_item(
            dataset_name=GOLDEN_DATASET_NAME,
            input={
                "timeframe": timeframe,
                "snapshots": json.loads(snapshots_json)
                if isinstance(snapshots_json, str)
                else snapshots_json,
                "prompt_version": prompt_version,
                "timestamp": now.isoformat(),
            },
            expected_output={
                "alerts": parsed_alerts,
                "alert_count": len(parsed_alerts) if isinstance(parsed_alerts, list) else 0,
            },
            metadata={
                "trace_id": trace_id or "",
                "auto_promoted": True,
                "batch_avg_quality": avg_quality,
            },
        )
        lf.flush()
        logger.info(
            "Auto-promoted decision run to golden dataset (quality=%.2f, alerts=%d)",
            avg_quality,
            len(parsed_alerts) if isinstance(parsed_alerts, list) else 0,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — golden promotion is best-effort
        register_langfuse_failure(exc)
        logger.warning("Auto-promote to golden dataset failed: %s", exc)
        return False
