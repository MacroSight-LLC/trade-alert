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
TRACE_LATENCY_MAX: float = float(os.getenv("TRACE_LATENCY_MAX", "180"))

# ── Embed colour thresholds ──────────────────────────────────────────────────
_COLOR_HEALTHY: int = 0x2ECC71  # green
_COLOR_DEGRADED: int = 0xF1C40F  # yellow
_COLOR_UNHEALTHY: int = 0xE74C3C  # red


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

        # Convert SDK object → dict with snake_case keys for uniform handling.
        # Pydantic v1 .dict() emits camelCase (e.g. totalCost), so we read
        # attributes directly to get the snake_case Python values.
        obs_list = []
        for obs in trace_obj.observations or []:
            obs_dict = obs.dict() if hasattr(obs, "dict") else obs.__dict__
            # Normalise observation cost/token fields
            obs_dict["calculated_total_cost"] = getattr(obs, "calculated_total_cost", None)
            obs_dict["usage"] = getattr(obs, "usage", None)
            obs_list.append(obs_dict)

        return {
            "id": trace_obj.id,
            "total_cost": trace_obj.total_cost,
            "latency": trace_obj.latency,
            "output": trace_obj.output,
            "observations": obs_list,
        }
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


def _check_collector_coverage(trace: dict) -> list[str]:
    """Check that the trace output reports adequate collector success.

    Args:
        trace: A Langfuse trace dict.

    Returns:
        List of collector coverage issues.
    """
    issues: list[str] = []
    output = trace.get("output")
    if not isinstance(output, dict):
        return issues

    # If merger_candidates is very low, signal data was likely thin
    candidates = output.get("merger_candidates", 0)
    if isinstance(candidates, (int, float)) and candidates < 3:
        issues.append(f"Low merger candidates ({candidates}) — collectors may have failed")
    return issues


def _check_alert_quality_scores(trace_id: str) -> list[str]:
    """Fetch Langfuse scores for this trace and flag quality regressions.

    Looks for batch_avg_quality scores posted by alert_quality.py.

    Args:
        trace_id: Langfuse trace ID.

    Returns:
        List of quality-related issues.
    """
    issues: list[str] = []
    lf = get_langfuse_client()
    if lf is None:
        return issues

    try:
        # Fetch the full trace to look at scores
        full = lf.fetch_trace(trace_id)
        if full and full.data:
            # Check scores attached to this trace
            scores = getattr(full.data, "scores", None) or []
            for score_obj in scores:
                name = getattr(score_obj, "name", "")
                value = getattr(score_obj, "value", None)
                if name == "batch_avg_quality" and value is not None and value < 0.5:
                    issues.append(f"Low batch alert quality ({value:.2f}) — prompt may need tuning")
                if name == "llm_json_valid" and value is not None and value < 1.0:
                    issues.append("LLM produced invalid JSON — prompt compliance issue")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not fetch quality scores for trace %s: %s", trace_id, exc)

    return issues


# ── Ops Embed Formatter ──────────────────────────────────────────────────────


def _health_bar(score: float, segments: int = 10) -> str:
    """Build a visual Unicode bar for a 0-1 health score.

    Args:
        score: Float between 0.0 and 1.0.
        segments: Number of bar segments.

    Returns:
        Unicode bar string like ``▓▓▓▓▓▓▓░░░ 70%``.
    """
    filled = round(score * segments)
    return "▓" * filled + "░" * (segments - filled) + f" {score * 100:.0f}%"


def _severity_emoji(issue: str) -> str:
    """Map an issue string to a severity emoji for the ops embed."""
    lower = issue.lower()
    if "cost" in lower or "budget" in lower:
        return "\U0001f4b8"  # 💸
    if "latency" in lower or "exceeds max" in lower:
        return "\u23f1\ufe0f"  # ⏱️
    if "validation failed" in lower or "invalid json" in lower:
        return "\u274c"  # ❌
    if "collector" in lower or "merger" in lower or "candidates" in lower:
        return "\U0001f4e1"  # 📡
    if "quality" in lower or "prompt" in lower:
        return "\U0001f9ea"  # 🧪
    return "\u26a0\ufe0f"  # ⚠️


def _recommendation(issue: str) -> str:
    """Generate an actionable recommendation for an issue."""
    lower = issue.lower()
    if "cost" in lower or "budget" in lower:
        return "Review prompt token usage; consider shorter system prompts or fewer tool calls"
    if "latency" in lower:
        return "Check MCP server response times; consider increasing TRACE_LATENCY_MAX or parallelising collectors"
    if "validation failed" in lower:
        return "LLM output schema drift — review decision prompt for PlaybookAlert compliance"
    if "invalid json" in lower:
        return "JSON parse failure — add stricter output format instructions to prompt"
    if "collector" in lower or "merger" in lower or "candidates" in lower:
        return "Collectors returned thin data — check MCP server health and API rate limits"
    if "quality" in lower:
        return "Alert quality below threshold — review and iterate on decision prompt"
    return "Investigate in Langfuse trace details"


def format_ops_embed(
    *,
    session_id: str,
    trace_id: str,
    health_score: float,
    is_healthy: bool,
    issues: list[str],
    cost_usd: float,
    latency_s: float,
    llm_calls: int,
    total_tokens: int,
    n_candidates: int,
    prompt_version: str,
) -> dict:
    """Build a rich Discord embed for the #trade-ops channel.

    Produces a color-coded, multi-section embed with health gauge,
    metric cards, issue breakdown with recommendations, and a
    Langfuse trace link.

    Args:
        session_id: Pipeline session identifier.
        trace_id: Langfuse trace ID.
        health_score: Computed health score (0.0-1.0).
        is_healthy: Whether all checks passed.
        issues: List of detected issues.
        cost_usd: Total LLM cost for this trace.
        latency_s: Pipeline duration in seconds.
        llm_calls: Number of LLM observations.
        total_tokens: Total tokens consumed.
        n_candidates: Number of merger candidates evaluated.
        prompt_version: Active prompt version tag.

    Returns:
        Dict with ``embeds`` key matching Discord embed structure.
    """
    now = datetime.now(tz=timezone.utc)

    # Color by health tier
    if health_score >= 0.80:
        color = _COLOR_HEALTHY
        status_emoji = "\u2705"  # ✅
        status_label = "HEALTHY"
    elif health_score >= 0.50:
        color = _COLOR_DEGRADED
        status_emoji = "\u26a0\ufe0f"  # ⚠️
        status_label = "DEGRADED"
    else:
        color = _COLOR_UNHEALTHY
        status_emoji = "\U0001f6a8"  # 🚨
        status_label = "UNHEALTHY"

    # Cost utilisation
    cost_pct = (cost_usd / TRACE_COST_BUDGET * 100) if TRACE_COST_BUDGET else 0
    cost_indicator = "\u2705" if cost_pct <= 80 else ("\u26a0\ufe0f" if cost_pct <= 100 else "\U0001f6a8")

    # Latency utilisation
    latency_pct = (latency_s / TRACE_LATENCY_MAX * 100) if TRACE_LATENCY_MAX else 0
    latency_indicator = (
        "\u2705" if latency_pct <= 80 else ("\u26a0\ufe0f" if latency_pct <= 100 else "\U0001f6a8")
    )

    # Tokens per candidate
    tpc = f"{total_tokens / n_candidates:,.0f}" if n_candidates else "N/A"

    # Langfuse trace link
    langfuse_host = os.getenv("LANGFUSE_HOST", "http://localhost:3000")
    trace_url = f"{langfuse_host}/trace/{trace_id}"

    fields: list[dict] = [
        # ── Health gauge ──
        {
            "name": f"{status_emoji} Pipeline Health",
            "value": f"```{_health_bar(health_score)}```",
            "inline": False,
        },
        # ── Resource metrics row ──
        {
            "name": f"{cost_indicator} Cost",
            "value": f"```${cost_usd:.4f} / ${TRACE_COST_BUDGET:.2f}```{cost_pct:.0f}% of budget",
            "inline": True,
        },
        {
            "name": f"{latency_indicator} Latency",
            "value": f"```{latency_s:.1f}s / {TRACE_LATENCY_MAX:.0f}s```{latency_pct:.0f}% of max",
            "inline": True,
        },
        {
            "name": "\U0001f916 LLM Usage",
            "value": f"```{llm_calls} calls \u2022 {total_tokens:,} tokens```Tokens/candidate: {tpc}",
            "inline": True,
        },
        # ── Pipeline details ──
        {
            "name": "\U0001f4e6 Pipeline Details",
            "value": (
                f"**Candidates:** {n_candidates}\n"
                f"**Prompt:** `{prompt_version}`\n"
                f"**Trace:** [{trace_id[:12]}...]({trace_url})"
            ),
            "inline": False,
        },
    ]

    # ── Issues breakdown ──
    if issues:
        issue_lines: list[str] = []
        for issue in issues:
            emoji = _severity_emoji(issue)
            rec = _recommendation(issue)
            issue_lines.append(f"{emoji} **{issue}**\n\u2514\u2500 _{rec}_")

        fields.append(
            {
                "name": f"\U0001f6a8 Issues ({len(issues)})",
                "value": "\n\n".join(issue_lines),
                "inline": False,
            }
        )
    else:
        fields.append(
            {
                "name": "\u2705 No Issues Detected",
                "value": "_All checks passed — pipeline operating normally._",
                "inline": False,
            }
        )

    return {
        "embeds": [
            {
                "title": f"\U0001f50d Trace Analysis \u2014 **{session_id}**",
                "description": f"**Status: {status_label}** \u2022 Score {health_score:.2f}/1.00",
                "color": color,
                "fields": fields,
                "footer": {"text": "trade-alert ops \u2022 MacroSight LLC"},
                "timestamp": now.isoformat(),
            }
        ]
    }


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
    all_issues.extend(_check_collector_coverage(trace))
    all_issues.extend(_check_alert_quality_scores(trace_id))

    is_healthy = len(all_issues) == 0
    cost = trace.get("total_cost") or 0.0
    latency = trace.get("latency") or 0.0

    # observations is a list of ObservationsView dicts from fetch_trace()
    observations = trace.get("observations") or []
    llm_calls = len(observations)
    total_tokens = _sum_tokens(observations)

    # Compute health score: 1.0 = perfect, deduct 0.20 per issue, floor 0.0
    health_score = max(0.0, 1.0 - 0.20 * len(all_issues))

    # Post score to Langfuse
    comment = "healthy" if is_healthy else f"issues: {', '.join(all_issues)}"
    score_trace(trace_id, health_score, comment)

    # Token efficiency: tokens per candidate evaluated
    n_candidates = (
        trace.get("output", {}).get("merger_candidates", 0) if isinstance(trace.get("output"), dict) else 0
    )
    if total_tokens and n_candidates:
        tokens_per_candidate = total_tokens / n_candidates
        try:
            from pipeline_tracing import add_score

            add_score(
                trace_id,
                "tokens_per_candidate",
                tokens_per_candidate,
                comment=f"{total_tokens} tokens / {n_candidates} candidates",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to post tokens_per_candidate score: %s", exc)

    # Cost efficiency: cost per alert
    output = trace.get("output")
    if isinstance(output, dict) and cost:
        try:
            from pipeline_tracing import add_score

            add_score(
                trace_id,
                "cost_per_alert",
                cost / max(1, output.get("alerts_fired", 1)),
                comment=f"${cost:.4f} total / alerts",
            )
        except Exception:  # noqa: BLE001
            pass

    # Send rich embed to ops channel (both healthy and unhealthy)
    try:
        from notifier_and_logger import send_ops_embed

        embed = format_ops_embed(
            session_id=session_id,
            trace_id=trace_id,
            health_score=health_score,
            is_healthy=is_healthy,
            issues=all_issues,
            cost_usd=cost,
            latency_s=latency,
            llm_calls=llm_calls,
            total_tokens=total_tokens,
            n_candidates=n_candidates,
            prompt_version=prompt_version,
        )
        send_ops_embed(embed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not send ops embed: %s", exc)

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
