"""Regime classification and dynamic gate overlays."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# ── EP ceiling lookup table ──────────────────────────────────────
# Maps the number of *actual* distinct signal types in the snapshot
# to the maximum allowed edge_probability.  Prevents the LLM from
# assigning inflated EP values that aren't supported by the evidence.
# Overridable via EP_CEILING_JSON env var (JSON dict str→float).
_DEFAULT_EP_CEILING: dict[int, float] = {
    1: 0.55,
    2: 0.65,
    3: 0.75,
    4: 0.85,
    5: 0.90,
    6: 0.92,
    7: 0.95,
    8: 0.96,
    9: 0.97,
    10: 0.98,
    11: 0.99,
}


def _load_ep_ceiling() -> dict[int, float]:
    """Load EP ceiling from EP_CEILING_JSON env var or use defaults.

    Returns:
        Mapping of source count → max edge_probability.
    """
    raw = os.environ.get("EP_CEILING_JSON", "")
    if raw:
        try:
            parsed = json.loads(raw)
            return {int(k): float(v) for k, v in parsed.items()}
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("Invalid EP_CEILING_JSON, using defaults: %s", e)
    return dict(_DEFAULT_EP_CEILING)


EP_CEILING: dict[int, float] = _load_ep_ceiling()

# Dynamic gate controls (regime + timeframe overlays)
_DYNAMIC_GATES_ENABLED: bool = os.environ.get("DYNAMIC_GATES_ENABLED", "1") == "1"
_REGIME_CHOPPY_EP_BUMP: float = float(os.environ.get("REGIME_CHOPPY_EP_BUMP", "0.03"))
_REGIME_CHOPPY_CONF_BUMP: float = float(os.environ.get("REGIME_CHOPPY_CONF_BUMP", "0.03"))
_REGIME_CHOPPY_SA_BUMP: int = int(os.environ.get("REGIME_CHOPPY_SA_BUMP", "1"))
_REGIME_TRENDING_EP_REDUCE: float = float(os.environ.get("REGIME_TRENDING_EP_REDUCE", "0.01"))
_REGIME_TRENDING_CONF_REDUCE: float = float(os.environ.get("REGIME_TRENDING_CONF_REDUCE", "0.01"))
# risk_off_high_vix regime (VIX 25–30 + risk_off=True): tighter gates, same magnitude as choppy.
# Previously this regime was classified but completely unhandled — gates fell through unchanged.
_REGIME_RISK_OFF_HIGH_VIX_EP_BUMP: float = float(os.environ.get("REGIME_RISK_OFF_HIGH_VIX_EP_BUMP", "0.03"))
_REGIME_RISK_OFF_HIGH_VIX_CONF_BUMP: float = float(
    os.environ.get("REGIME_RISK_OFF_HIGH_VIX_CONF_BUMP", "0.03")
)
_REGIME_RISK_OFF_HIGH_VIX_SA_BUMP: int = int(os.environ.get("REGIME_RISK_OFF_HIGH_VIX_SA_BUMP", "1"))
_TF_EP_OFFSET_15M: float = float(os.environ.get("TF_EP_OFFSET_15M", "0.00"))
_TF_EP_OFFSET_1H: float = float(os.environ.get("TF_EP_OFFSET_1H", "0.00"))
_TF_CONF_OFFSET_15M: float = float(os.environ.get("TF_CONF_OFFSET_15M", "0.00"))
_TF_CONF_OFFSET_1H: float = float(os.environ.get("TF_CONF_OFFSET_1H", "0.00"))


def _signal_surface(snaps: list[dict[str, Any]]) -> tuple[int, int, float]:
    bulls = 0
    bears = 0
    strengths: list[float] = []
    for snap in snaps:
        for sig in snap.get("signals", []):
            st = sig.get("type", "")
            try:
                sc = float(sig.get("score", 0.0))
            except (TypeError, ValueError):
                continue
            if sc > 0 and st in (
                "technical_trend",
                "sentiment_bull",
                "options_flow",
                "relative_strength",
                "price_forecast",
                "insider_activity",
                "catalyst_event",
            ):
                bulls += 1
                strengths.append(min(abs(sc) / 3.0, 1.0))
            elif sc < 0 and st in (
                "technical_trend",
                "options_flow",
                "relative_strength",
                "price_forecast",
                "insider_activity",
                "catalyst_event",
            ):
                bears += 1
                strengths.append(min(abs(sc) / 3.0, 1.0))
            elif st in ("sentiment_bear", "macro_risk_off") and sc > 0:
                bears += 1
                strengths.append(min(abs(sc) / 3.0, 1.0))
            elif st == "short_interest" and sc > 0:
                # High short interest amplifies existing bulls (squeeze potential)
                bulls += 1
                strengths.append(min(abs(sc) / 3.0, 1.0))
    trend_strength = sum(strengths) / len(strengths) if strengths else 0.0
    return bulls, bears, trend_strength


def _classify_regime(vix: float, risk_off: bool, bulls: int, bears: int, trend_strength: float) -> str:
    """Classify market regime for dynamic gate overlays (SSOT §10.2 regime classification)."""
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


def _dynamic_gates(
    base_ep: float, base_sa: int, base_conf: float, timeframe: str, regime: str
) -> tuple[float, int, float]:
    """Apply regime/timeframe overlays to base gate thresholds (SSOT §10.2 dynamic gates)."""
    import validate_and_filter as vf

    ep = base_ep
    sa = base_sa
    conf = base_conf
    if not vf._DYNAMIC_GATES_ENABLED:
        return ep, sa, conf
    if regime == "choppy":
        ep += _REGIME_CHOPPY_EP_BUMP
        sa += _REGIME_CHOPPY_SA_BUMP
        conf += _REGIME_CHOPPY_CONF_BUMP
    elif regime == "risk_off_high_vix":
        # Previously unhandled — gates fell through unchanged.
        # Now applies a matching tightening: elevated conviction required
        # when VIX is 25–30 and macro is risk-off.
        ep += _REGIME_RISK_OFF_HIGH_VIX_EP_BUMP
        sa += _REGIME_RISK_OFF_HIGH_VIX_SA_BUMP
        conf += _REGIME_RISK_OFF_HIGH_VIX_CONF_BUMP
    elif regime in ("trending_up", "trending_down"):
        ep -= _REGIME_TRENDING_EP_REDUCE
        conf -= _REGIME_TRENDING_CONF_REDUCE

    if timeframe == "15m":
        ep += _TF_EP_OFFSET_15M
        conf += _TF_CONF_OFFSET_15M
    elif timeframe == "1h":
        ep += _TF_EP_OFFSET_1H
        conf += _TF_CONF_OFFSET_1H

    ep = min(max(ep, 0.50), 0.95)
    sa = max(sa, 1)
    conf = min(max(conf, 0.50), 0.99)
    return ep, sa, conf
