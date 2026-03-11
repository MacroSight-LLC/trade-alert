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
import time
from datetime import datetime, timezone

import httpx

from models import PlaybookAlert, TraceAnalysis

logger = logging.getLogger(__name__)

# ── Configuration via environment ────────────────────────────────────────────
LANGFUSE_HOST: str = os.getenv("LANGFUSE_HOST", "http://localhost:3000")
LANGFUSE_PUBLIC_KEY: str = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY: str = os.getenv("LANGFUSE_SECRET_KEY", "")
TRACE_COST_BUDGET: float = float(os.getenv("TRACE_COST_BUDGET", "0.50"))
TRACE_LATENCY_MAX: float = float(os.getenv("TRACE_LATENCY_MAX", "120"))
PROMPT_VERSION: str = os.getenv("PROMPT_VERSION", "v1.0")

_MAX_RETRIES: int = 3
_BACKOFF_BASE: float = 2.0


def _auth() -> tuple[str, str]:
    """Return basic-auth credentials for the Langfuse REST API."""
    return (LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY)


def _is_configured() -> bool:
    """Check whether Langfuse credentials are present."""
    return bool(LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY)


# ── Trace Fetching ───────────────────────────────────────────────────────────


def fetch_latest_trace(session_id: str) -> dict | None:
    """Fetch the most recent Langfuse trace for *session_id*.

    Args:
        session_id: Langfuse session identifier (e.g. ``orchestrator-15m``).

    Returns:
        The trace dict from the Langfuse API, or ``None`` if unavailable.
    """
    if not _is_configured():
        logger.warning("Langfuse credentials not set — skipping trace fetch")
        return None

    url = f"{LANGFUSE_HOST.rstrip('/')}/api/public/traces"
    params = {"sessionId": session_id, "limit": 1, "orderBy": "timestamp.desc"}

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(url, params=params, auth=_auth())
                resp.raise_for_status()
                data = resp.json()
                traces = data.get("data", [])
                if traces:
                    return traces[0]
                logger.info("No traces found for session %s", session_id)
                return None
        except httpx.HTTPError as exc:
            logger.warning(
                "Langfuse trace fetch attempt %d/%d failed: %s",
                attempt,
                _MAX_RETRIES,
                exc,
            )
            if attempt < _MAX_RETRIES:
                time.sleep(_BACKOFF_BASE**attempt)
    return None


# ── Individual Checks ────────────────────────────────────────────────────────


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

    # Output may be a string (JSON) or already a dict
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
    cost = trace.get("calculatedTotalCost") or 0.0
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
    if not _is_configured():
        return

    url = f"{LANGFUSE_HOST.rstrip('/')}/api/public/scores"
    payload = {
        "traceId": trace_id,
        "name": "pipeline_health",
        "value": score,
        "comment": comment,
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload, auth=_auth())
            resp.raise_for_status()
            logger.info("Scored trace %s: %.2f", trace_id, score)
    except httpx.HTTPError as exc:
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

    trace = fetch_latest_trace(session_id)
    if trace is None:
        logger.info("No trace available for %s — skipping analysis", session_id)
        return TraceAnalysis(
            trace_id="",
            is_healthy=True,
            issues=["no_trace_available"],
            prompt_version=PROMPT_VERSION,
            timestamp=now,
        )

    trace_id: str = trace.get("id", "unknown")

    # Run all checks
    all_issues: list[str] = []
    all_issues.extend(check_output_validity(trace))
    all_issues.extend(check_cost(trace, TRACE_COST_BUDGET))
    all_issues.extend(check_latency(trace, TRACE_LATENCY_MAX))

    is_healthy = len(all_issues) == 0
    cost = trace.get("calculatedTotalCost") or 0.0
    latency = trace.get("latency") or 0.0
    llm_calls = trace.get("observations", 0) if isinstance(trace.get("observations"), int) else 0
    total_tokens = trace.get("totalTokens") or trace.get("usage", {}).get("totalTokens", 0)

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
        prompt_version=PROMPT_VERSION,
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
