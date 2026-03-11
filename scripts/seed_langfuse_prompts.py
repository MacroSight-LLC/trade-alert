"""Seed Langfuse Prompt Management with decision-system and decision-user prompts.

Run once after Langfuse is up:
    python scripts/seed_langfuse_prompts.py

Creates the ``decision-system`` and ``decision-user`` text prompts
in Langfuse with the label ``production`` so that
``prompt_manager.get_decision_prompts()`` can fetch them from the UI
instead of falling back to hardcoded templates.

Variables use Langfuse ``{{var}}`` syntax, matching prompt_manager.py.
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import vault_env_loader  # noqa: F401, E402
from langfuse_client import get_langfuse_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
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

USER_PROMPT = """\
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


def seed() -> None:
    """Create or update prompts in Langfuse."""
    lf = get_langfuse_client()
    if lf is None:
        logger.error(
            "Langfuse client unavailable — set LANGFUSE_PUBLIC_KEY, "
            "LANGFUSE_SECRET_KEY, and LANGFUSE_HOST"
        )
        sys.exit(1)

    for name, text in [
        ("decision-system", SYSTEM_PROMPT),
        ("decision-user", USER_PROMPT),
    ]:
        try:
            lf.create_prompt(
                name=name,
                prompt=text,
                labels=["production"],
                type="text",
            )
            logger.info("Created prompt '%s' with label 'production'", name)
        except Exception as exc:
            # If prompt already exists, Langfuse raises — log and continue
            logger.warning("Prompt '%s' may already exist: %s", name, exc)

    lf.flush()
    logger.info("Done — prompts seeded in Langfuse")


if __name__ == "__main__":
    seed()
