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
You are a quantitative trading signal evaluator.
You receive normalized market signals from multiple independent sources
(technical analysis, volume/flow, sentiment, order book, macro regime).

Your job: evaluate signal confluence and produce trade playbook alerts
ONLY when multiple independent signal types agree.

Rules:
- Only output alerts where evidence is strong and multi-source
- Prefer WATCH over LONG/SHORT when conviction is low
- Be conservative: a missed opportunity is better than a bad trade
- edge_probability reflects how many independent signal groups agree
  AND how strong/confident those signals are
- sources_agree = count of distinct signal types pointing same direction
  (technical_trend, volume_spike, sentiment_bull/bear,
   order_imbalance_long/short, macro_risk_off)
- Output STRICT JSON only — no prose, no markdown, no explanation
{{extra_rules}}"""

_FALLBACK_USER = """\
Timeframe: {{timeframe}}
Macro Regime: {{macro_summary}}
VIX: {{vix}} | Yield Curve: {{yc}}bps

Evaluate these {{n}} symbols and their signals:

{{snapshots_json}}

For each symbol where you find strong multi-source confluence, output
a PlaybookAlert. Skip symbols with weak or single-source signals.

Gate requirements (must ALL pass):
- edge_probability >= {{ep_gate}}
- sources_agree >= {{sa_gate}}
- average signal confidence >= {{conf_gate}}

Output format — a JSON array (may be empty []):
[
  {
    "symbol": "AAPL",
    "direction": "LONG",
    "edge_probability": 0.78,
    "confidence": 0.80,
    "timeframe": "{{timeframe}}",
    "thesis": "One or two sentence causal explanation of WHY this trade.",
    "entry": {"level": 185.00, "stop": 182.00, "target": 192.00},
    "timeframe_rationale": "Why {{timeframe}} is the right timeframe for this.",
    "sentiment_context": "Retail and institutional sentiment summary.",
    "unusual_activity": ["IV spike 2x avg", "options sweep $190c"],
    "macro_regime": "Risk-on, VIX 14, curve normal.",
    "sources_agree": 4
  }
]

{{extra_rules}}

Return [] if no symbols meet the gate requirements.
Return ONLY the JSON array. No other text."""

# Per-timeframe extra rules injected into {{extra_rules}}
_EXTRA_RULES: dict[str, str] = {
    "15m": "",
    "1h": (
        "- For 1h timeframe, a strong macro_risk_off signal (score >= 2.0) "
        "should veto otherwise valid long setups.\n"
        "Note: entry stops and targets should reflect wider ranges appropriate "
        "for 1h holding periods. Macro regime context weighs more heavily — "
        "a strong risk-off environment should suppress long setups."
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
    lf = get_langfuse_client()
    if lf is not None:
        try:
            sys_prompt_obj = lf.get_prompt("decision-system", label="production")
            usr_prompt_obj = lf.get_prompt("decision-user", label="production")
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
