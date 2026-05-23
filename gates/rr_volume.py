"""R:R, forecast, volume, and reference price helpers."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from constants import MACRO_STALE_SECONDS as _MACRO_STALE_SECONDS
from models import PlaybookAlert

logger = logging.getLogger(__name__)

# Forecast contradiction gate thresholds (configurable)
_FORECAST_GATE_SCORE_THRESHOLD: float = float(os.environ.get("FORECAST_GATE_SCORE_THRESHOLD", "0.8"))
_FORECAST_GATE_SA: int = int(os.environ.get("FORECAST_GATE_SA", "5"))
_FORECAST_GATE_EP: float = float(os.environ.get("FORECAST_GATE_EP", "0.85"))

# Volume confirmation: minimum volume_spike score required for LONG/SHORT.
# Alerts without volume confirmation get confidence downgraded by this amount.
# In choppy / risk_off_high_vix regimes the penalty is larger — thin-volume
# breakouts are significantly more unreliable in indecisive or stressed markets.
_VOLUME_CONFIRM_SCORE: float = float(os.environ.get("VOLUME_CONFIRM_SCORE", "1.5"))
_VOLUME_CONFIRM_PENALTY: float = float(os.environ.get("VOLUME_CONFIRM_PENALTY", "0.05"))
_VOLUME_CONFIRM_PENALTY_CHOPPY: float = float(os.environ.get("VOLUME_CONFIRM_PENALTY_CHOPPY", "0.10"))

# Reject alerts where entry is too far from latest reference price (e.g., stale/unrealistic fills)
_ENTRY_MARKET_DRIFT_MAX_PCT: float = float(os.environ.get("ENTRY_MARKET_DRIFT_MAX_PCT", "0.03"))
_ENTRY_MARKET_DRIFT_VIX_BUMP: float = float(os.environ.get("ENTRY_MARKET_DRIFT_VIX_BUMP", "0.01"))
_ENTRY_MARKET_DRIFT_PREPOST_BUMP: float = float(os.environ.get("ENTRY_MARKET_DRIFT_PREPOST_BUMP", "0.01"))
_ENTRY_MARKET_DRIFT_CAP_PCT: float = float(os.environ.get("ENTRY_MARKET_DRIFT_CAP_PCT", "0.08"))
# Second VIX tier: extreme-volatility regimes (VIX >= 30) add extra tolerance on top of soft-threshold bump
_ENTRY_MARKET_DRIFT_VIX_HIGH_THRESHOLD: float = float(
    os.environ.get("ENTRY_MARKET_DRIFT_VIX_HIGH_THRESHOLD", "30.0")
)
_ENTRY_MARKET_DRIFT_VIX_HIGH_BUMP: float = float(os.environ.get("ENTRY_MARKET_DRIFT_VIX_HIGH_BUMP", "0.02"))


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _rr(alert: PlaybookAlert) -> float:
    risk = abs(alert.entry["level"] - alert.entry["stop"])
    if risk <= 0:
        return 0.0
    reward = abs(alert.entry["target"] - alert.entry["level"])
    return reward / risk


def _candidate_distribution(alerts: list[PlaybookAlert]) -> dict[str, float]:
    return {
        "count": float(len(alerts)),
        "median_ep": _median([a.edge_probability for a in alerts]),
        "median_conf": _median([a.confidence for a in alerts]),
        "median_rr": _median([_rr(a) for a in alerts]),
        "median_sa": _median([float(a.sources_agree) for a in alerts]),
    }


def _get_forecast_scores(snaps: list[dict[str, Any]]) -> dict[str, float]:
    """Extract per-symbol price_forecast scores from parsed snapshots.

    Args:
        snaps: List of parsed Snapshot dicts.

    Returns:
        Mapping of symbol → highest absolute price_forecast score.
    """
    scores: dict[str, float] = {}
    for s in snaps:
        sym = s.get("symbol", "")
        for sig in s.get("signals", []):
            if sig.get("type") == "price_forecast":
                try:
                    val = float(sig.get("score", 0))
                    if sym not in scores or abs(val) > abs(scores[sym]):
                        scores[sym] = val
                except (TypeError, ValueError):
                    pass
    return scores


def _get_macro_risk_off_score(snaps: list[dict[str, Any]]) -> float:
    """Extract max macro_risk_off score from parsed snapshot signals.

    Used by the 1h macro veto gate.

    Args:
        snaps: List of parsed Snapshot dicts.

    Returns:
        Maximum absolute macro_risk_off score, or 0.0 if none found.
    """
    score = 0.0
    for s in snaps:
        for sig in s.get("signals", []):
            if sig.get("type") == "macro_risk_off":
                try:
                    score = max(score, abs(float(sig.get("score", 0))))
                except (TypeError, ValueError):
                    pass
    return score


def _is_macro_stale(snaps: list[dict[str, Any]]) -> bool:
    """Check if the most recent macro snapshot is older than the staleness threshold.

    Args:
        snaps: List of parsed Snapshot dicts.

    Returns:
        True if macro data is stale or absent.
    """
    now = datetime.now(UTC)
    newest_macro_ts: datetime | None = None
    for s in snaps:
        for sig in s.get("signals", []):
            if sig.get("type") == "macro_risk_off":
                ts_str = s.get("timestamp", "")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if newest_macro_ts is None or ts > newest_macro_ts:
                        newest_macro_ts = ts
                except (ValueError, TypeError):
                    continue
    if newest_macro_ts is None:
        return True
    age = (now - newest_macro_ts).total_seconds()
    if age > _MACRO_STALE_SECONDS:
        logger.warning("Macro data is stale (%.0fs old, threshold=%ds)", age, _MACRO_STALE_SECONDS)
        return True
    return False


def _get_volume_spike_scores(snaps: list[dict[str, Any]]) -> dict[str, float]:
    """Extract per-symbol max volume_spike score from parsed snapshots.

    Args:
        snaps: List of parsed Snapshot dicts.

    Returns:
        Mapping of symbol → highest volume_spike score.
    """
    scores: dict[str, float] = {}
    for s in snaps:
        sym = s.get("symbol", "")
        for sig in s.get("signals", []):
            if sig.get("type") == "volume_spike":
                try:
                    val = float(sig.get("score", 0))
                    if sym not in scores or val > scores[sym]:
                        scores[sym] = val
                except (TypeError, ValueError):
                    pass
    return scores


def _get_reference_prices(snaps: list[dict[str, Any]]) -> dict[str, float]:
    """Extract per-symbol latest reference prices from snapshot signal raw payloads.

    Priority order (0 = highest):
      0) technical_trend  — live TradingView quote (most reliable real-time price)
      1) volume_spike     — live Polygon trade print
      2) options_flow     — Polygon/Alpaca options last price
      3) insider_activity — EDGAR trade execution price
      4) catalyst_event   — ROT/SpamShield reference price
      5) short_interest   — short interest data price
      6) price_forecast   — TimesFM model input feature (stale training price, lowest priority)
    """
    prices: dict[str, float] = {}
    candidates: dict[str, tuple[int, int, float]] = {}
    type_priority = {
        "technical_trend": 0,
        "volume_spike": 1,
        "options_flow": 2,
        "insider_activity": 3,
        "catalyst_event": 4,
        "short_interest": 5,
        "price_forecast": 6,
    }
    key_priority = {
        "current_price": 0,
        "last": 1,
        "last_price": 2,
        "price": 3,
        "close": 4,
    }

    for s in snaps:
        sym = s.get("symbol", "")
        if not sym:
            continue
        for sig in s.get("signals", []):
            raw = sig.get("raw") or {}
            if not isinstance(raw, dict):
                continue

            sig_type = str(sig.get("type", ""))
            sig_rank = type_priority.get(sig_type, 99)
            for k in ("current_price", "last", "last_price", "price", "close"):
                try:
                    v = float(raw.get(k, 0.0))
                    if v > 0:
                        key_rank = key_priority.get(k, 99)
                        existing = candidates.get(sym)
                        proposal = (sig_rank, key_rank, v)
                        if existing is None or proposal < existing:
                            candidates[sym] = proposal
                        break
                except (TypeError, ValueError):
                    continue

    for sym, (_, _, px) in candidates.items():
        prices[sym] = px
    return prices
