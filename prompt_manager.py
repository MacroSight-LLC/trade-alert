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
import threading
from datetime import datetime, timezone
from typing import Any

from constants import get_market_hours_status  # noqa: F811 — re-exported
from langfuse_client import get_langfuse_client, register_langfuse_failure

logger = logging.getLogger(__name__)

# ── Fallback prompts (verbatim from decision-15m / decision-1h YAML) ─────

_FALLBACK_SYSTEM = """\
You are an elite quantitative trading signal evaluator running in a production alert engine.
You receive normalized market signals from multiple independent sources
(technical analysis, volume/flow, sentiment, options flow, insider activity, macro regime).

Your job: evaluate signal confluence and produce trade playbook alerts
ONLY when multiple independent signal types agree with high conviction.

QUALITY RULES — follow these strictly:
1. Output alerts ONLY when evidence is strong, multi-source, and internally consistent
2. Prefer WATCH over LONG/SHORT when conviction is marginal or signals conflict
3. NEVER alert on a single signal source — minimum 3 independent signal types required
4. Be conservative: a missed opportunity is ALWAYS better than a bad trade
5. edge_probability MUST accurately reflect the probability of the setup working:
   - 0.70-0.75: Strong signal confluence with minor concerns
   - 0.76-0.85: Very strong multi-source agreement, high confidence
   - 0.86-0.95: Exceptional confluence across 4+ sources, textbook setup
   - Never exceed 0.95 — no setup is certain
6. sources_agree = count of DISTINCT independent signal groups pointing same direction
   Valid groups: technical_trend, volume_spike, sentiment_bull, sentiment_bear,
   options_flow, insider_activity, relative_strength, macro_risk_off,
   catalyst_event, short_interest, price_forecast
   - sentiment_bull and sentiment_bear are SEPARATE groups — never merge them.
     If a symbol has both, they represent conflicting signals from different sources.
   - catalyst_event: Upcoming earnings, material SEC filings, or corporate events.
     Positive score = catalyst imminent. Higher score = closer/more impactful.
     Use to gauge volatility risk and event-driven opportunity.
   - short_interest: Short interest as % of float from FINRA data.
     Positive score = high short interest (squeeze potential).
     Combine with volume_spike for short-squeeze conviction.
   - insider_activity: SEC Form 4 cluster buys/sells from EDGAR.
     Recent insider buys with score > 1.0 = strong conviction signal.
   - price_forecast: TimesFM neural forecast of future price direction.
     Positive score = model forecasts upward move. Negative = downward.
     High confidence = tight quantile spread (model is certain).
     Use as a confirming signal — strong agreement with technical_trend
     and volume_spike significantly increases conviction. A contradiction
     (forecast disagrees with other signals) is a caution flag.
   WEIGHTING GUIDANCE:
   - technical_trend + volume_spike together are stronger than either alone
   - options_flow large sweeps (high score) outweigh small mixed flow
   - insider_activity is a slow but reliable signal — weight highly for 1h timeframe
   - short_interest alone is not actionable; it amplifies existing bullish signals
7. ENTRY LEVEL RULES:
   - entry.level must be a realistic current or near-term fill price
   - entry.stop must represent a logical invalidation point (support/resistance break)
   - entry.target must be technically justified (next resistance/support level)
   - Minimum reward:risk ratio of 3:1 for LONG/SHORT (target-entry > 3x entry-stop)
   - Stop distance must be proportional to timeframe volatility
8. THESIS QUALITY: thesis must explain the specific causal chain — not vague buzzwords.
   Bad: "Strong signals across multiple sources suggest upside."
   Good: "Bollinger squeeze resolving upward with 2.8x avg volume, unusual options activity (large $185c sweeps), and positive retail sentiment shift — classic breakout pattern."
9. SIGNAL QUALITY FILTERING:
   - Discard signals with confidence < 0.5 from your analysis
   - Weight higher-confidence signals more heavily in your assessment
   - If the strongest signal has score < 1.0, the setup is likely not tradeable
10. CONTRADICTION HANDLING:
   - If sentiment_bull AND sentiment_bear both present for the same symbol,
     compare their confidence scores: the higher-confidence one wins but
     reduce net sentiment conviction by 30%. If both are within 0.05 confidence,
     treat sentiment as fully neutral — do not count either toward sources_agree.
   - If technical_trend conflicts with options_flow direction, downgrade edge_probability
     by at least 0.05 and note the divergence in thesis.
   - Insider buying + bearish technical = potential bottom — weight insider signal
     more heavily at 1h but treat with caution at 15m.
   - Volume_spike without directional technical confirmation = noise, not signal.
     Do not count it toward sources_agree unless another directional source confirms.
11. Output STRICT JSON only — no prose, no markdown, no explanation outside JSON
12. DE-DUPLICATION: Review {{recent_alerts_context}} before alerting.
   - Do NOT re-alert on a symbol that was alerted in the last 2 hours
     UNLESS significant new signals have appeared (new signal type, score
     increase > 0.3, or direction reversal).
   - If you would re-alert, explain what changed in the thesis.
13. MARKET HOURS AWARENESS: Current status is {{market_hours_status}}.
   - Pre-market / After-hours: volume signals are less reliable; require
     sources_agree >= 4 and raise conviction bar.
   - Market closed (weekend/holiday): do NOT generate LONG/SHORT alerts;
     WATCH only.
   - Regular Trading Hours: normal rules apply.

Outputs MUST conform to this exact PlaybookAlert schema (all fields required
except unusual_activity which defaults to []):
  symbol: string (1-10 chars, uppercase ticker)
  direction: "LONG" | "SHORT" | "WATCH"
  edge_probability: float 0.50-0.95
  confidence: float 0.50-1.00
  timeframe: string (must match input timeframe)
  thesis: string (min 50 chars, specific causal chain)
  entry: {"level": float, "stop": float, "target": float}
  timeframe_rationale: string
  sentiment_context: string
  unusual_activity: list[string] (may be empty)
  macro_regime: string
  sources_agree: int 3-11

RECENT PERFORMANCE CONTEXT (use to calibrate your edge_probability):
{{performance_context}}
If the actual win-rate for your EP bucket is below 50%, lower your EP estimates.

{{extra_rules}}"""

_FALLBACK_USER = """\
Timeframe: {{timeframe}}
Market Hours: {{market_hours_status}}
Macro Regime: {{macro_summary}}
VIX: {{vix}} | Yield Curve: {{yc}}bps | Data: {{data_freshness}}
Snapshot age: oldest={{snapshot_age_oldest}}s, newest={{snapshot_age_newest}}s
Market reference context:
{{market_reference_context}}

Recent alerts (avoid duplicates): {{recent_alerts_context}}

Evaluate these {{n}} symbols and their signals:

{{snapshots_json}}

For each symbol where you find strong multi-source confluence, produce a PlaybookAlert.
Skip symbols with weak, single-source, or contradictory signals.
When in doubt, DO NOT alert — silence is better than a low-quality alert.

Gate requirements (ALL must pass — enforce strictly):
- edge_probability >= {{ep_gate}}
- sources_agree >= {{sa_gate}}
- average signal confidence >= {{conf_gate}}
- reward:risk >= 3:1
- thesis must be specific and causal (not generic)

Output format — a JSON array (may be empty []):
[
  {
    "symbol": "AAPL",

CRITICAL OUTPUT RULES:
- Return ONLY raw JSON (the array above).
- Do NOT wrap output in markdown/code fences.
- Do NOT add any commentary, prefixes, or suffixes.
- If no LONG/SHORT qualifies, you MUST still check for the required WATCH fallback before returning [].
- Return exactly [] ONLY when no LONG/SHORT qualifies AND no symbol satisfies the WATCH fallback rules.
    "direction": "LONG",
    "edge_probability": 0.78,
    "confidence": 0.80,
    "timeframe": "{{timeframe}}",
    "thesis": "Bollinger squeeze resolving upward with 2.8x avg volume. Unusual options activity: large $185c sweep, 500+ contracts. Retail sentiment turned bullish in last 2h. Earnings in 2 days (BMO) adds catalyst urgency. SI at 8% with 4.2 DTC provides squeeze fuel. Classic breakout pattern with multi-source confirmation.",
    "entry": {"level": 185.00, "stop": 182.00, "target": 192.00},
    "timeframe_rationale": "15m breakout aligning with 1h uptrend — momentum expected to persist 2-4 candles.",
    "sentiment_context": "ROT: strong_bullish (0.82 conf), Finnhub aggregate +0.6. Institutional flow neutral.",
    "unusual_activity": ["IV spike 2.1x avg", "options sweep $190c 0DTE 500 contracts", "earnings in 2d (BMO) — elevated implied move", "SI 8.0% / DTC 4.2 — moderate squeeze potential", "TimesFM forecast +2.1% (high confidence) — confirms breakout direction"],
    "macro_regime": "Risk-on. VIX 14.2, curve +18bps. No headwinds.",
    "sources_agree": 7
  }
]

CRITICAL CHECKS before outputting each alert:
1. Count DISTINCT signal types — sources_agree must match your actual count
2. Verify entry.target - entry.level > 2 * abs(entry.level - entry.stop)
3. Verify thesis is specific (mentions actual signal values, not just "strong signals")
4. If any required field would be vague or uncertain, do NOT include that alert

{{extra_rules}}

{{few_shot_examples}}

Return [] only if no symbols meet LONG/SHORT requirements and no symbol qualifies for the WATCH fallback.
Return ONLY the JSON array. No other text."""

# Per-timeframe extra rules injected into {{extra_rules}}
_EXTRA_RULES: dict[str, str] = {
    "15m": (
        "\nADDITIONAL 15m RULES:\n"
        "- If VIX > 25 and the macro regime is risk-off, BE MORE CAUTIOUS with LONG alerts "
        "but DO NOT suppress them entirely. In elevated-VIX risk-off environments, "
        "allow high-quality LONG setups when sources_agree >= 3 and edge_probability >= 0.72, "
        "provided the thesis explicitly explains why the setup is not a late chase. "
        "VIX 20-25 is NORMAL volatility — do not treat it as a suppression signal.\n"
        "- 15m stops should be tight (0.5-2% of entry)\n"
        "- Momentum must be FRESH — if the move already happened (score relates to "
        "a completed move), do not alert on a chase entry.\n"
        "- Prefer pullback/retest entries over stretched breakouts when risk-off+high-VIX is present.\n"
        "- BORDERLINE WATCH POLICY: When no LONG/SHORT qualifies, you MUST output "
        "exactly 1 WATCH alert if ALL criteria are met: sources_agree >= 2, "
        "confidence >= 0.60, edge_probability >= (gate - 0.05). "
        "DO NOT return [] if these criteria are met — issue the WATCH. "
        "WATCH direction is the consensus direction from aligned sources."
    ),
    "1h": (
        "\nADDITIONAL 1h RULES:\n"
        "- Treat this as a higher-timeframe swing decision layer. Use 1h structure as primary, "
        "and require confirmation from at least one longer horizon context (4h trend, daily trend, "
        "or fundamental/macro regime persistence).\n"
        "- Do NOT base a 1h alert primarily on 15m-only trigger language (e.g., '15m breakout', "
        "'next 2-4 candles', 'scalp momentum'). 15m may be used only for execution timing after "
        "a 1h thesis is already established.\n"
        "- A strong macro_risk_off signal should HEAVILY DISCOUNT long setups — "
        "require 4+ sources for LONG when macro is risk-off, but do NOT refuse "
        "to alert entirely. Exceptional confluences can override macro headwinds.\n"
        "- Entry stops and targets must reflect wider ranges appropriate "
        "for 1h holding periods (1-3% stops for equities).\n"
        "- Macro regime context weighs MORE heavily at 1h than 15m — "
        "a risk-off environment should raise the bar for longs, not eliminate them.\n"
        "- Prefer setups near key technical levels (support/resistance) rather than "
        "mid-range entries.\n"
        "- MACRO AWARENESS at 1h: weight FRED data (VIX, yield curve) in "
        "your assessment. VIX > 25 + risk-off is a headwind for longs but "
        "VIX 20-25 is normal volatility — do not suppress alerts for it.\n"
        "- De-weight intraday noise: 15m momentum spikes that haven't sustained "
        "across multiple candles are less reliable at the 1h timeframe.\n"
        "- Prefer setups with fundamental catalysts (earnings surprise, insider "
        "buying cluster, sector rotation) over pure TA patterns.\n"
        "- Thesis MUST reference at least one macro or fundamental factor, "
        "not just technical indicators.\n"
        "- 1h timeframe_rationale must describe an expected holding window in hours/days, "
        "not minutes.\n"
        "- OUTPUT CONTRACT: Return ONLY strict JSON. Start with '[' and end with ']'. "
        "No markdown fences, no prose, no prefixed labels, no trailing commentary.\n"
        "- BORDERLINE WATCH POLICY: When no LONG/SHORT qualifies, you MUST output "
        "exactly 1 WATCH alert if ALL criteria are met: sources_agree >= 2, "
        "confidence >= 0.60, edge_probability >= (gate - 0.05). "
        "DO NOT return [] if these criteria are met — issue the WATCH. "
        "WATCH direction is the consensus direction from aligned sources."
    ),
}

# Per-timeframe gate defaults
_GATE_DEFAULTS: dict[str, dict[str, str]] = {
    "15m": {"ep_gate": "0.70", "sa_gate": "3", "conf_gate": "0.75"},
    "1h": {"ep_gate": "0.75", "sa_gate": "3", "conf_gate": "0.75"},
}


def format_winrate_context() -> str:
    """Format recent win-rate data for injection into the system prompt.

    Produces a structured table of per-EP-bucket win rates so the LLM
    can self-calibrate edge_probability claims against actual outcomes.

    Returns:
        Human-readable summary of recent win-rate stats, or a default
        message if data is unavailable.
    """
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
    """Format few-shot examples from the golden dataset for the user prompt.

    Returns:
        Formatted examples string, or empty string if none available.
    """
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
    """Check recent quality scores and return escalation rules if degrading.

    Fetches the last 5 ``batch_avg_quality`` scores from Langfuse traces
    for the given timeframe. If the rolling average is below threshold,
    returns stricter rules to inject into the prompt.

    Args:
        timeframe: Pipeline timeframe (``"15m"`` or ``"1h"``).

    Returns:
        Extra rules string to append, or empty string if quality is fine.
    """
    try:
        from langfuse_client import get_langfuse_client

        lf = get_langfuse_client()
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
            return ""  # not enough data to judge

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


def compute_snapshot_freshness(snapshots_json: str) -> dict[str, int]:
    """Compute age of oldest and newest snapshots for prompt injection.

    Parses ISO 8601 timestamps from snapshot objects and computes
    the delta to now.

    Args:
        snapshots_json: JSON string of Snapshot dicts (each with a
            ``timestamp`` field in ISO 8601 format).

    Returns:
        Dict with ``oldest_seconds`` and ``newest_seconds`` age values.
        Returns ``{"oldest_seconds": 0, "newest_seconds": 0}`` when
        timestamps cannot be parsed.
    """
    try:
        snaps = json.loads(snapshots_json)
        timestamps: list[datetime] = []
        for s in snaps:
            ts_str = s.get("timestamp", "")
            if ts_str:
                # Handle both timezone-aware and naive (assume UTC) strings
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                timestamps.append(dt)
        if not timestamps:
            return {"oldest_seconds": 0, "newest_seconds": 0}
        now = datetime.now(timezone.utc)
        oldest = int((now - min(timestamps)).total_seconds())
        newest = int((now - max(timestamps)).total_seconds())
        return {"oldest_seconds": max(oldest, 0), "newest_seconds": max(newest, 0)}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"oldest_seconds": 0, "newest_seconds": 0}


# Module-level cache for last prompt source
_last_source: str = "not-loaded"
_last_version: str = "yaml-fallback"

# TTL cache for Langfuse prompt objects (avoids repeated API calls)
_prompt_cache: dict[str, tuple[float, Any, Any]] = {}  # key → (ts, sys_obj, usr_obj)
_PROMPT_CACHE_TTL: float = 300.0  # seconds
_prompt_cache_lock = threading.Lock()


def _compile_template(template: str, variables: dict[str, Any]) -> str:
    """Replace ``{{var}}`` placeholders with values from *variables*.

    Variable values are sanitized to prevent template injection:
    ``}}`` sequences in values are escaped to ``} }``.

    Args:
        template: Prompt template with ``{{key}}`` placeholders.
        variables: Mapping of placeholder name → replacement value.

    Returns:
        Compiled prompt string.
    """
    result = template
    for key, value in variables.items():
        safe_value = str(value).replace("}}", "} }")
        result = result.replace("{{" + key + "}}", safe_value)
    return result


def _check_unresolved_placeholders(text: str, label: str) -> None:
    """Raise if any ``{{...}}`` placeholders remain after compilation.

    Args:
        text: Compiled prompt text.
        label: Human-readable label for the error (e.g. ``"system"``).

    Raises:
        ValueError: If unresolved placeholders are found.
    """
    import re

    remaining = re.findall(r"\{\{(\w+)\}\}", text)
    if remaining:
        raise ValueError(
            f"Unresolved placeholders in {label} prompt: " + ", ".join(f"{{{{{v}}}}}" for v in remaining)
        )


# get_market_hours_status is imported from constants.
# Re-exporting here so callers of prompt_manager.get_market_hours_status() still work.


def get_recent_alerts_context(hours: int = 2, limit: int = 10) -> str:
    """Build a de-duplication context string from recent alerts.

    Queries the alerts table for alerts fired within the look-back
    window so the LLM can avoid duplicate alerting.

    Args:
        hours: Look-back window in hours.
        limit: Maximum recent alerts to include.

    Returns:
        Human-readable summary, or ``"None in the last 2 hours."``
        if no recent alerts exist.
    """
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
            age_min = int(
                (datetime.now(timezone.utc) - r["created_at"].replace(tzinfo=timezone.utc)).total_seconds()
                / 60
            )
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
    """Return compiled (system, user) prompts for the decision engine.

    Tries the Langfuse Prompt Management API first.  If that fails
    (credentials missing, network error, prompt not found) falls
    back to built-in templates identical to the original YAML.

    Args:
        timeframe: Pipeline timeframe (``"15m"`` or ``"1h"``).
        variables: Context dict with keys: ``macro_summary``, ``vix``,
            ``yc``, ``n``, ``snapshots_json``.  Gate thresholds
            (``ep_gate``, ``sa_gate``, ``conf_gate``) are auto-filled
            from per-timeframe defaults if not provided.

    Returns:
        Tuple of ``(system_prompt, user_prompt)`` ready to send to the LLM.
    """
    global _last_source, _last_version  # noqa: PLW0603

    # Merge timeframe defaults into variables
    merged = {
        "timeframe": timeframe,
        "extra_rules": _EXTRA_RULES.get(timeframe, ""),
        "data_freshness": "LIVE",
        "market_reference_context": "",
        "performance_context": "No recent outcome data available yet.",
        "few_shot_examples": "",
        "snapshot_age_oldest": "0",
        "snapshot_age_newest": "0",
        "market_hours_status": get_market_hours_status(),
        "recent_alerts_context": get_recent_alerts_context(),
        **_GATE_DEFAULTS.get(timeframe, _GATE_DEFAULTS["15m"]),
        **variables,
    }

    # ── Inject snapshot data freshness (Group 3a) ────────────────
    snap_json = merged.get("snapshots_json", "")
    if snap_json and snap_json != "[]":
        freshness = compute_snapshot_freshness(str(snap_json))
        merged["snapshot_age_oldest"] = str(freshness["oldest_seconds"])
        merged["snapshot_age_newest"] = str(freshness["newest_seconds"])

    # ── Build dynamic system prompt warnings ─────────────────────
    _warnings: list[str] = []
    # 3a: Stale snapshot warning (> 20 min)
    if int(merged["snapshot_age_oldest"]) > 1200:
        _warnings.append(
            f"⚠️ SIGNAL FRESHNESS WARNING: Oldest snapshot is "
            f"{merged['snapshot_age_oldest']}s old. "
            f"Downgrade confidence on time-sensitive signals."
        )
    # 3b: FRED data freshness — CACHED sentinel set by decision YAML
    #     when FRED MCP is unreachable and cached values are used.
    if str(merged.get("data_freshness", "")).startswith("CACHED"):
        _warnings.append(
            "⚠️ MACRO DATA WARNING: VIX/yield data is stale "
            "(FRED unavailable). Downgrade confidence on "
            "macro-sensitive signals."
        )

    # ── Inject historical signal accuracy (Group 4b) ─────────────
    # Appends per-bucket win-rate breakdown to the system prompt so
    # the LLM can self-calibrate its edge_probability estimates.
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

    # ── Try Langfuse first ───────────────────────────────────────
    import time as _time

    cache_key = timeframe
    with _prompt_cache_lock:
        cached = _prompt_cache.get(cache_key)
    if cached:
        ts, sys_obj, usr_obj = cached
        if (_time.monotonic() - ts) < _PROMPT_CACHE_TTL:
            try:
                # Validate version hasn't changed before using cache
                cached_version = str(getattr(sys_obj, "version", "unknown"))
                system = sys_obj.compile(**merged)
                user = usr_obj.compile(**merged)
                if str(merged.get("market_reference_context", "")).strip():
                    user = (
                        "Current market reference prices (use these to calibrate "
                        "entry/stop/target near live levels):\n"
                        f"{merged['market_reference_context']}\n\n{user}"
                    )
                _last_source = "langfuse"
                _last_version = cached_version
                if _warnings:
                    system = "\n".join(_warnings) + "\n\n" + system
                _check_unresolved_placeholders(system, "system")
                _check_unresolved_placeholders(user, "user")
                return (system, user)
            except (KeyError, TypeError, ValueError, RuntimeError):
                pass  # stale/broken cache entry — refetch below

    lf = get_langfuse_client()
    if lf is not None:
        try:
            sys_prompt_obj = lf.get_prompt("decision-system", label="production")
            usr_prompt_obj = lf.get_prompt("decision-user", label="production")
            with _prompt_cache_lock:
                _prompt_cache[cache_key] = (_time.monotonic(), sys_prompt_obj, usr_prompt_obj)
            system = sys_prompt_obj.compile(**merged)
            user = usr_prompt_obj.compile(**merged)
            if str(merged.get("market_reference_context", "")).strip():
                user = (
                    "Current market reference prices (use these to calibrate "
                    "entry/stop/target near live levels):\n"
                    f"{merged['market_reference_context']}\n\n{user}"
                )
            _last_source = "langfuse"
            _last_version = str(getattr(sys_prompt_obj, "version", "unknown"))
            logger.info("Prompts loaded from Langfuse (version=%s)", _last_version)
            if _warnings:
                system = "\n".join(_warnings) + "\n\n" + system
            _check_unresolved_placeholders(system, "system")
            _check_unresolved_placeholders(user, "user")
            return (system, user)
        except Exception as exc:  # noqa: BLE001 - missing prompts or auth issues should fall back cleanly
            register_langfuse_failure(exc)
            logger.warning("Langfuse prompt fetch failed — using YAML fallback: %s", exc)

    # ── Fallback to built-in templates ───────────────────────────
    system = _compile_template(_FALLBACK_SYSTEM, merged)
    user = _compile_template(_FALLBACK_USER, merged)
    _last_source = "yaml-fallback"
    _last_version = "yaml-fallback"
    logger.info("Prompts loaded from YAML fallback (timeframe=%s)", timeframe)

    # Prepend any freshness warnings to the compiled system prompt
    if _warnings:
        system = "\n".join(_warnings) + "\n\n" + system
    _check_unresolved_placeholders(system, "system")
    _check_unresolved_placeholders(user, "user")
    return (system, user)


def get_prompt_version() -> str:
    """Return the version tag of the last loaded prompts.

    Returns:
        Langfuse prompt version string, or ``"yaml-fallback"`` if
        the built-in templates were used.
    """
    return _last_version


def get_prompt_source() -> str:
    """Return ``"langfuse"`` or ``"yaml-fallback"``."""
    return _last_source


def get_gate_defaults() -> dict[str, dict[str, str]]:
    """Return per-timeframe gate threshold defaults for generation metadata.

    Returns:
        Dict mapping timeframe to gate thresholds (ep, sa, conf).
    """
    return dict(_GATE_DEFAULTS)
