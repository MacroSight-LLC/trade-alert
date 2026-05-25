"""Single source of truth for validation gate thresholds (SSOT §10.2 / §10.3)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gates.candidate import CandidateGateConfig

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


@dataclass(frozen=True)
class GateConfig:
    """Unified gate thresholds — global (module-level) + per-candidate runtime fields."""

    sa_family_min_score: float
    sa_include_macro_context: bool
    sa_macro_context_score: float
    sa_forecast_confirm_bonus_enabled: bool
    sa_forecast_bonus_threshold: float
    high_confidence_min: float
    high_confidence_min_sa: int
    macro_veto_sa: int
    macro_veto_ep: float
    vix_soft_threshold: float
    vix_soft_sa: int
    vix_soft_ep: float
    watch_sa_min: int
    watch_conf_min: float
    watch_ep_delta: float
    forecast_gate_score_threshold: float
    forecast_gate_sa: int
    forecast_gate_ep: float
    volume_confirm_score: float
    volume_confirm_penalty: float
    volume_confirm_penalty_choppy: float
    entry_market_drift_max_pct: float
    entry_market_drift_vix_bump: float
    entry_market_drift_prepost_bump: float
    entry_market_drift_cap_pct: float
    entry_market_drift_vix_high_threshold: float
    entry_market_drift_vix_high_bump: float
    market_hours_gates_enabled: bool

    @classmethod
    def from_env(cls) -> GateConfig:
        return cls(
            sa_family_min_score=float(os.environ.get("SA_FAMILY_MIN_SCORE", "0.25")),
            sa_include_macro_context=os.environ.get("SA_INCLUDE_MACRO_CONTEXT", "1") == "1",
            sa_macro_context_score=float(os.environ.get("SA_MACRO_CONTEXT_SCORE", "0.50")),
            sa_forecast_confirm_bonus_enabled=os.environ.get("SA_FORECAST_CONFIRM_BONUS_ENABLED", "1") == "1",
            sa_forecast_bonus_threshold=float(os.environ.get("SA_FORECAST_BONUS_THRESHOLD", "0.80")),
            high_confidence_min=float(os.environ.get("HIGH_CONFIDENCE_MIN", "0.85")),
            high_confidence_min_sa=int(os.environ.get("HIGH_CONFIDENCE_MIN_SA", "5")),
            macro_veto_sa=int(os.environ.get("MACRO_VETO_SA", "6")),
            macro_veto_ep=float(os.environ.get("MACRO_VETO_EP", "0.90")),
            vix_soft_threshold=float(os.environ.get("VIX_SOFT_THRESHOLD", "25.0")),
            vix_soft_sa=int(os.environ.get("VIX_SOFT_SA", "3")),
            vix_soft_ep=float(os.environ.get("VIX_SOFT_EP", "0.72")),
            watch_sa_min=int(os.environ.get("WATCH_SA_MIN", "2")),
            watch_conf_min=float(os.environ.get("WATCH_CONF_MIN", "0.60")),
            watch_ep_delta=float(os.environ.get("WATCH_EP_DELTA", "0.05")),
            forecast_gate_score_threshold=float(os.environ.get("FORECAST_GATE_SCORE_THRESHOLD", "0.8")),
            forecast_gate_sa=int(os.environ.get("FORECAST_GATE_SA", "5")),
            forecast_gate_ep=float(os.environ.get("FORECAST_GATE_EP", "0.85")),
            volume_confirm_score=float(os.environ.get("VOLUME_CONFIRM_SCORE", "1.5")),
            volume_confirm_penalty=float(os.environ.get("VOLUME_CONFIRM_PENALTY", "0.05")),
            volume_confirm_penalty_choppy=float(os.environ.get("VOLUME_CONFIRM_PENALTY_CHOPPY", "0.10")),
            entry_market_drift_max_pct=float(os.environ.get("ENTRY_MARKET_DRIFT_MAX_PCT", "0.03")),
            entry_market_drift_vix_bump=float(os.environ.get("ENTRY_MARKET_DRIFT_VIX_BUMP", "0.01")),
            entry_market_drift_prepost_bump=float(os.environ.get("ENTRY_MARKET_DRIFT_PREPOST_BUMP", "0.01")),
            entry_market_drift_cap_pct=float(os.environ.get("ENTRY_MARKET_DRIFT_CAP_PCT", "0.08")),
            entry_market_drift_vix_high_threshold=float(
                os.environ.get("ENTRY_MARKET_DRIFT_VIX_HIGH_THRESHOLD", "30.0")
            ),
            entry_market_drift_vix_high_bump=float(
                os.environ.get("ENTRY_MARKET_DRIFT_VIX_HIGH_BUMP", "0.02")
            ),
            market_hours_gates_enabled=os.environ.get("MARKET_HOURS_GATES_ENABLED", "1") == "1",
        )

    @classmethod
    def from_module(cls, mod: object) -> GateConfig:
        """Load from a module namespace (supports validate_and_filter test monkeypatch)."""
        return cls(
            sa_family_min_score=mod._SA_FAMILY_MIN_SCORE,
            sa_include_macro_context=mod._SA_INCLUDE_MACRO_CONTEXT,
            sa_macro_context_score=mod._SA_MACRO_CONTEXT_SCORE,
            sa_forecast_confirm_bonus_enabled=mod._SA_FORECAST_CONFIRM_BONUS_ENABLED,
            sa_forecast_bonus_threshold=mod._SA_FORECAST_BONUS_THRESHOLD,
            high_confidence_min=mod._HIGH_CONFIDENCE_MIN,
            high_confidence_min_sa=mod._HIGH_CONFIDENCE_MIN_SA,
            macro_veto_sa=mod._MACRO_VETO_SA,
            macro_veto_ep=mod._MACRO_VETO_EP,
            vix_soft_threshold=mod._VIX_SOFT_THRESHOLD,
            vix_soft_sa=mod._VIX_SOFT_SA,
            vix_soft_ep=mod._VIX_SOFT_EP,
            watch_sa_min=mod._WATCH_SA_MIN,
            watch_conf_min=mod._WATCH_CONF_MIN,
            watch_ep_delta=mod._WATCH_EP_DELTA,
            forecast_gate_score_threshold=mod._FORECAST_GATE_SCORE_THRESHOLD,
            forecast_gate_sa=mod._FORECAST_GATE_SA,
            forecast_gate_ep=mod._FORECAST_GATE_EP,
            volume_confirm_score=mod._VOLUME_CONFIRM_SCORE,
            volume_confirm_penalty=mod._VOLUME_CONFIRM_PENALTY,
            volume_confirm_penalty_choppy=mod._VOLUME_CONFIRM_PENALTY_CHOPPY,
            entry_market_drift_max_pct=mod._ENTRY_MARKET_DRIFT_MAX_PCT,
            entry_market_drift_vix_bump=mod._ENTRY_MARKET_DRIFT_VIX_BUMP,
            entry_market_drift_prepost_bump=mod._ENTRY_MARKET_DRIFT_PREPOST_BUMP,
            entry_market_drift_cap_pct=mod._ENTRY_MARKET_DRIFT_CAP_PCT,
            entry_market_drift_vix_high_threshold=mod._ENTRY_MARKET_DRIFT_VIX_HIGH_THRESHOLD,
            entry_market_drift_vix_high_bump=mod._ENTRY_MARKET_DRIFT_VIX_HIGH_BUMP,
            market_hours_gates_enabled=mod._MARKET_HOURS_GATES_ENABLED,
        )

    def candidate_config(self) -> CandidateGateConfig:
        from gates.candidate import CandidateGateConfig

        return CandidateGateConfig(
            sa_family_min_score=self.sa_family_min_score,
            sa_include_macro_context=self.sa_include_macro_context,
            sa_macro_context_score=self.sa_macro_context_score,
            sa_forecast_confirm_bonus_enabled=self.sa_forecast_confirm_bonus_enabled,
            sa_forecast_bonus_threshold=self.sa_forecast_bonus_threshold,
            high_confidence_min=self.high_confidence_min,
            high_confidence_min_sa=self.high_confidence_min_sa,
            macro_veto_sa=self.macro_veto_sa,
            macro_veto_ep=self.macro_veto_ep,
            vix_soft_threshold=self.vix_soft_threshold,
            vix_soft_sa=self.vix_soft_sa,
            vix_soft_ep=self.vix_soft_ep,
            watch_sa_min=self.watch_sa_min,
            watch_conf_min=self.watch_conf_min,
            watch_ep_delta=self.watch_ep_delta,
            forecast_gate_score_threshold=self.forecast_gate_score_threshold,
            forecast_gate_sa=self.forecast_gate_sa,
            forecast_gate_ep=self.forecast_gate_ep,
            volume_confirm_score=self.volume_confirm_score,
            volume_confirm_penalty=self.volume_confirm_penalty,
            volume_confirm_penalty_choppy=self.volume_confirm_penalty_choppy,
            entry_market_drift_max_pct=self.entry_market_drift_max_pct,
            entry_market_drift_vix_bump=self.entry_market_drift_vix_bump,
            entry_market_drift_prepost_bump=self.entry_market_drift_prepost_bump,
            entry_market_drift_cap_pct=self.entry_market_drift_cap_pct,
            entry_market_drift_vix_high_threshold=self.entry_market_drift_vix_high_threshold,
            entry_market_drift_vix_high_bump=self.entry_market_drift_vix_high_bump,
            market_hours_gates_enabled=self.market_hours_gates_enabled,
        )
