"""Server-side validate-and-filter for LLM decision output.

Extracted from inline decision YAML code blocks for testability and
DRY across both 15m and 1h decision workflows.

Implements SSOT §4 (PlaybookAlert validation) and §10.2/10.3 gates:
  - edge_probability, sources_agree, confidence thresholds
  - EP ceiling by actual source count (prevents LLM inflation)
  - VIX hard gate (universal reject when VIX > 30)
  - Source-hallucination detection (delta ≥ 2 → hard reject)
  - R:R minimum (2:1 for actionable alerts)
  - R:R zero-risk rejection (stop == level → hard reject)
  - VIX + risk-off soft gate (VIX > 25 suppresses weak longs/shorts)
  - 1h macro veto (strong macro_risk_off score discounts longs)
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from constants import MACRO_STALE_SECONDS as _MACRO_STALE_SECONDS
from models import PlaybookAlert
from redis_client import get_redis

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")
_SESSION_STATS_TTL_SECONDS = int(os.environ.get("SESSION_STATS_TTL_SECONDS", "604800"))


# ── Gate-rejection enum ──────────────────────────────────────────
class GateRejection(str, Enum):
    """Structured gate rejection reasons for telemetry."""

    VIX_HARD = "vix_hard"
    SOURCE_HALLUCINATION = "source_hallucination"
    EP_THRESHOLD = "ep_threshold"
    SA_THRESHOLD = "sa_threshold"
    CONF_THRESHOLD = "conf_threshold"
    RR_MINIMUM = "rr_minimum"
    RR_ZERO_RISK = "rr_zero_risk"
    ENTRY_ORDER_INVALID = "entry_order_invalid"
    MACRO_VETO = "macro_veto"
    VIX_SOFT = "vix_soft"
    FORECAST_CONTRADICTS = "forecast_contradicts"
    TIMEFRAME_INVALID = "timeframe_invalid"
    ENTRY_MARKET_DRIFT = "entry_market_drift"
    VOLUME_UNCONFIRMED = "volume_unconfirmed"
    WATCH_EP_THRESHOLD = "watch_ep_threshold"
    WATCH_SA_THRESHOLD = "watch_sa_threshold"
    WATCH_CONF_THRESHOLD = "watch_conf_threshold"
    WATCH_CAP = "watch_cap"
    WATCH_DROPPED_DIRECTIONAL_PRESENT = "watch_dropped_directional_present"
    WATCH_DECAY = "watch_decay"


# Valid alert timeframes
_VALID_TIMEFRAMES = {"5m", "15m", "1h", "4h", "1D"}


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

# Per-timeframe gate thresholds (SSOT §10.2 / §10.3)
# Configurable via env vars: GATE_EP_15M, GATE_EP_1H, GATE_SA, GATE_CONF
_GATE_EP: dict[str, float] = {
    "15m": float(os.environ.get("GATE_EP_15M", "0.70")),
    "1h": float(os.environ.get("GATE_EP_1H", "0.75")),
}
_GATE_SA: int = int(os.environ.get("GATE_SA", "3"))
_GATE_CONF: float = float(os.environ.get("GATE_CONF", "0.75"))

# Limited WATCH policy (borderline-only, conservative)
_WATCH_MAX_PER_RUN: int = int(os.environ.get("WATCH_MAX_PER_RUN", "1"))
_WATCH_SA_MIN: int = int(os.environ.get("WATCH_SA_MIN", "2"))
_WATCH_CONF_MIN: float = float(os.environ.get("WATCH_CONF_MIN", "0.60"))
_WATCH_EP_DELTA: float = float(os.environ.get("WATCH_EP_DELTA", "0.05"))
# WATCH decay: drop WATCH alerts that persist unresolved across N pipeline cycles
_WATCH_DECAY_CYCLES: int = int(os.environ.get("WATCH_DECAY_CYCLES", "4"))
_WATCH_DECAY_TTL_SECONDS: int = int(os.environ.get("WATCH_DECAY_TTL_SECONDS", str(60 * 60 * 24)))

# Macro veto bypass thresholds (configurable)
_MACRO_VETO_SA: int = int(os.environ.get("MACRO_VETO_SA", "6"))
_MACRO_VETO_EP: float = float(os.environ.get("MACRO_VETO_EP", "0.90"))

# VIX soft-gate bypass thresholds (configurable)
_VIX_SOFT_THRESHOLD: float = float(os.environ.get("VIX_SOFT_THRESHOLD", "25.0"))
_VIX_SOFT_SA: int = int(os.environ.get("VIX_SOFT_SA", "3"))
_VIX_SOFT_EP: float = float(os.environ.get("VIX_SOFT_EP", "0.72"))

# Forecast contradiction gate thresholds (configurable)
_FORECAST_GATE_SCORE_THRESHOLD: float = float(os.environ.get("FORECAST_GATE_SCORE_THRESHOLD", "0.8"))
_FORECAST_GATE_SA: int = int(os.environ.get("FORECAST_GATE_SA", "5"))
_FORECAST_GATE_EP: float = float(os.environ.get("FORECAST_GATE_EP", "0.85"))

# Volume confirmation: minimum volume_spike score required for LONG/SHORT.
# Alerts without volume confirmation get confidence downgraded by this amount.
_VOLUME_CONFIRM_SCORE: float = float(os.environ.get("VOLUME_CONFIRM_SCORE", "1.5"))
_VOLUME_CONFIRM_PENALTY: float = float(os.environ.get("VOLUME_CONFIRM_PENALTY", "0.10"))

# Reject alerts where entry is too far from latest reference price (e.g., stale/unrealistic fills)
_ENTRY_MARKET_DRIFT_MAX_PCT: float = float(os.environ.get("ENTRY_MARKET_DRIFT_MAX_PCT", "0.08"))

# Dynamic gate controls (regime + timeframe overlays)
_DYNAMIC_GATES_ENABLED: bool = os.environ.get("DYNAMIC_GATES_ENABLED", "1") == "1"
_REGIME_CHOPPY_EP_BUMP: float = float(os.environ.get("REGIME_CHOPPY_EP_BUMP", "0.03"))
_REGIME_CHOPPY_CONF_BUMP: float = float(os.environ.get("REGIME_CHOPPY_CONF_BUMP", "0.03"))
_REGIME_CHOPPY_SA_BUMP: int = int(os.environ.get("REGIME_CHOPPY_SA_BUMP", "1"))
_REGIME_TRENDING_EP_REDUCE: float = float(os.environ.get("REGIME_TRENDING_EP_REDUCE", "0.01"))
_REGIME_TRENDING_CONF_REDUCE: float = float(os.environ.get("REGIME_TRENDING_CONF_REDUCE", "0.01"))
_TF_EP_OFFSET_15M: float = float(os.environ.get("TF_EP_OFFSET_15M", "0.00"))
_TF_EP_OFFSET_1H: float = float(os.environ.get("TF_EP_OFFSET_1H", "0.00"))
_TF_CONF_OFFSET_15M: float = float(os.environ.get("TF_CONF_OFFSET_15M", "0.00"))
_TF_CONF_OFFSET_1H: float = float(os.environ.get("TF_CONF_OFFSET_1H", "0.00"))


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
            if st in ("technical_trend", "sentiment_bull", "options_flow", "relative_strength") and sc > 0:
                bulls += 1
                strengths.append(min(abs(sc) / 3.0, 1.0))
            elif st in ("sentiment_bear", "macro_risk_off") or (
                st in ("technical_trend", "options_flow", "relative_strength") and sc < 0
            ):
                bears += 1
                strengths.append(min(abs(sc) / 3.0, 1.0))
    trend_strength = sum(strengths) / len(strengths) if strengths else 0.0
    return bulls, bears, trend_strength


def _classify_regime(vix: float, risk_off: bool, bulls: int, bears: int, trend_strength: float) -> str:
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


def _dynamic_gates(base_ep: float, base_sa: int, base_conf: float, timeframe: str, regime: str) -> tuple[float, int, float]:
    ep = base_ep
    sa = base_sa
    conf = base_conf
    if not _DYNAMIC_GATES_ENABLED:
        return ep, sa, conf
    if regime == "choppy":
        ep += _REGIME_CHOPPY_EP_BUMP
        sa += _REGIME_CHOPPY_SA_BUMP
        conf += _REGIME_CHOPPY_CONF_BUMP
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


# ── WATCH cycle-decay helpers ────────────────────────────────────

def _watch_decay_key(symbol: str, timeframe: str) -> str:
    return f"watch:decay:{timeframe}:{symbol}"


def _get_watch_cycles(symbol: str, timeframe: str) -> int:
    """Return the number of consecutive pipeline cycles a WATCH has persisted."""
    try:
        val = get_redis().hget(_watch_decay_key(symbol, timeframe), "cycles")
        return int(val) if val else 0
    except Exception:  # noqa: BLE001
        return 0


def _incr_watch_cycles(symbol: str, timeframe: str, ep: float, conf: float) -> int:
    """Increment the watch cycle counter. Returns the new cycle count."""
    try:
        r = get_redis()
        key = _watch_decay_key(symbol, timeframe)
        pipe = r.pipeline()
        pipe.hincrby(key, "cycles", 1)
        pipe.hset(key, mapping={"last_ep": str(ep), "last_conf": str(conf)})
        pipe.expire(key, _WATCH_DECAY_TTL_SECONDS)
        results = pipe.execute()
        return int(results[0])
    except Exception:  # noqa: BLE001
        return 0


def _reset_watch_cycles(symbols: list[str], timeframe: str) -> None:
    """Delete watch-cycle state for symbols that graduated to a directional alert."""
    try:
        r = get_redis()
        for sym in symbols:
            r.delete(_watch_decay_key(sym, timeframe))
    except Exception:  # noqa: BLE001
        pass


def _session_stats_key(timeframe: str, now: datetime | None = None) -> str:
    now_utc = now or datetime.now(timezone.utc)
    session_date = now_utc.astimezone(_ET).date().isoformat()
    return f"session:stats:{session_date}:{timeframe}"


def _record_session_gate_metrics(
    timeframe: str,
    llm_candidates: int,
    alerts_passed: int,
    watch_kept: int,
    rejections: list[tuple[str, GateRejection]],
) -> None:
    try:
        redis_client = get_redis()
        key = _session_stats_key(timeframe)
        pipe = redis_client.pipeline()
        pipe.hincrby(key, "decision_runs", 1)
        pipe.hincrby(key, "llm_candidates", llm_candidates)
        pipe.hincrby(key, "alerts_passed", alerts_passed)
        pipe.hincrby(key, "watch_kept", watch_kept)
        pipe.hincrby(key, "alerts_rejected", len(rejections))
        for _symbol, gate in rejections:
            pipe.hincrby(key, f"gate_{gate.value}", 1)
        pipe.expire(key, _SESSION_STATS_TTL_SECONDS)
        pipe.execute()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to record session gate metrics: %s", exc)


def _parse_snapshots(snapshots_json: str) -> list[dict[str, Any]]:
    """Parse snapshot JSON once for reuse by downstream helpers.

    Args:
        snapshots_json: JSON string of Snapshot dicts.

    Returns:
        Parsed list of snapshot dicts, or empty list on error.
    """
    try:
        parsed = json.loads(snapshots_json)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _build_snap_type_index(snaps: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Build per-symbol signal-type index from parsed snapshots.

    Args:
        snaps: List of parsed Snapshot dicts.

    Returns:
        Mapping of symbol → set of distinct signal type strings.
    """
    snap_types: dict[str, set[str]] = {}
    for s in snaps:
        sym = s.get("symbol", "")
        for sig in s.get("signals", []):
            snap_types.setdefault(sym, set()).add(sig.get("type", ""))
    return snap_types


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
    now = datetime.now(timezone.utc)
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

    Preference order:
      1) timesfm price_forecast raw.current_price
      2) any signal raw current_price/price/close/last/last_price
    """
    prices: dict[str, float] = {}
    fallback: dict[str, float] = {}
    for s in snaps:
        sym = s.get("symbol", "")
        if not sym:
            continue
        for sig in s.get("signals", []):
            raw = sig.get("raw") or {}
            if not isinstance(raw, dict):
                continue

            if sig.get("type") == "price_forecast":
                try:
                    cp = float(raw.get("current_price", 0.0))
                    if cp > 0:
                        prices[sym] = cp
                        continue
                except (TypeError, ValueError):
                    pass

            for k in ("current_price", "price", "close", "last", "last_price"):
                try:
                    v = float(raw.get(k, 0.0))
                    if v > 0:
                        fallback[sym] = v
                        break
                except (TypeError, ValueError):
                    continue

    for sym, px in fallback.items():
        prices.setdefault(sym, px)
    return prices


def validate_and_filter(
    llm_response: Any,
    snapshots_json: str,
    macro: dict[str, Any],
    vix: float,
    timeframe: str,
    *,
    add_score_fn: Any | None = None,
    trace_id: str | None = None,
) -> tuple[list[PlaybookAlert], str]:
    """Validate LLM output and apply server-side gate filters.

    Parses the LLM JSON response, validates each item as a
    PlaybookAlert, and applies a cascade of server-side gates:

    1. **VIX hard gate** (universal): VIX > 30 rejects LONG/SHORT
    2. **Source-hallucination check**: delta ≥ 2 → hard reject
    3. **EP ceiling**: caps edge_probability by actual source count
    4. **Gate thresholds**: EP, sources_agree, confidence minimums
    5. **R:R gate**: reward ≥ 2× risk for LONG/SHORT
    6. **VIX + risk-off soft gate**: VIX > 25 suppresses weak longs
    7. **Macro veto** (1h): strong macro_risk_off discounts longs

    Args:
        llm_response: Raw JSON string from the LLM decision engine.
        snapshots_json: JSON string of merged Snapshot dicts.
        macro: Macro regime dict (must contain ``risk_on`` key).
        vix: Current VIX level (0.0 if unavailable).
        timeframe: Pipeline timeframe (``"15m"`` or ``"1h"``).
        add_score_fn: Optional Langfuse ``add_score`` callback.
        trace_id: Optional Langfuse trace ID for scoring.

    Returns:
        Tuple of (list of passing PlaybookAlerts, JSON string of alerts).
    """
    # ── Parse LLM JSON ───────────────────────────────────────────
    # Some providers return fenced markdown (```json ... ```) or wrap
    # JSON in explanatory text. Extract the array payload defensively.
    def _extract_json_array_text(payload: Any) -> str:
        if isinstance(payload, str):
            text = payload.strip()
        elif isinstance(payload, list):
            return json.dumps(payload)
        elif isinstance(payload, dict):
            content = payload.get("content")
            if isinstance(content, str):
                text = content.strip()
            else:
                return json.dumps(payload)
        else:
            text = str(payload or "").strip()

        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        if not text.startswith("["):
            start = text.find("[")
            end = text.rfind("]")
            if start != -1 and end > start:
                text = text[start : end + 1]

        return text

    try:
        llm_json_text = _extract_json_array_text(llm_response)
        raw = json.loads(llm_json_text)
        if not isinstance(raw, list):
            raise ValueError(f"Expected list, got {type(raw).__name__}")
    except Exception as e:
        logger.error("Decision engine JSON parse error: %s", e)
        logger.error("Raw response: %s", str(llm_response)[:500])
        if add_score_fn and trace_id:
            add_score_fn(trace_id, "llm_json_valid", 0.0, comment="JSON parse failed")
        return [], "[]"

    if add_score_fn and trace_id:
        add_score_fn(trace_id, "llm_json_valid", 1.0, comment="valid JSON array")

    # ── Parse snapshots once ──────────────────────────────────────
    parsed_snaps = _parse_snapshots(snapshots_json)

    # ── Build snapshot signal-type index ─────────────────────────
    snap_types = _build_snap_type_index(parsed_snaps)

    # ── Build per-symbol forecast score index ────────────────────
    forecast_scores = _get_forecast_scores(parsed_snaps)
    # ── Build per-symbol volume spike index ───────────────────
    volume_scores = _get_volume_spike_scores(parsed_snaps)
    # ── Build per-symbol reference price index ────────────────
    ref_prices = _get_reference_prices(parsed_snaps)
    # 1h-specific: pre-compute macro_risk_off score for macro veto
    macro_risk_off_score = _get_macro_risk_off_score(parsed_snaps) if timeframe == "1h" else 0.0

    # Macro staleness guard: discard macro signals if data is too old
    macro_stale = _is_macro_stale(parsed_snaps)
    if macro_stale:
        macro_risk_off_score = 0.0

    risk_off = not macro.get("risk_on", True)
    base_ep_gate = _GATE_EP.get(timeframe, 0.70)
    base_sa_gate = _GATE_SA
    base_conf_gate = _GATE_CONF

    # ── VIX safety: treat NaN/Inf as conservative high-VIX ──────
    if not math.isfinite(vix):
        logger.warning("VIX value non-finite (%.4f), treating as 35.0", vix)
        vix = 35.0

    bulls, bears, trend_strength = _signal_surface(parsed_snaps)
    regime = _classify_regime(vix, risk_off, bulls, bears, trend_strength)
    ep_gate, sa_gate, conf_gate = _dynamic_gates(
        base_ep_gate,
        base_sa_gate,
        base_conf_gate,
        timeframe,
        regime,
    )

    alerts: list[PlaybookAlert] = []
    candidates: list[PlaybookAlert] = []
    rejections: list[tuple[str, GateRejection]] = []

    for item in raw:
        try:
            alert = PlaybookAlert(**item)
        except Exception as e:
            logger.warning("PlaybookAlert validation failed for %s: %s", item, e)
            continue

        # ── Timeframe validation gate ─────────────────────────────
        alert_tf = getattr(alert, "timeframe", timeframe)
        if alert_tf not in _VALID_TIMEFRAMES:
            logger.warning(
                "Timeframe invalid: %s timeframe=%s not in %s",
                alert.symbol,
                alert_tf,
                _VALID_TIMEFRAMES,
            )
            rejections.append((alert.symbol, GateRejection.TIMEFRAME_INVALID))
            continue

        candidates.append(alert)

        # ── Entry-order validation (Gate 0) ──────────────────────
        # LONG must have stop < level < target.
        # SHORT must have target < level < stop.
        # WATCH alerts are exempt.
        # All prices must be strictly positive for equities.
        if alert.direction in ("LONG", "SHORT"):
            _price_invalid = False
            for _pk in ("stop", "level", "target"):
                if alert.entry[_pk] <= 0:
                    logger.warning(
                        "Invalid price: %s %s %s=%.4f (must be > 0)",
                        alert.symbol,
                        alert.direction,
                        _pk,
                        alert.entry[_pk],
                    )
                    _price_invalid = True
                    break
            if _price_invalid:
                rejections.append((alert.symbol, GateRejection.ENTRY_ORDER_INVALID))
                continue

        if alert.direction == "LONG":
            if not (alert.entry["stop"] < alert.entry["level"] < alert.entry["target"]):
                logger.warning(
                    "Entry order invalid: %s LONG stop=%.2f level=%.2f target=%.2f",
                    alert.symbol,
                    alert.entry["stop"],
                    alert.entry["level"],
                    alert.entry["target"],
                )
                rejections.append((alert.symbol, GateRejection.ENTRY_ORDER_INVALID))
                continue

        # ── Entry-vs-market drift gate ─────────────────────────
        # Reject LONG/SHORT alerts whose proposed entry is too far
        # from latest reference price from snapshots.
        if alert.direction in ("LONG", "SHORT"):
            ref_price = ref_prices.get(alert.symbol)
            if ref_price and ref_price > 0:
                drift_pct = abs(alert.entry["level"] - ref_price) / ref_price
                if drift_pct > _ENTRY_MARKET_DRIFT_MAX_PCT:
                    logger.info(
                        "Entry drift filtered: %s %s entry=%.2f ref=%.2f drift=%.1f%% > max=%.1f%%",
                        alert.symbol,
                        alert.direction,
                        alert.entry["level"],
                        ref_price,
                        drift_pct * 100.0,
                        _ENTRY_MARKET_DRIFT_MAX_PCT * 100.0,
                    )
                    rejections.append((alert.symbol, GateRejection.ENTRY_MARKET_DRIFT))
                    continue
        elif alert.direction == "SHORT":
            if not (alert.entry["target"] < alert.entry["level"] < alert.entry["stop"]):
                logger.warning(
                    "Entry order invalid: %s SHORT target=%.2f level=%.2f stop=%.2f",
                    alert.symbol,
                    alert.entry["target"],
                    alert.entry["level"],
                    alert.entry["stop"],
                )
                rejections.append((alert.symbol, GateRejection.ENTRY_ORDER_INVALID))
                continue

        # ── VIX universal hard gate ──────────────────────────────
        # When VIX > 30 the market is in extreme stress.  Reject ALL
        # directional signals (LONG/SHORT) regardless of risk_on flag
        # or timeframe.  WATCH alerts are allowed through.
        if vix > 30.0 and alert.direction in ("LONG", "SHORT"):
            logger.warning(
                "VIX hard gate: %s %s rejected (VIX=%.1f > 30.0)",
                alert.symbol,
                alert.direction,
                vix,
            )
            rejections.append((alert.symbol, GateRejection.VIX_HARD))
            continue

        # ── Source-hallucination check (SSOT §10.2) ──────────────
        # delta = LLM claimed sources - actual distinct source types
        #   delta ≥ 2  → hard reject (SOURCE_HALLUCINATION)
        #   delta == 1 → downgrade to actual count (existing behaviour)
        #   delta ≤ 0  → no change
        actual_sources = len(snap_types.get(alert.symbol, set()))
        if actual_sources == 0:
            # Symbol not in any snapshot — LLM hallucinated the ticker
            logger.warning(
                "SYMBOL_HALLUCINATION: %s not present in merged snapshots",
                alert.symbol,
            )
            if add_score_fn and trace_id:
                add_score_fn(
                    trace_id,
                    "symbol_hallucination",
                    1.0,
                    comment=f"{alert.symbol}: not in snapshot data",
                )
            rejections.append((alert.symbol, GateRejection.SOURCE_HALLUCINATION))
            continue
        # actual_sources > 0 guaranteed past this point
        sa_delta = alert.sources_agree - actual_sources
        if sa_delta >= 2:
            logger.warning(
                "SOURCE_HALLUCINATION: %s LLM=%d actual=%d (delta=%d)",
                alert.symbol,
                alert.sources_agree,
                actual_sources,
                sa_delta,
            )
            if add_score_fn and trace_id:
                add_score_fn(
                    trace_id,
                    "sources_agree_hallucination",
                    float(sa_delta),
                    comment=f"{alert.symbol}: claimed {alert.sources_agree}, actual {actual_sources}",
                )
            rejections.append((alert.symbol, GateRejection.SOURCE_HALLUCINATION))
            continue
        elif sa_delta > 0:
            logger.warning(
                "sources_agree override: %s LLM=%d actual=%d",
                alert.symbol,
                alert.sources_agree,
                actual_sources,
            )
            if add_score_fn and trace_id:
                add_score_fn(
                    trace_id,
                    "sources_agree_override",
                    float(sa_delta),
                    comment=f"{alert.symbol}: claimed {alert.sources_agree}, actual {actual_sources}",
                )
            alert.sources_agree = actual_sources

        # ── EP ceiling by actual source count ────────────────────
        # Caps edge_probability to a ceiling determined by how many
        # distinct signal types actually appear in the snapshot.
        # Prevents the LLM from assigning inflated confidence with
        # thin supporting evidence.
        # When actual_sources == 0, cap aggressively at 0.50 — no
        # evidence should not yield a high-confidence alert.
        if actual_sources == 0:
            ceiling = 0.50
        else:
            ceiling = EP_CEILING.get(min(actual_sources, 11), 0.99)
        if alert.edge_probability > ceiling:
            original_ep = alert.edge_probability
            alert.edge_probability = ceiling
            logger.warning(
                "EP_CAPPED_%d_SOURCES: %s EP=%.2f capped to %.2f (sources=%d)",
                actual_sources,
                alert.symbol,
                original_ep,
                ceiling,
                actual_sources,
            )

        # ── Gate thresholds (per-timeframe) ──────────────────────
        if alert.direction == "WATCH":
            watch_ep_gate = max(ep_gate - _WATCH_EP_DELTA, 0.50)
            if alert.edge_probability < watch_ep_gate:
                logger.info(
                    "WATCH filtered (EP): %s ep=%.2f < watch_gate=%.2f",
                    alert.symbol,
                    alert.edge_probability,
                    watch_ep_gate,
                )
                rejections.append((alert.symbol, GateRejection.WATCH_EP_THRESHOLD))
                continue
            if alert.sources_agree < _WATCH_SA_MIN:
                logger.info(
                    "WATCH filtered (SA): %s sa=%d < watch_gate=%d",
                    alert.symbol,
                    alert.sources_agree,
                    _WATCH_SA_MIN,
                )
                rejections.append((alert.symbol, GateRejection.WATCH_SA_THRESHOLD))
                continue
            if alert.confidence < _WATCH_CONF_MIN:
                logger.info(
                    "WATCH filtered (CONF): %s conf=%.2f < watch_gate=%.2f",
                    alert.symbol,
                    alert.confidence,
                    _WATCH_CONF_MIN,
                )
                rejections.append((alert.symbol, GateRejection.WATCH_CONF_THRESHOLD))
                continue
        else:
            if alert.edge_probability < ep_gate:
                logger.info(
                    "Alert filtered (EP): %s ep=%.2f < gate=%.2f",
                    alert.symbol,
                    alert.edge_probability,
                    ep_gate,
                )
                rejections.append((alert.symbol, GateRejection.EP_THRESHOLD))
                continue
            if alert.sources_agree < sa_gate:
                logger.info(
                    "Alert filtered (SA): %s sa=%d < gate=%d",
                    alert.symbol,
                    alert.sources_agree,
                    sa_gate,
                )
                rejections.append((alert.symbol, GateRejection.SA_THRESHOLD))
                continue
            if alert.confidence < conf_gate:
                logger.info(
                    "Alert filtered (CONF): %s conf=%.2f < gate=%.2f",
                    alert.symbol,
                    alert.confidence,
                    conf_gate,
                )
                rejections.append((alert.symbol, GateRejection.CONF_THRESHOLD))
                continue

        # ── R:R gate: reward must be ≥ 2× risk ──────────────────
        risk = abs(alert.entry["level"] - alert.entry["stop"])
        reward = abs(alert.entry["target"] - alert.entry["level"])
        if alert.direction != "WATCH":
            if risk == 0:
                logger.warning(
                    "R:R zero-risk rejected: %s stop==level (%.2f)",
                    alert.symbol,
                    alert.entry["level"],
                )
                rejections.append((alert.symbol, GateRejection.RR_ZERO_RISK))
                continue
            # Reject unrealistic risk: stop so close to entry that R:R
            # is meaningless.  Use price-normalized floor to handle
            # penny stocks ($0.05 min) and higher-priced equities.
            micro_risk_floor = max(alert.entry["level"] * 0.001, 0.05)
            if risk < micro_risk_floor:
                logger.warning(
                    "R:R micro-risk rejected: %s risk=%.4f < floor=%.4f (level=%.2f)",
                    alert.symbol,
                    risk,
                    micro_risk_floor,
                    alert.entry["level"],
                )
                rejections.append((alert.symbol, GateRejection.RR_ZERO_RISK))
                continue
            if reward / risk < 2.0:
                logger.info(
                    "Alert filtered: %s R:R %.1f:1 below 2:1 minimum",
                    alert.symbol,
                    reward / risk,
                )
                rejections.append((alert.symbol, GateRejection.RR_MINIMUM))
                continue

        # ── Forecast contradiction gate ──────────────────────────
        # If a price_forecast signal exists for this symbol and
        # contradicts the alert direction, reject.
        # Non-blocking: if no forecast signal → pass through.
        # High-conviction bypass: SA >= 5 AND EP >= 0.85.
        if alert.direction in ("LONG", "SHORT"):
            _fc_score = forecast_scores.get(alert.symbol)
            if _fc_score is not None:
                _fc_contradicts = False
                if alert.direction == "LONG" and _fc_score < -_FORECAST_GATE_SCORE_THRESHOLD:
                    _fc_contradicts = True
                elif alert.direction == "SHORT" and _fc_score > _FORECAST_GATE_SCORE_THRESHOLD:
                    _fc_contradicts = True

                _fc_high_conviction = (
                    alert.sources_agree >= _FORECAST_GATE_SA and alert.edge_probability >= _FORECAST_GATE_EP
                )
                if _fc_contradicts and not _fc_high_conviction:
                    logger.info(
                        "Forecast contradiction: %s %s rejected "
                        "(forecast_score=%.2f, threshold=%.2f, sa=%d, ep=%.2f)",
                        alert.symbol,
                        alert.direction,
                        _fc_score,
                        _FORECAST_GATE_SCORE_THRESHOLD,
                        alert.sources_agree,
                        alert.edge_probability,
                    )
                    rejections.append((alert.symbol, GateRejection.FORECAST_CONTRADICTS))
                    continue

        # ── 1h macro veto: strong macro_risk_off discounts longs ─
        # After score harmonization, macro_risk_off scores are in [-1, +1].
        # 0.30 corresponds to the old 2.0 on [0, 3] scale.
        # High-conviction bypass: SA >= 6 AND EP >= 0.90 overrides macro veto.
        _macro_high_conviction = (
            alert.sources_agree >= _MACRO_VETO_SA and alert.edge_probability >= _MACRO_VETO_EP
        )
        if (
            timeframe == "1h"
            and alert.direction == "LONG"
            and macro_risk_off_score >= 0.30
            and not _macro_high_conviction
        ):
            logger.info(
                "Macro veto: %s LONG rejected (macro_risk_off score=%.2f)",
                alert.symbol,
                macro_risk_off_score,
            )
            rejections.append((alert.symbol, GateRejection.MACRO_VETO))
            continue

        # ── VIX + risk-off soft gate (both timeframes) ───────────
        # Suppress weak LONGs when VIX elevated + risk-off.
        # Suppress weak SHORTs when VIX elevated + risk-on  (short
        # squeezes are common in volatile risk-on rebounds).
        # High-conviction setups (SA >= 4 AND EP >= 0.80) pass even
        # in elevated-VIX environments.
        _high_conviction = alert.sources_agree >= _VIX_SOFT_SA and alert.edge_probability >= _VIX_SOFT_EP
        _vix_soft_triggered = False
        if vix > _VIX_SOFT_THRESHOLD and not _high_conviction:
            if risk_off and alert.direction == "LONG":
                _vix_soft_triggered = True
            elif not risk_off and alert.direction == "SHORT":
                _vix_soft_triggered = True
        if _vix_soft_triggered:
            # VIX soft gate: elevated volatility + directional mismatch.
            # These are borderline — log with REVIEW flag for manual inspection.
            logger.info(
                "VIX gate [REVIEW]: %s %s suppressed (VIX=%.1f, risk_off=%s, sa=%d, ep=%.2f)",
                alert.symbol,
                alert.direction,
                vix,
                risk_off,
                alert.sources_agree,
                alert.edge_probability,
            )
            rejections.append((alert.symbol, GateRejection.VIX_SOFT))
            continue

        # ── Volume confirmation gate ───────────────────────────────
        # Directional alerts without volume confirmation get a confidence
        # downgrade — thin-volume breakouts are unreliable.
        if alert.direction in ("LONG", "SHORT"):
            _vol_score = volume_scores.get(alert.symbol, 0.0)
            if _vol_score < _VOLUME_CONFIRM_SCORE:
                alert.confidence = max(alert.confidence - _VOLUME_CONFIRM_PENALTY, 0.0)
                # Re-check confidence gate after downgrade
                if alert.confidence < conf_gate:
                    logger.info(
                        "Volume unconfirmed: %s %s (vol_score=%.2f < %.2f) "
                        "conf downgraded to %.2f < gate %.2f",
                        alert.symbol,
                        alert.direction,
                        _vol_score,
                        _VOLUME_CONFIRM_SCORE,
                        alert.confidence,
                        conf_gate,
                    )
                    rejections.append((alert.symbol, GateRejection.VOLUME_UNCONFIRMED))
                    continue

        alerts.append(alert)

    # Keep WATCH output intentionally limited:
    # - If directional alerts exist, drop all WATCH alerts for this run.
    # - Otherwise rank WATCH candidates by composite score (ep × conf),
    #   apply stale-cycle decay filter, then cap to _WATCH_MAX_PER_RUN.
    directional_alerts = [a for a in alerts if a.direction in ("LONG", "SHORT")]
    watch_alerts = [a for a in alerts if a.direction == "WATCH"]
    if directional_alerts and watch_alerts:
        for w in watch_alerts:
            rejections.append((w.symbol, GateRejection.WATCH_DROPPED_DIRECTIONAL_PRESENT))
        watch_alerts = []
        alerts = directional_alerts
    else:
        # Sort by composite quality score so the best candidate stays
        watch_alerts.sort(key=lambda a: a.edge_probability * a.confidence, reverse=True)

        # ── Log full ranked WATCH queue for observability ──────────
        if watch_alerts:
            ranked_lines = " | ".join(
                f"#{i + 1} {a.symbol} ep={a.edge_probability:.2f} "
                f"conf={a.confidence:.2f} score={a.edge_probability * a.confidence:.3f}"
                for i, a in enumerate(watch_alerts)
            )
            logger.info(
                "Decision-%s WATCH ranked queue (%d): %s",
                timeframe,
                len(watch_alerts),
                ranked_lines,
            )

        # ── Stale-cycle decay filter ───────────────────────────────
        # Drop any WATCH that has been kept unresolved for >= N cycles
        # so stale setups do not crowd out fresh borderline candidates.
        decay_kept: list[PlaybookAlert] = []
        for w in watch_alerts:
            cycles = _get_watch_cycles(w.symbol, timeframe)
            if cycles >= _WATCH_DECAY_CYCLES:
                logger.info(
                    "WATCH_DECAY: %s stale across %d cycles "
                    "(ep=%.2f conf=%.2f) – dropping",
                    w.symbol,
                    cycles,
                    w.edge_probability,
                    w.confidence,
                )
                rejections.append((w.symbol, GateRejection.WATCH_DECAY))
            else:
                decay_kept.append(w)
        watch_alerts = decay_kept

        # ── Cap to _WATCH_MAX_PER_RUN ──────────────────────────────
        if len(watch_alerts) > _WATCH_MAX_PER_RUN:
            for w in watch_alerts[_WATCH_MAX_PER_RUN:]:
                rejections.append((w.symbol, GateRejection.WATCH_CAP))
            watch_alerts = watch_alerts[:_WATCH_MAX_PER_RUN]

        alerts = directional_alerts + watch_alerts

    # ── Update Redis WATCH-cycle state ────────────────────────────
    # Increment cycle count for each kept WATCH alert.
    for w in [a for a in alerts if a.direction == "WATCH"]:
        new_cycles = _incr_watch_cycles(w.symbol, timeframe, w.edge_probability, w.confidence)
        logger.debug("WATCH_CYCLE_INCR: %s cycles=%d", w.symbol, new_cycles)
    # Reset cycle state for symbols that graduated to a directional alert.
    directional_symbols = [a.symbol for a in alerts if a.direction in ("LONG", "SHORT")]
    if directional_symbols:
        _reset_watch_cycles(directional_symbols, timeframe)
        logger.debug("WATCH_CYCLE_RESET: %s", ", ".join(directional_symbols))

    pre_dist = _candidate_distribution(candidates)
    post_dist = _candidate_distribution(alerts)

    logger.info(
        "Decision-%s gate summary: llm_candidates=%d parsed_candidates=%d passed=%d rejected=%d "
        "regime=%s trend_strength=%.2f breadth=%d/%d "
        "ep_gate=%.2f(base=%.2f) sa_gate=%d(base=%d) conf_gate=%.2f(base=%.2f)",
        timeframe,
        len(raw),
        len(candidates),
        len(alerts),
        len(rejections),
        regime,
        trend_strength,
        bulls,
        bears,
        ep_gate,
        base_ep_gate,
        sa_gate,
        base_sa_gate,
        conf_gate,
        base_conf_gate,
    )
    logger.info(
        "Decision-%s candidate quality pre-gates: median_ep=%.2f median_conf=%.2f median_rr=%.2f median_sa=%.1f",
        timeframe,
        pre_dist["median_ep"],
        pre_dist["median_conf"],
        pre_dist["median_rr"],
        pre_dist["median_sa"],
    )
    logger.info(
        "Decision-%s candidate quality post-gates: median_ep=%.2f median_conf=%.2f median_rr=%.2f median_sa=%.1f",
        timeframe,
        post_dist["median_ep"],
        post_dist["median_conf"],
        post_dist["median_rr"],
        post_dist["median_sa"],
    )
    logger.info("Decision-%s: %d alerts passed gates", timeframe, len(alerts))

    # ── Log sample rejections per gate for debugging ──────────────
    gate_samples: dict[str, list[str]] = {}
    for sym, gate in rejections:
        gate_samples.setdefault(gate.value, []).append(sym)
    for gate_name, symbols in gate_samples.items():
        sample = symbols[:3]  # cap at 3 examples per gate
        logger.info(
            "Gate %s rejected %d alerts (sample: %s)",
            gate_name,
            len(symbols),
            ", ".join(sample),
        )
    if gate_samples:
        logger.info(
            "Decision-%s rejection counts: %s",
            timeframe,
            ", ".join(
                f"{gate_name}={len(symbols)}" for gate_name, symbols in sorted(gate_samples.items())
            ),
        )

    if len(alerts) == 0:
        if len(raw) == 0:
            no_alert_reason = "llm_zero_candidates"
        elif len(candidates) == 0:
            no_alert_reason = "all_candidates_invalid"
        elif gate_samples:
            top_gate = sorted(gate_samples.items(), key=lambda kv: (-len(kv[1]), kv[0]))[0]
            no_alert_reason = f"gate_filtered:{top_gate[0]}"
        else:
            no_alert_reason = "no_actionable_candidates"
        logger.info(
            "Decision-%s no-alert summary: reason=%s parsed_candidates=%d llm_candidates=%d",
            timeframe,
            no_alert_reason,
            len(candidates),
            len(raw),
        )

    watch_kept = sum(1 for a in alerts if a.direction == "WATCH")
    _record_session_gate_metrics(timeframe, len(raw), len(alerts), watch_kept, rejections)

    # ── Structured gate telemetry ─────────────────────────────────
    if add_score_fn and trace_id:
        total = max(len(raw), 1)
        pass_rate = len(alerts) / total
        rejection_rate = len(rejections) / total
        add_score_fn(
            trace_id,
            "alert_pass_rate",
            pass_rate,
            comment=f"{len(alerts)}/{len(raw)} passed gates",
        )
        add_score_fn(
            trace_id,
            "alerts_fired",
            float(len(alerts)),
            comment=f"{len(alerts)} alerts",
        )
        add_score_fn(
            trace_id,
            "candidate_median_ep_pre",
            pre_dist["median_ep"],
            comment="median edge_probability before gates",
        )
        add_score_fn(
            trace_id,
            "candidate_median_conf_pre",
            pre_dist["median_conf"],
            comment="median confidence before gates",
        )
        add_score_fn(
            trace_id,
            "candidate_median_rr_pre",
            pre_dist["median_rr"],
            comment="median R:R before gates",
        )
        add_score_fn(
            trace_id,
            "candidate_median_ep_post",
            post_dist["median_ep"],
            comment="median edge_probability after gates",
        )
        add_score_fn(
            trace_id,
            "candidate_median_conf_post",
            post_dist["median_conf"],
            comment="median confidence after gates",
        )
        add_score_fn(
            trace_id,
            "candidate_median_rr_post",
            post_dist["median_rr"],
            comment="median R:R after gates",
        )
        add_score_fn(
            trace_id,
            "gate_rejection_rate",
            rejection_rate,
            comment=f"{len(rejections)}/{len(raw)} rejected",
        )
        if rejection_rate > 0.9 and len(raw) >= 3:
            logger.warning(
                "Gate rejection rate %.0f%% (%d/%d) exceeds 90%% threshold — "
                "LLM output quality may have degraded",
                rejection_rate * 100,
                len(rejections),
                len(raw),
            )
        # Per-gate rejection counts for observability
        gate_counts: dict[str, int] = {}
        for _sym, gate in rejections:
            gate_counts[gate.value] = gate_counts.get(gate.value, 0) + 1
        for gate_name, count in gate_counts.items():
            add_score_fn(
                trace_id,
                f"gate_reject_{gate_name}",
                float(count),
                comment=f"{count} alerts rejected by {gate_name}",
            )

    alerts_json = json.dumps([a.model_dump() for a in alerts])
    return alerts, alerts_json
