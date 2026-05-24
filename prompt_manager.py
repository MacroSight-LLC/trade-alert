"""Langfuse prompt management with YAML-equivalent fallback.

Fetches versioned system/user prompts from the Langfuse Prompt
Management API so you can iterate on them from the Langfuse UI
(localhost:3000) without touching YAML files.  Falls back to
built-in strings identical to the original inline YAML prompts
when Langfuse is unreachable or the prompts have not been seeded.

Prompt names in Langfuse:
    ``decision-system`` — role definition, rules, output format
    ``decision-user``   — macro context, signals, gate thresholds

Variables injected at runtime:
    timeframe, extra_rules, data_freshness, performance_context,
    few_shot_examples, snapshot_age_oldest, snapshot_age_newest,
    market_hours_status, recent_alerts_context,
    macro_summary, vix, yc, n, snapshots_json,
    market_reference_context,
    ep_gate, sa_gate, conf_gate
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from constants import get_market_hours_status  # noqa: F811 — re-exported
from gate_config import GATE_PROMPT_DEFAULTS, prompt_gate_vars
from langfuse_client import get_langfuse_client
from prompt_fetcher import (
    _last_source,
    _last_version,
    _prompt_cache,
    fetch_prompts_from_langfuse,
    get_prompt_source,
    get_prompt_version,
    set_yaml_fallback_source,
)
from prompt_renderer import (
    MAX_PROMPT_TOKENS,
    _compile_template,
    compile_fallback_prompts,
    compute_snapshot_freshness,
    get_extra_rules,
    trim_snapshots_for_token_budget,
)

logger = logging.getLogger(__name__)

# Per-timeframe gate defaults — sourced from gate_config SSOT
_GATE_DEFAULTS: dict[str, dict[str, str]] = GATE_PROMPT_DEFAULTS


def format_winrate_context() -> str:
    """Format recent win-rate data for injection into the system prompt."""
    try:
        from db import get_recent_winrate_summary

        data = get_recent_winrate_summary(days=7)
        total = data.get("total_resolved", 0)
        if total < 5:
            return "No recent outcome data available yet."

        winrate = data.get("winrate")
        avg_ep = data.get("avg_ep")
        lines = [
            f"Last 7 days: {total} resolved alerts, win-rate {winrate:.0%}, avg EP claimed {avg_ep:.2f}.",
            "",
            "EP Calibration Table (claimed EP → actual outcome):",
            "| EP Bucket | Alerts | Actual Win% | Deviation |",
            "|-----------|--------|-------------|-----------|",
        ]
        for bucket in data.get("ep_calibration", []):
            b = bucket["bucket"]
            t = bucket["total"]
            wr = bucket.get("actual_winrate")
            if t >= 2 and wr is not None:
                deviation = wr - b
                dev_label = f"{deviation:+.0%}"
                lines.append(f"| {b:.2f}      | {t:>6} | {wr:>10.0%}  | {dev_label:>9} |")
        lines.append("")
        lines.append(
            "IMPORTANT: If your claimed EP consistently exceeds actual win-rate "
            "for a bucket, lower your EP for similar setups."
        )
        return "\n".join(lines)
    except Exception:
        return "No recent outcome data available yet."


def format_golden_examples() -> str:
    """Format few-shot examples from the golden dataset for the user prompt."""
    try:
        from langfuse_datasets import get_golden_examples

        examples = get_golden_examples(n=3)
        if not examples:
            return ""

        lines = [
            "REFERENCE EXAMPLES (high-quality alerts from production — "
            "match this specificity and calibration):",
        ]
        for i, ex in enumerate(examples, 1):
            alerts = ex.get("expected_output", {}).get("alerts", [])
            if alerts:
                lines.append(f"\nExample {i}:")
                lines.append(json.dumps(alerts[0], indent=2))
        return "\n".join(lines)
    except (ImportError, json.JSONDecodeError, TypeError, KeyError):
        return ""


def get_quality_escalation_rules(timeframe: str) -> str:
    """Check recent quality scores and return escalation rules if degrading."""
    try:
        from langfuse_client import get_langfuse_client as _get_langfuse_client

        lf = _get_langfuse_client()
        if lf is None:
            return ""

        session_id = f"orchestrator-{timeframe}"
        response = lf.fetch_traces(
            session_id=session_id,
            limit=5,
            order_by="timestamp.DESC",
        )
        traces = response.data if response.data else []
        if not traces:
            return ""

        quality_scores: list[float] = []
        for trace in traces:
            scores = getattr(trace, "scores", None) or []
            for score_obj in scores:
                if getattr(score_obj, "name", "") == "batch_avg_quality":
                    val = getattr(score_obj, "value", None)
                    if val is not None:
                        quality_scores.append(float(val))

        if len(quality_scores) < 3:
            return ""

        avg_quality = sum(quality_scores) / len(quality_scores)

        if avg_quality < 0.50:
            return (
                "\n⚠️ QUALITY ESCALATION (STRICT): Recent alert quality has "
                "degraded significantly. Apply stricter standards:\n"
                "- Output at MOST 2 alerts this cycle\n"
                "- Each alert MUST have sources_agree >= 4\n"
                "- Thesis MUST contain specific numeric values from the signals\n"
                "- When in doubt, output [] rather than a marginal alert"
            )
        if avg_quality < 0.65:
            return (
                "\n⚠️ QUALITY ESCALATION (MODERATE): Recent alert quality is "
                "below target. Be MORE selective:\n"
                "- Raise your internal conviction bar — only alert on setups "
                "you would consider exceptional\n"
                "- Ensure every thesis is specific and causal, not generic"
            )
        return ""
    except Exception:  # noqa: BLE001 - Langfuse quality lookups must never block prompt generation
        return ""


def get_recent_alerts_context(hours: int = 2, limit: int = 10) -> str:
    """Build a de-duplication context string from recent alerts."""
    try:
        import psycopg2.extras

        from db import _put_conn, get_conn

        sql = """
            SELECT symbol, direction, edge_probability, timeframe, created_at
            FROM alerts
            WHERE created_at >= NOW() - make_interval(hours => %s)
            ORDER BY created_at DESC
            LIMIT %s
        """
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (hours, limit))
                rows = cur.fetchall()
        finally:
            _put_conn(conn)

        if not rows:
            return f"None in the last {hours} hours."

        parts: list[str] = []
        for r in rows:
            age_min = int((datetime.now(UTC) - r["created_at"].replace(tzinfo=UTC)).total_seconds() / 60)
            parts.append(
                f"{r['symbol']} {r['direction']} (EP {r['edge_probability']:.2f}, "
                f"{r['timeframe']}, {age_min}m ago)"
            )
        return "; ".join(parts)
    except (OSError, KeyError, TypeError, RuntimeError):
        return "Unable to fetch recent alerts."


def get_decision_prompts(
    timeframe: str,
    variables: dict[str, Any],
) -> tuple[str, str]:
    """Return compiled (system, user) prompts for the decision engine."""
    merged = {
        "timeframe": timeframe,
        "extra_rules": get_extra_rules(timeframe),
        "data_freshness": "LIVE",
        "market_reference_context": "",
        "performance_context": "No recent outcome data available yet.",
        "few_shot_examples": "",
        "snapshot_age_oldest": "0",
        "snapshot_age_newest": "0",
        "market_hours_status": get_market_hours_status(),
        "recent_alerts_context": get_recent_alerts_context(),
        **prompt_gate_vars(timeframe),
        **variables,
    }

    trimmed_json, _trimmed = trim_snapshots_for_token_budget(str(merged.get("snapshots_json", "[]")))
    merged["snapshots_json"] = trimmed_json
    merged["n"] = len(json.loads(trimmed_json)) if trimmed_json and trimmed_json != "[]" else 0

    snap_json = merged.get("snapshots_json", "")
    if snap_json and snap_json != "[]":
        freshness = compute_snapshot_freshness(str(snap_json))
        merged["snapshot_age_oldest"] = str(freshness["oldest_seconds"])
        merged["snapshot_age_newest"] = str(freshness["newest_seconds"])

    warnings: list[str] = []
    if int(merged["snapshot_age_oldest"]) > 1200:
        warnings.append(
            f"⚠️ SIGNAL FRESHNESS WARNING: Oldest snapshot is "
            f"{merged['snapshot_age_oldest']}s old. "
            f"Downgrade confidence on time-sensitive signals."
        )
    if str(merged.get("data_freshness", "")).startswith("CACHED"):
        warnings.append(
            "⚠️ MACRO DATA WARNING: VIX/yield data is stale "
            "(FRED unavailable). Downgrade confidence on "
            "macro-sensitive signals."
        )

    try:
        from winrate_injector import format_winrate_section, get_winrate_context

        wr_dict = get_winrate_context(timeframe)
        wr_section = format_winrate_section(wr_dict)
        if wr_section:
            existing_perf = str(merged.get("performance_context", ""))
            merged["performance_context"] = (
                existing_perf + "\n\n" + wr_section if existing_perf else wr_section
            )
    except (ImportError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        logger.warning("winrate injection skipped: %s", exc)

    langfuse_result = fetch_prompts_from_langfuse(timeframe, merged, warnings, get_langfuse_client())
    if langfuse_result is not None:
        return langfuse_result

    set_yaml_fallback_source()
    logger.info("Prompts loaded from YAML fallback (timeframe=%s)", timeframe)
    return compile_fallback_prompts(merged, warnings)


def get_gate_defaults() -> dict[str, dict[str, str]]:
    """Return per-timeframe gate threshold defaults for generation metadata."""
    return dict(_GATE_DEFAULTS)


__all__ = [
    "MAX_PROMPT_TOKENS",
    "_compile_template",
    "_last_source",
    "_last_version",
    "_prompt_cache",
    "format_golden_examples",
    "format_winrate_context",
    "get_decision_prompts",
    "get_gate_defaults",
    "get_market_hours_status",
    "get_prompt_source",
    "get_prompt_version",
    "get_quality_escalation_rules",
    "get_recent_alerts_context",
    "get_langfuse_client",
]
