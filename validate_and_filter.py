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
import os
from enum import Enum
from typing import Any

from models import PlaybookAlert

logger = logging.getLogger(__name__)


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

# Macro veto bypass thresholds (configurable)
_MACRO_VETO_SA: int = int(os.environ.get("MACRO_VETO_SA", "6"))
_MACRO_VETO_EP: float = float(os.environ.get("MACRO_VETO_EP", "0.90"))

# VIX soft-gate bypass thresholds (configurable)
_VIX_SOFT_THRESHOLD: float = float(os.environ.get("VIX_SOFT_THRESHOLD", "25.0"))
_VIX_SOFT_SA: int = int(os.environ.get("VIX_SOFT_SA", "4"))
_VIX_SOFT_EP: float = float(os.environ.get("VIX_SOFT_EP", "0.80"))


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


def validate_and_filter(
    llm_response: str,
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
    try:
        raw = json.loads(llm_response)
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

    # 1h-specific: pre-compute macro_risk_off score for macro veto
    macro_risk_off_score = _get_macro_risk_off_score(parsed_snaps) if timeframe == "1h" else 0.0

    risk_off = not macro.get("risk_on", True)
    ep_gate = _GATE_EP.get(timeframe, 0.70)

    alerts: list[PlaybookAlert] = []
    rejections: list[tuple[str, GateRejection]] = []

    for item in raw:
        try:
            alert = PlaybookAlert(**item)
        except Exception as e:
            logger.warning("PlaybookAlert validation failed for %s: %s", item, e)
            continue

        # ── Entry-order validation (Gate 0) ──────────────────────
        # LONG must have stop < level < target.
        # SHORT must have target < level < stop.
        # WATCH alerts are exempt.
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
        if actual_sources > 0:
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
            ceiling = EP_CEILING.get(min(actual_sources, 7), 0.95)
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
        if alert.edge_probability < ep_gate:
            logger.info(
                "Alert filtered (EP): %s ep=%.2f < gate=%.2f",
                alert.symbol,
                alert.edge_probability,
                ep_gate,
            )
            rejections.append((alert.symbol, GateRejection.EP_THRESHOLD))
            continue
        if alert.sources_agree < _GATE_SA:
            logger.info(
                "Alert filtered (SA): %s sa=%d < gate=%d",
                alert.symbol,
                alert.sources_agree,
                _GATE_SA,
            )
            rejections.append((alert.symbol, GateRejection.SA_THRESHOLD))
            continue
        if alert.confidence < _GATE_CONF:
            logger.info(
                "Alert filtered (CONF): %s conf=%.2f < gate=%.2f",
                alert.symbol,
                alert.confidence,
                _GATE_CONF,
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
            # is meaningless (< 0.1% of entry price).
            if risk < alert.entry["level"] * 0.001:
                logger.warning(
                    "R:R micro-risk rejected: %s risk=%.4f < 0.1%% of level=%.2f",
                    alert.symbol,
                    risk,
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

        alerts.append(alert)

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
