"""Server-side validate-and-filter for LLM decision output.

Extracted from inline decision YAML code blocks for testability and
DRY across both 15m and 1h decision workflows.

Implements SSOT §4 (PlaybookAlert validation) and §10.2/10.3 gates:
  - edge_probability, sources_agree, confidence thresholds
  - EP ceiling by actual source count (prevents LLM inflation)
  - VIX hard gate (universal reject when VIX > 30)
  - Source-hallucination detection (delta ≥ 2 → hard reject)
  - R:R minimum (2:1 for actionable alerts)
  - 15m VIX + risk-off soft gate
  - 1h macro veto (strong macro_risk_off score vetoes longs)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from models import PlaybookAlert

logger = logging.getLogger(__name__)

# ── EP ceiling lookup table ──────────────────────────────────────
# Maps the number of *actual* distinct signal types in the snapshot
# to the maximum allowed edge_probability.  Prevents the LLM from
# assigning inflated EP values that aren't supported by the evidence.
EP_CEILING: dict[int, float] = {
    1: 0.55,
    2: 0.65,
    3: 0.75,
    4: 0.85,
    5: 0.90,
}

# Per-timeframe gate thresholds (SSOT §10.2 / §10.3)
_GATE_EP: dict[str, float] = {"15m": 0.70, "1h": 0.75}
_GATE_SA: int = 3
_GATE_CONF: float = 0.75


def _build_snap_type_index(snapshots_json: str) -> dict[str, set[str]]:
    """Build per-symbol signal-type index from snapshot JSON.

    Args:
        snapshots_json: JSON string of Snapshot dicts.

    Returns:
        Mapping of symbol → set of distinct signal type strings.
    """
    snap_types: dict[str, set[str]] = {}
    try:
        snaps = json.loads(snapshots_json)
        for s in snaps:
            sym = s.get("symbol", "")
            for sig in s.get("signals", []):
                snap_types.setdefault(sym, set()).add(sig.get("type", ""))
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass  # degrade gracefully — skip cross-check if parsing fails
    return snap_types


def _get_macro_risk_off_score(snapshots_json: str) -> float:
    """Extract max macro_risk_off score from snapshot signals.

    Used by the 1h macro veto gate.

    Args:
        snapshots_json: JSON string of Snapshot dicts.

    Returns:
        Maximum absolute macro_risk_off score, or 0.0 if none found.
    """
    score = 0.0
    try:
        snaps = json.loads(snapshots_json)
        for s in snaps:
            for sig in s.get("signals", []):
                if sig.get("type") == "macro_risk_off":
                    score = max(score, abs(float(sig.get("score", 0))))
    except (json.JSONDecodeError, TypeError, ValueError):
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
    6. **VIX + risk-off soft gate** (15m): elevated VIX suppresses weak longs
    7. **Macro veto** (1h): strong macro_risk_off vetoes all longs

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

    # ── Build snapshot signal-type index ─────────────────────────
    snap_types = _build_snap_type_index(snapshots_json)

    # 1h-specific: pre-compute macro_risk_off score for macro veto
    macro_risk_off_score = _get_macro_risk_off_score(snapshots_json) if timeframe == "1h" else 0.0

    risk_off = not macro.get("risk_on", True)
    ep_gate = _GATE_EP.get(timeframe, 0.70)

    alerts: list[PlaybookAlert] = []

    for item in raw:
        try:
            alert = PlaybookAlert(**item)
        except Exception as e:
            logger.warning("PlaybookAlert validation failed for %s: %s", item, e)
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
                continue
            elif sa_delta > 0:
                logger.warning(
                    "sources_agree override: %s LLM=%d actual=%d",
                    alert.symbol,
                    alert.sources_agree,
                    actual_sources,
                )
                alert.sources_agree = actual_sources

        # ── EP ceiling by actual source count ────────────────────
        # Caps edge_probability to a ceiling determined by how many
        # distinct signal types actually appear in the snapshot.
        # Prevents the LLM from assigning inflated confidence with
        # thin supporting evidence.
        ceiling = EP_CEILING.get(min(actual_sources, 5), 0.90) if actual_sources > 0 else 0.90
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
        if not (
            alert.edge_probability >= ep_gate
            and alert.sources_agree >= _GATE_SA
            and alert.confidence >= _GATE_CONF
        ):
            logger.info(
                "Alert filtered post-validation: %s ep=%.2f sa=%d conf=%.2f",
                alert.symbol,
                alert.edge_probability,
                alert.sources_agree,
                alert.confidence,
            )
            continue

        # ── R:R gate: reward must be ≥ 2× risk ──────────────────
        risk = abs(alert.entry["level"] - alert.entry["stop"])
        reward = abs(alert.entry["target"] - alert.entry["level"])
        if alert.direction != "WATCH" and risk > 0 and reward / risk < 2.0:
            logger.info(
                "Alert filtered: %s R:R %.1f:1 below 2:1 minimum",
                alert.symbol,
                reward / risk,
            )
            continue

        # ── 1h macro veto: strong macro_risk_off vetoes longs ────
        if timeframe == "1h" and alert.direction == "LONG" and macro_risk_off_score >= 2.0:
            logger.info(
                "Macro veto: %s LONG rejected (macro_risk_off score=%.1f)",
                alert.symbol,
                macro_risk_off_score,
            )
            continue

        # ── VIX + risk-off soft gate (both timeframes) ───────────
        if (
            vix > 20
            and risk_off
            and alert.direction == "LONG"
            and (alert.sources_agree < 4 or alert.edge_probability < 0.80)
        ):
            logger.info(
                "VIX gate: %s LONG suppressed (VIX=%.1f, risk-off, sa=%d, ep=%.2f)",
                alert.symbol,
                vix,
                alert.sources_agree,
                alert.edge_probability,
            )
            continue

        alerts.append(alert)

    logger.info("Decision-%s: %d alerts passed gates", timeframe, len(alerts))

    # ── Langfuse pass-rate scoring ───────────────────────────────
    if add_score_fn and trace_id:
        pass_rate = len(alerts) / max(len(raw), 1)
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

    alerts_json = json.dumps([a.model_dump() for a in alerts])
    return alerts, alerts_json
