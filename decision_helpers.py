"""Shared helpers for decision-15m.yaml and decision-1h.yaml.

Extracts the common code blocks from both decision workflows to
eliminate the 92% DRY violation between them.  Each YAML step
calls a single helper function with just the timeframe parameter.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def merge_snapshots(
    timeframe: str,
    inputs: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge snapshots from orchestrator inputs or Redis fallback.

    Args:
        timeframe: Pipeline timeframe (``"15m"`` or ``"1h"``).
        inputs: Workflow inputs dict (may contain pre-merged data).

    Returns:
        Dict with keys: ``skip``, ``snapshots_json``, ``macro``, ``n``.
    """
    _inp_json = inputs.get("merged_snapshots_json", "") if inputs else ""
    _inp_macro = inputs.get("merged_macro") if inputs else None
    _inp_n = inputs.get("merged_n") if inputs else None

    if _inp_json and _inp_n:
        snapshots_json = _inp_json
        macro = _inp_macro or {}
        n = int(_inp_n)
        logger.info("Decision-%s: using %d pre-merged symbols from orchestrator", timeframe, n)
        return {"skip": n == 0, "snapshots_json": snapshots_json, "macro": macro, "n": n}

    from merger import get_macro_regime, merge

    snapshots = merge(timeframe, limit=20)
    macro = get_macro_regime()
    if len(snapshots) == 0:
        logger.info("No snapshots available for %s decision — skipping", timeframe)
        return {"skip": True, "snapshots_json": "[]", "macro": {}, "n": 0}

    snapshots_json = json.dumps([snap.model_dump() for snap in snapshots], indent=2)
    logger.info("Decision-%s: merged %d symbols for evaluation", timeframe, len(snapshots))
    return {"skip": False, "snapshots_json": snapshots_json, "macro": macro, "n": len(snapshots)}


def build_prompt(
    timeframe: str,
    merge_result: dict[str, Any],
    fred_results: list[dict[str, Any]],
) -> dict[str, str]:
    """Build the ensemble prompt for the decision LLM.

    Args:
        timeframe: Pipeline timeframe.
        merge_result: Output from :func:`merge_snapshots`.
        fred_results: FRED MCP results ``[{vix_level: ...}, {spread_bps: ...}]``.

    Returns:
        Dict with ``prompt`` and ``prompt_version`` keys.
    """
    from prompt_manager import (
        format_golden_examples,
        format_winrate_context,
        get_decision_prompts,
        get_prompt_version,
        get_quality_escalation_rules,
    )

    macro = merge_result["macro"]
    snapshots_json = merge_result["snapshots_json"]
    n = merge_result["n"]

    vix = fred_results[0].get("vix_level") or fred_results[0].get("value", "N/A")
    yc = fred_results[1].get("spread_bps") or fred_results[1].get("value", "N/A")

    _fred_live = vix != "N/A" and yc != "N/A"
    data_freshness = "LIVE" if _fred_live else "CACHED (stale — FRED unavailable)"

    risk_on = macro.get("risk_on", True)
    macro_summary = f"{'Risk-on' if risk_on else 'Risk-off'}, VIX={vix}, Yield curve={yc}bps"

    perf_ctx = format_winrate_context()
    escalation = get_quality_escalation_rules(timeframe)
    if escalation:
        perf_ctx = perf_ctx + "\n" + escalation if perf_ctx else escalation

    system_prompt, user_prompt = get_decision_prompts(
        timeframe,
        {
            "macro_summary": macro_summary,
            "vix": vix,
            "yc": yc,
            "n": n,
            "snapshots_json": snapshots_json,
            "data_freshness": data_freshness,
            "performance_context": perf_ctx,
            "few_shot_examples": format_golden_examples(),
        },
    )

    full_prompt = f"SYSTEM:\n{system_prompt}\n\nUSER:\n{user_prompt}"
    return {"prompt": full_prompt, "prompt_version": get_prompt_version()}


def validate_and_filter_step(
    timeframe: str,
    llm_response: str,
    merge_result: dict[str, Any],
    fred_results: list[dict[str, Any]],
    trace_id: str | None,
) -> dict[str, Any]:
    """Validate LLM output, score quality, and capture to dataset.

    Args:
        timeframe: Pipeline timeframe.
        llm_response: Raw LLM output string.
        merge_result: Output from :func:`merge_snapshots`.
        fred_results: FRED MCP results.
        trace_id: Langfuse trace ID (may be None).

    Returns:
        Dict with ``alerts_json``, ``count``, and optional ``quality``.
    """
    try:
        from pipeline_tracing import add_score, tag_trace
    except ImportError:
        add_score = tag_trace = None  # type: ignore[assignment]

    # Read VIX for server-side gates
    try:
        _vix = float(fred_results[0].get("vix_level") or fred_results[0].get("value", 0))
    except (ValueError, TypeError, IndexError, KeyError):
        _vix = 0.0
    _macro = merge_result.get("macro") or {}

    from validate_and_filter import validate_and_filter as _vf

    alerts, alerts_json = _vf(
        llm_response=llm_response,
        snapshots_json=merge_result["snapshots_json"],
        macro=_macro,
        vix=_vix,
        timeframe=timeframe,
        add_score_fn=add_score,
        trace_id=trace_id,
    )
    result: dict[str, Any] = {"alerts_json": alerts_json, "count": len(alerts)}

    # Deep quality scoring
    quality_report = None
    try:
        from alert_quality import post_quality_scores

        quality_report = post_quality_scores(trace_id, alerts)
        result["quality"] = quality_report
    except Exception as qe:
        logger.debug("Quality scoring failed (non-blocking): %s", qe)

    # Capture to Langfuse dataset
    try:
        from langfuse_datasets import capture_decision_run
        from prompt_manager import get_prompt_version

        capture_decision_run(
            timeframe,
            merge_result["snapshots_json"],
            llm_response,
            alerts_json,
            quality_report,
            trace_id=trace_id,
            prompt_version=get_prompt_version(),
        )
    except Exception as de:
        logger.debug("Dataset capture failed (non-blocking): %s", de)

    if tag_trace and trace_id:
        tag_trace(trace_id, [f"alerts:{len(alerts)}"])

    return result
