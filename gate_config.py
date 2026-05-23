"""Single source of truth for validation gate thresholds (SSOT §10.2 / §10.3)."""

from __future__ import annotations

import os

GATE_EP: dict[str, float] = {
    "15m": float(os.environ.get("GATE_EP_15M", "0.70")),
    "1h": float(os.environ.get("GATE_EP_1H", "0.75")),
}
GATE_SA: int = int(os.environ.get("GATE_SA", "4"))
GATE_CONF: float = float(os.environ.get("GATE_CONF", "0.75"))
GATE_RR: dict[str, float] = {
    "15m": float(os.environ.get("GATE_RR_15M", "2.0")),
    "1h": float(os.environ.get("GATE_RR_1H", "2.5")),
}

# Prompt-facing string defaults (aligned with GATE_* above).
GATE_PROMPT_DEFAULTS: dict[str, dict[str, str]] = {
    "15m": {
        "ep_gate": f"{GATE_EP['15m']:.2f}",
        "sa_gate": str(GATE_SA),
        "conf_gate": f"{GATE_CONF:.2f}",
        "rr_gate": f"{GATE_RR['15m']:.1f}",
    },
    "1h": {
        "ep_gate": f"{GATE_EP['1h']:.2f}",
        "sa_gate": str(GATE_SA),
        "conf_gate": f"{GATE_CONF:.2f}",
        "rr_gate": f"{GATE_RR['1h']:.1f}",
    },
}


EXTENDED_HOURS_ALERTS_ENABLED: bool = os.environ.get("EXTENDED_HOURS_ALERTS_ENABLED", "0") == "1"
EXTENDED_HOURS_CONFIDENCE_PENALTY: float = float(os.environ.get("EXTENDED_HOURS_CONFIDENCE_PENALTY", "-0.10"))

# WATCH policy caps (SSOT §10.2)
WATCH_MAX_PER_RUN: int = int(os.environ.get("WATCH_MAX_PER_RUN", "1"))
WATCH_MAX_STRESSED: int = int(os.environ.get("WATCH_MAX_STRESSED", str(WATCH_MAX_PER_RUN)))
WATCH_MAX_NEUTRAL: int = int(os.environ.get("WATCH_MAX_NEUTRAL", "2"))
WATCH_MAX_TRENDING: int = int(os.environ.get("WATCH_MAX_TRENDING", "3"))
WATCH_DECAY_TTL_SECONDS: int = int(os.environ.get("WATCH_DECAY_TTL_SECONDS", str(60 * 60 * 24)))


def classify_regime(
    vix: float,
    risk_off: bool,
    bulls: int,
    bears: int,
    trend_strength: float,
) -> str:
    """Classify market regime for dynamic gate overlays (SSOT §10.2)."""
    total = bulls + bears
    bull_ratio = (bulls / total) if total else 0.5
    if vix > 30:
        return "extreme"
    if risk_off and vix >= 25:
        return "risk_off_high_vix"
    if trend_strength < 0.35 or (0.45 <= bull_ratio <= 0.55):
        return "choppy"
    if bull_ratio > 0.55 and not risk_off:
        return "trending_up"
    if bull_ratio < 0.45 and risk_off:
        return "trending_down"
    return "neutral"


def prompt_gate_vars(timeframe: str) -> dict[str, str]:
    """Return gate template variables for decision prompts."""
    return dict(GATE_PROMPT_DEFAULTS.get(timeframe, GATE_PROMPT_DEFAULTS["15m"]))
