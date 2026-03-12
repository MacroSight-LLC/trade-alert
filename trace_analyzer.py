"""Post-execution trace analyzer for pipeline self-healing.

Fetches the most recent Langfuse trace for a pipeline run, validates
LLM outputs against PlaybookAlert, checks cost/latency budgets,
posts a health score back to Langfuse, and alerts ops on Discord
if anything looks wrong.
Implements the self-healing layer described in the Langfuse integration plan.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from langfuse_client import get_langfuse_client
from models import PlaybookAlert, TraceAnalysis
from prompt_manager import get_prompt_version

logger = logging.getLogger(__name__)

# ── Configuration via environment ────────────────────────────────────────────
TRACE_COST_BUDGET: float = float(os.getenv("TRACE_COST_BUDGET", "0.50"))
TRACE_LATENCY_MAX: float = float(os.getenv("TRACE_LATENCY_MAX", "120"))


# ── Trace Fetching ───────────────────────────────────────────────────────────


def fetch_latest_trace(session_id: str) -> dict | None:
    """Fetch the most recent Langfuse trace for *session_id*.

    Uses the Langfuse SDK ``fetch_traces`` to find the latest trace ID,
    then ``fetch_trace`` to retrieve full details (including observations
    with cost and token data).

    Args:
        session_id: Langfuse session identifier (e.g. ``orchestrator-15m``).

    Returns:
        The trace dict from the Langfuse API, or ``None`` if unavailable.
    """
    lf = get_langfuse_client()
    if lf is None:
        logger.warning("Langfuse credentials not set — skipping trace fetch")
        return None

    try:
        response = lf.fetch_traces(
            session_id=session_id,
            limit=1,
            order_by="timestamp.DESC",
        )
        traces = response.data if response.data else []
        if not traces:
            logger.info("No traces found for session %s", session_id)
            return None

        trace_id: str = traces[0].id

        # Fetch full trace with observations (includes cost/token data)
        full = lf.fetch_trace(trace_id)
        trace_obj = full.data if full else None
        if trace_obj is None:
            logger.warning("fetch_trace(%s) returned no data", trace_id)
            return None

        # Convert SDK object → dict for uniform downstream handling
        return trace_obj.dict() if hasattr(trace_obj, "dict") else trace_obj.__dict__
    except Exception as exc:  # noqa: BLE001
        logger.warning("Langfuse trace fetch failed: %s", exc)
        return None


# ── Individual Checks ────────────────────────────────────────────────────────


def _sum_tokens(observations: list[dict]) -> int:
    """Sum total tokens across all observations in a trace.

    Args:
        observations: List of observation dicts from ``TraceWithFullDetails``.

    Returns:
        Total token count across all observations.
    """
    total = 0
    for obs in observations:
        usage = obs.get("usage") if isinstance(obs, dict) else getattr(obs, "usage", None)
        if usage is None:
            continue
        tok = usage.get("total") if isinstance(usage, dict) else getattr(usage, "total", None)
        if tok:
            total += int(tok)
    return total


def check_output_validity(trace: dict) -> list[str]:
    """Validate that the LLM output in *trace* conforms to PlaybookAlert.

    Args:
        trace: A Langfuse trace dict (must contain ``output`` field).

    Returns:
        List of validation issues (empty means valid).
    """
    issues: list[str] = []
    output = trace.get("output")
    if output is None:
        # No output recorded — not necessarily an error for collector traces
        return issues

    # Only validate outputs that look like a PlaybookAlert (must have "symbol").
    # Orchestrator traces set output to a summary dict like
    # {"timeframe": "15m", "status": "completed", "merger_candidates": 20}
    # which would always fail validation and penalise pipeline_health.
    if isinstance(output, dict) and "symbol" not in output:
        return issues
    if isinstance(output, str):
        try:
            import json as _json

            parsed = _json.loads(output)
            if isinstance(parsed, dict) and "symbol" not in parsed:
                return issues
        except (ValueError, TypeError):
            pass

    # Output looks like a PlaybookAlert — validate it
    try:
        if isinstance(output, str):
            PlaybookAlert.model_validate_json(output)
        elif isinstance(output, dict):
            PlaybookAlert.model_validate(output)
        else:
            issues.append(f"Unexpected output type: {type(output).__name__}")
    except Exception as exc:  # noqa: BLE001
        issues.append(f"PlaybookAlert validation failed: {exc}")
    return issues


def check_cost(trace: dict, budget: float) -> list[str]:
    """Check whether the trace cost exceeds the per-run *budget*.

    Args:
        trace: A Langfuse trace dict.
        budget: Maximum allowed cost in USD.

    Returns:
        List of cost issues (empty means within budget).
    """
    issues: list[str] = []
    cost = trace.get("total_cost") or 0.0
    if cost > budget:
        issues.append(f"Cost ${cost:.4f} exceeds budget ${budget:.2f}")
    return issues


def check_latency(trace: dict, max_seconds: float) -> list[str]:
    """Check whether the trace duration exceeds *max_seconds*.

    Args:
        trace: A Langfuse trace dict (uses ``latency`` field in seconds).
        max_seconds: Maximum allowed pipeline duration.

    Returns:
        List of latency issues (empty means within threshold).
    """
    issues: list[str] = []
    latency = trace.get("latency")
    if latency is not None and latency > max_seconds:
        issues.append(f"Latency {latency:.1f}s exceeds max {max_seconds:.0f}s")
    return issues


# ── Scoring ──────────────────────────────────────────────────────────────────


def score_trace(trace_id: str, score: float, comment: str) -> None:
    """Post a health score to Langfuse for the given trace.

    Args:
        trace_id: The Langfuse trace ID to score.
        score: Numeric score (0.0 = unhealthy, 1.0 = perfect).
        comment: Human-readable summary of the analysis.
    """
    lf = get_langfuse_client()
    if lf is None:
        return

    try:
        lf.score(
            trace_id=trace_id,
            name="pipeline_health",
            value=score,
            comment=comment,
        )
        logger.info("Scored trace %s: %.2f", trace_id, score)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to score trace %s: %s", trace_id, exc)


# ── Main Entry Point ─────────────────────────────────────────────────────────


def analyze_pipeline_trace(timeframe: str) -> TraceAnalysis:
    """Analyze the most recent pipeline trace and trigger self-healing alerts.

    This is the main entry point called from orchestrator YAML workflows.
    It fetches the latest trace, runs validity/cost/latency checks, posts
    a health score to Langfuse, and sends a Discord ops alert if unhealthy.

    Args:
        timeframe: Pipeline timeframe (e.g. ``"15m"``, ``"1h"``).

    Returns:
        A ``TraceAnalysis`` summarising the findings.
    """
    session_id = f"orchestrator-{timeframe}"
    now = datetime.now(tz=timezone.utc).isoformat()
    prompt_version = get_prompt_version()

    trace = fetch_latest_trace(session_id)
    if trace is None:
        logger.info("No trace available for %s — skipping analysis", session_id)
        return TraceAnalysis(
            trace_id="",
            is_healthy=True,
            issues=["no_trace_available"],
            prompt_version=prompt_version,
            timestamp=now,
        )

    trace_id: str = trace.get("id", "unknown")

    # Run all checks
    all_issues: list[str] = []
    all_issues.extend(check_output_validity(trace))
    all_issues.extend(check_cost(trace, TRACE_COST_BUDGET))
    all_issues.extend(check_latency(trace, TRACE_LATENCY_MAX))

    is_healthy = len(all_issues) == 0
    cost = trace.get("total_cost") or 0.0
    latency = trace.get("latency") or 0.0

    # observations is a list of ObservationsView dicts from fetch_trace()
    observations = trace.get("observations") or []
    llm_calls = len(observations)
    total_tokens = _sum_tokens(observations)

    # Compute health score: 1.0 = perfect, deduct 0.25 per issue, floor 0.0
    health_score = max(0.0, 1.0 - 0.25 * len(all_issues))

    # Post score to Langfuse
    comment = "healthy" if is_healthy else f"issues: {', '.join(all_issues)}"
    score_trace(trace_id, health_score, comment)

    # Alert ops on Discord if unhealthy
    if not is_healthy:
        try:
            from notifier_and_logger import send_ops_message

            msg = f"🔍 Trace analysis for **{session_id}** — score {health_score:.2f}\n" + "\n".join(
                f"  • {issue}" for issue in all_issues
            )
            send_ops_message(msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not send ops alert: %s", exc)

    result = TraceAnalysis(
        trace_id=trace_id,
        is_healthy=is_healthy,
        issues=all_issues,
        cost_usd=cost,
        latency_s=latency,
        llm_calls=llm_calls,
        total_tokens=total_tokens,
        prompt_version=prompt_version,
        timestamp=now,
    )
    logger.info(
        "Trace analysis complete: trace=%s healthy=%s issues=%d cost=$%.4f latency=%.1fs",
        trace_id,
        is_healthy,
        len(all_issues),
        cost,
        latency,
    )
    return result
