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
    timeframe, macro_summary, vix, yc, n, snapshots_json,
    ep_gate, sa_gate, conf_gate, extra_rules
"""

from __future__ import annotations

import logging
from typing import Any

from langfuse_client import get_langfuse_client

logger = logging.getLogger(__name__)

# ── Fallback prompts (verbatim from decision-15m / decision-1h YAML) ─────

_FALLBACK_SYSTEM = """\
You are an elite quantitative trading signal evaluator running in a production alert engine.
You receive normalized market signals from multiple independent sources
(technical analysis, volume/flow, sentiment, order book, macro regime).

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
   Valid groups: technical_trend, volume_spike, sentiment_bull/bear,
   order_imbalance_long/short, macro_risk_off
7. ENTRY LEVEL RULES:
   - entry.level must be a realistic current or near-term fill price
   - entry.stop must represent a logical invalidation point (support/resistance break)
   - entry.target must be technically justified (next resistance/support level)
   - Minimum reward:risk ratio of 2:1 for LONG/SHORT (target-entry > 2x entry-stop)
   - Stop distance must be proportional to timeframe volatility
8. THESIS QUALITY: thesis must explain the specific causal chain — not vague buzzwords.
   Bad: "Strong signals across multiple sources suggest upside."
   Good: "Bollinger squeeze resolving upward with 2.8x avg volume, bullish order book imbalance (65/35), and positive retail sentiment shift — classic breakout pattern."
9. SIGNAL QUALITY FILTERING:
   - Discard signals with confidence < 0.5 from your analysis
   - Weight higher-confidence signals more heavily in your assessment
   - If the strongest signal has score < 1.0, the setup is likely not tradeable
10. CONTRADICTION HANDLING:
   - If sentiment_bull AND sentiment_bear both present, they cancel — treat as neutral
   - If technical_trend conflicts with order_imbalance direction, downgrade edge_probability
   - Volume_spike without directional technical confirmation = noise, not signal
11. Output STRICT JSON only — no prose, no markdown, no explanation outside JSON
{{extra_rules}}"""

_FALLBACK_USER = """\
Timeframe: {{timeframe}}
Macro Regime: {{macro_summary}}
VIX: {{vix}} | Yield Curve: {{yc}}bps

Evaluate these {{n}} symbols and their signals:

{{snapshots_json}}

For each symbol where you find strong multi-source confluence, produce a PlaybookAlert.
Skip symbols with weak, single-source, or contradictory signals.
When in doubt, DO NOT alert — silence is better than a low-quality alert.

Gate requirements (ALL must pass — enforce strictly):
- edge_probability >= {{ep_gate}}
- sources_agree >= {{sa_gate}}
- average signal confidence >= {{conf_gate}}
- reward:risk >= 2:1
- thesis must be specific and causal (not generic)

Output format — a JSON array (may be empty []):
[
  {
    "symbol": "AAPL",
    "direction": "LONG",
    "edge_probability": 0.78,
    "confidence": 0.80,
    "timeframe": "{{timeframe}}",
    "thesis": "Bollinger squeeze resolving upward with 2.8x avg volume. Order book shows 65/35 buy-side imbalance at $185 level. Retail sentiment turned bullish in last 2h. Classic breakout pattern with volume confirmation.",
    "entry": {"level": 185.00, "stop": 182.00, "target": 192.00},
    "timeframe_rationale": "15m breakout aligning with 1h uptrend — momentum expected to persist 2-4 candles.",
    "sentiment_context": "ROT: strong_bullish (0.82 conf), Finnhub aggregate +0.6. Institutional flow neutral.",
    "unusual_activity": ["IV spike 2.1x avg", "options sweep $190c 0DTE 500 contracts"],
    "macro_regime": "Risk-on. VIX 14.2, curve +18bps. No headwinds.",
    "sources_agree": 4
  }
]

CRITICAL CHECKS before outputting each alert:
1. Count DISTINCT signal types — sources_agree must match your actual count
2. Verify entry.target - entry.level > 2 * abs(entry.level - entry.stop)
3. Verify thesis is specific (mentions actual signal values, not just "strong signals")
4. If any required field would be vague or uncertain, do NOT include that alert

{{extra_rules}}

Return [] if no symbols meet ALL requirements.
Return ONLY the JSON array. No other text."""

# Per-timeframe extra rules injected into {{extra_rules}}
_EXTRA_RULES: dict[str, str] = {
    "15m": (
        "\nADDITIONAL 15m RULES:\n"
        "- If VIX > 20 and the macro regime is risk-off, suppress LONG alerts "
        "unless sources_agree >= 4 and edge_probability >= 0.80. "
        "In elevated-volatility environments, only the strongest confluences "
        "justify short-timeframe longs.\n"
        "- 15m stops should be tight (0.5-2% of entry for equities, 1-3% for crypto)\n"
        "- Momentum must be FRESH — if the move already happened (score relates to "
        "a completed move), do not alert on a chase entry."
    ),
    "1h": (
        "\nADDITIONAL 1h RULES:\n"
        "- A strong macro_risk_off signal (score >= 2.0) VETOES all long setups — "
        "do not output LONG alerts when macro is strongly risk-off.\n"
        "- Entry stops and targets must reflect wider ranges appropriate "
        "for 1h holding periods (1-3% stops for equities, 2-5% for crypto).\n"
        "- Macro regime context weighs MORE heavily at 1h than 15m — "
        "a risk-off environment should suppress long setups unless 4+ sources agree.\n"
        "- Prefer setups near key technical levels (support/resistance) rather than "
        "mid-range entries."
    ),
}

# Per-timeframe gate defaults
_GATE_DEFAULTS: dict[str, dict[str, str]] = {
    "15m": {"ep_gate": "0.70", "sa_gate": "3", "conf_gate": "0.75"},
    "1h": {"ep_gate": "0.75", "sa_gate": "3", "conf_gate": "0.75"},
}

# Module-level cache for last prompt source
_last_source: str = "not-loaded"
_last_version: str = "yaml-fallback"

# TTL cache for Langfuse prompt objects (avoids repeated API calls)
_prompt_cache: dict[str, tuple[float, Any, Any]] = {}  # key → (ts, sys_obj, usr_obj)
_PROMPT_CACHE_TTL: float = 300.0  # seconds


def _compile_template(template: str, variables: dict[str, Any]) -> str:
    """Replace ``{{var}}`` placeholders with values from *variables*.

    Args:
        template: Prompt template with ``{{key}}`` placeholders.
        variables: Mapping of placeholder name → replacement value.

    Returns:
        Compiled prompt string.
    """
    result = template
    for key, value in variables.items():
        result = result.replace("{{" + key + "}}", str(value))
    return result


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
        **_GATE_DEFAULTS.get(timeframe, _GATE_DEFAULTS["15m"]),
        **variables,
    }

    # ── Try Langfuse first ───────────────────────────────────────
    import time as _time

    cache_key = timeframe
    cached = _prompt_cache.get(cache_key)
    if cached:
        ts, sys_obj, usr_obj = cached
        if (_time.monotonic() - ts) < _PROMPT_CACHE_TTL:
            try:
                system = sys_obj.compile(**merged)
                user = usr_obj.compile(**merged)
                _last_source = "langfuse"
                _last_version = str(getattr(sys_obj, "version", "unknown"))
                return (system, user)
            except Exception:  # noqa: BLE001
                pass  # stale/broken cache entry — refetch below

    lf = get_langfuse_client()
    if lf is not None:
        try:
            sys_prompt_obj = lf.get_prompt("decision-system", label="production")
            usr_prompt_obj = lf.get_prompt("decision-user", label="production")
            _prompt_cache[cache_key] = (_time.monotonic(), sys_prompt_obj, usr_prompt_obj)
            system = sys_prompt_obj.compile(**merged)
            user = usr_prompt_obj.compile(**merged)
            _last_source = "langfuse"
            _last_version = str(getattr(sys_prompt_obj, "version", "unknown"))
            logger.info("Prompts loaded from Langfuse (version=%s)", _last_version)
            return (system, user)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse prompt fetch failed — using YAML fallback: %s", exc)

    # ── Fallback to built-in templates ───────────────────────────
    system = _compile_template(_FALLBACK_SYSTEM, merged)
    user = _compile_template(_FALLBACK_USER, merged)
    _last_source = "yaml-fallback"
    _last_version = "yaml-fallback"
    logger.info("Prompts loaded from YAML fallback (timeframe=%s)", timeframe)
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
