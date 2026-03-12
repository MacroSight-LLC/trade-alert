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
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from langfuse_client import get_langfuse_client

logger = logging.getLogger(__name__)

DATASET_NAME = "decision-runs"
GOLDEN_DATASET_NAME = "decision-golden"


def _ensure_dataset(lf: Any, name: str) -> bool:
    """Create the dataset if it doesn't exist yet.

    Args:
        lf: Langfuse client instance.
        name: Dataset name to create.

    Returns:
        True if dataset exists or was created, False on error.
    """
    try:
        lf.get_dataset(name)
        return True
    except Exception:
        try:
            lf.create_dataset(name=name, description=f"Auto-captured {name} for trade-alert")
            logger.info("Created Langfuse dataset: %s", name)
            return True
        except Exception as exc:  # noqa: BLE001
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
    lf = get_langfuse_client()
    if lf is None:
        return

    if not _ensure_dataset(lf, DATASET_NAME):
        return

    now = datetime.now(tz=timezone.utc)
    item_id = f"{timeframe}-{now.strftime('%Y%m%dT%H%M%S')}"

    try:
        # Parse alerts for the expected output
        try:
            parsed_alerts = json.loads(alerts_json)
        except (json.JSONDecodeError, TypeError):
            parsed_alerts = []

        input_data = {
            "timeframe": timeframe,
            "snapshots": json.loads(snapshots_json) if isinstance(snapshots_json, str) else snapshots_json,
            "prompt_version": prompt_version,
            "timestamp": now.isoformat(),
        }

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
        logger.info(
            "Captured decision run to dataset '%s': %s (%d alerts)",
            DATASET_NAME,
            item_id,
            len(parsed_alerts),
        )
    except Exception as exc:  # noqa: BLE001
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to promote to golden dataset: %s", exc)
