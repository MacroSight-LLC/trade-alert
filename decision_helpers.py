"""Shared helpers for decision-15m.yaml and decision-1h.yaml.

Extracts the common code blocks from both decision workflows to
eliminate the 92% DRY violation between them.  Each YAML step
calls a single helper function with just the timeframe parameter.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_MERGE_LIMIT_15M: int = int(os.environ.get("MERGE_LIMIT_15M", "30"))
_MERGE_LIMIT_1H: int = int(os.environ.get("MERGE_LIMIT_1H", "20"))
_PRUNE_ENABLED: bool = os.environ.get("PRUNE_ENABLED", "1") == "1"
_PRUNE_MIN_TYPES_15M: int = int(os.environ.get("PRUNE_MIN_TYPES_15M", "4"))
_PRUNE_MIN_TYPES_1H: int = int(os.environ.get("PRUNE_MIN_TYPES_1H", "3"))
_PRUNE_MIN_STRENGTH_15M: float = float(os.environ.get("PRUNE_MIN_STRENGTH_15M", "2.5"))
_PRUNE_MIN_STRENGTH_1H: float = float(os.environ.get("PRUNE_MIN_STRENGTH_1H", "2.5"))
_PRUNE_RESCUE_TOP_K: int = int(os.environ.get("PRUNE_RESCUE_TOP_K", "3"))


def _merge_limit_for_timeframe(timeframe: str) -> int:
    if timeframe == "15m":
        return _MERGE_LIMIT_15M
    if timeframe == "1h":
        return _MERGE_LIMIT_1H
    return 20


def _prune_thresholds(timeframe: str) -> tuple[int, float]:
    if timeframe == "1h":
        return _PRUNE_MIN_TYPES_1H, _PRUNE_MIN_STRENGTH_1H
    return _PRUNE_MIN_TYPES_15M, _PRUNE_MIN_STRENGTH_15M


def _snapshot_strength(snapshot: dict[str, Any]) -> tuple[int, float]:
    signals = snapshot.get("signals", [])
    if not isinstance(signals, list):
        return 0, 0.0

    types: set[str] = set()
    strength = 0.0
    for sig in signals:
        if not isinstance(sig, dict):
            continue
        sig_type = sig.get("type")
        if isinstance(sig_type, str) and sig_type:
            types.add(sig_type)
        try:
            score = abs(float(sig.get("score", 0.0)))
            conf = max(float(sig.get("confidence", 0.0)), 0.0)
        except (TypeError, ValueError):
            continue
        strength += score * conf

    return len(types), strength


def _prune_snapshots_for_llm(
    timeframe: str,
    snapshots: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Drop weak candidates before prompt construction to reduce LLM noise/cost."""
    if not _PRUNE_ENABLED:
        return snapshots, {
            "input": len(snapshots),
            "kept": len(snapshots),
            "dropped_low_types": 0,
            "dropped_low_strength": 0,
            "rescued": 0,
        }

    min_types, min_strength = _prune_thresholds(timeframe)

    kept: list[dict[str, Any]] = []
    dropped_low_types = 0
    dropped_low_strength = 0

    for snap in snapshots:
        type_count, strength = _snapshot_strength(snap)
        if type_count < min_types:
            dropped_low_types += 1
            continue
        if strength < min_strength:
            dropped_low_strength += 1
            continue
        kept.append(snap)

    stats = {
        "input": len(snapshots),
        "kept": len(kept),
        "dropped_low_types": dropped_low_types,
        "dropped_low_strength": dropped_low_strength,
        "rescued": 0,
    }
    return kept, stats


def _rescue_top_candidates(
    snapshots: list[dict[str, Any]],
    top_k: int,
) -> list[dict[str, Any]]:
    """Return top-k snapshots by deterministic strength score."""
    ranked: list[tuple[float, dict[str, Any]]] = []
    for snap in snapshots:
        _type_count, strength = _snapshot_strength(snap)
        ranked.append((strength, snap))
    ranked.sort(key=lambda row: row[0], reverse=True)
    return [snap for _score, snap in ranked[:top_k]]


def merge_snapshots(
    timeframe: str,
    inputs: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge snapshots from orchestrator inputs or Redis fallback.

    Args:
        timeframe: Pipeline timeframe (``"15m"`` or ``"1h"``).
        inputs: Workflow inputs dict (may contain pre-merged data).

    Returns:
        Dict with keys: ``skip``, ``snapshots_json``, ``macro``, ``n``.
    """
    _inp_json = inputs.get("merged_snapshots_json", "") if inputs else ""
    _inp_macro = inputs.get("merged_macro") if inputs else None
    _inp_n = inputs.get("merged_n") if inputs else None

    if _inp_json and _inp_n is not None:
        snapshots_json = _inp_json
        macro = _inp_macro or {}
        prune_stats = {
            "input": 0,
            "kept": 0,
            "dropped_low_types": 0,
            "dropped_low_strength": 0,
            "rescued": 0,
        }
        try:
            n = int(_inp_n)
        except (ValueError, TypeError):
            logger.warning(
                "Decision-%s: invalid merged_n value (%r) — skipping pre-merged path", timeframe, _inp_n
            )
            return {"skip": True, "snapshots_json": "[]", "macro": macro, "n": 0, "prune_stats": prune_stats}

        # Deterministic pre-LLM pruning even for orchestrator-provided merges.
        try:
            parsed = json.loads(_inp_json)
            if isinstance(parsed, list):
                pruned, stats = _prune_snapshots_for_llm(timeframe, parsed)
                prune_stats = dict(stats)
                if len(pruned) == 0 and parsed:
                    rescue_n = max(1, _PRUNE_RESCUE_TOP_K)
                    pruned = _rescue_top_candidates(parsed, rescue_n)
                    prune_stats["rescued"] = len(pruned)
                    logger.warning(
                        "Decision-%s pre-LLM prune rescued %d candidate(s) to avoid empty cycle",
                        timeframe,
                        len(pruned),
                    )
                snapshots_json = json.dumps(pruned, indent=2)
                n = len(pruned)
                logger.info(
                    "Decision-%s pre-LLM prune (orchestrator): input=%d kept=%d "
                    "drop_types=%d drop_strength=%d rescued=%d",
                    timeframe,
                    stats["input"],
                    len(pruned),
                    stats["dropped_low_types"],
                    stats["dropped_low_strength"],
                    prune_stats["rescued"],
                )
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("Decision-%s: unable to parse pre-merged snapshots for pruning", timeframe)

        logger.info("Decision-%s: using %d pre-merged symbols from orchestrator", timeframe, n)
        return {
            "skip": n == 0,
            "snapshots_json": snapshots_json,
            "macro": macro,
            "n": n,
            "prune_stats": prune_stats,
        }

    from merger import get_macro_regime, merge

    merge_limit = _merge_limit_for_timeframe(timeframe)
    snapshots = merge(timeframe, limit=merge_limit)
    macro = get_macro_regime()
    if len(snapshots) == 0:
        logger.info("No snapshots available for %s decision — skipping", timeframe)
        return {
            "skip": True,
            "snapshots_json": "[]",
            "macro": {},
            "n": 0,
            "prune_stats": {
                "input": 0,
                "kept": 0,
                "dropped_low_types": 0,
                "dropped_low_strength": 0,
                "rescued": 0,
            },
        }

    snapshot_dicts = [snap.model_dump() for snap in snapshots]
    pruned, stats = _prune_snapshots_for_llm(timeframe, snapshot_dicts)
    if len(pruned) == 0:
        rescue_n = max(1, _PRUNE_RESCUE_TOP_K)
        pruned = _rescue_top_candidates(snapshot_dicts, rescue_n)
        stats["rescued"] = len(pruned)
        logger.warning(
            "Decision-%s pre-LLM prune dropped all candidates; rescued top %d by strength",
            timeframe,
            len(pruned),
        )
    else:
        stats["rescued"] = 0

    snapshots_json = json.dumps(pruned, indent=2)
    logger.info(
        "Decision-%s: merged %d symbols for evaluation (limit=%d), kept %d after pre-LLM prune "
        "(drop_types=%d drop_strength=%d rescued=%d)",
        timeframe,
        len(snapshots),
        merge_limit,
        len(pruned),
        stats["dropped_low_types"],
        stats["dropped_low_strength"],
        stats["rescued"],
    )
    return {
        "skip": False,
        "snapshots_json": snapshots_json,
        "macro": macro,
        "n": len(pruned),
        "prune_stats": stats,
    }


def build_prompt(
    timeframe: str,
    merge_result: dict[str, Any],
    fred_results: list[dict[str, Any]],
) -> dict[str, str]:
    """Build the ensemble prompt for the decision LLM.

    Args:
        timeframe: Pipeline timeframe.
        merge_result: Output from :func:`merge_snapshots`.
        fred_results: FRED MCP results ``[{vix_level: ...}, {spread_bps: ...}]``.

    Returns:
        Dict with ``prompt`` and ``prompt_version`` keys.
    """
    from prompt_manager import (
        format_golden_examples,
        format_winrate_context,
        get_decision_prompts,
        get_prompt_version,
        get_quality_escalation_rules,
    )

    macro = merge_result["macro"]
    snapshots_json = merge_result["snapshots_json"]
    n = merge_result["n"]

    def _market_reference_context(snaps_json: str, limit: int = 20) -> str:
        """Extract symbol->reference price lines from snapshot raw payloads for prompt context."""
        try:
            snaps = json.loads(snaps_json)
        except (TypeError, ValueError):
            return ""

        refs: dict[str, float] = {}
        for snap in snaps:
            sym = str(snap.get("symbol", "")).upper()
            if not sym or sym in refs:
                continue
            for sig in snap.get("signals", []):
                raw = sig.get("raw") or {}
                if not isinstance(raw, dict):
                    continue
                for key in ("current_price", "last", "last_price", "price", "close"):
                    try:
                        px = float(raw.get(key, 0.0))
                    except (TypeError, ValueError):
                        continue
                    if px > 0:
                        refs[sym] = px
                        break
                if sym in refs:
                    break

        if not refs:
            return ""

        lines: list[str] = []
        for i, sym in enumerate(sorted(refs.keys())):
            if i >= limit:
                break
            lines.append(f"- {sym}: ${refs[sym]:,.2f}")
        return "\n".join(lines)

    _fred_vix = fred_results[0] if len(fred_results) > 0 else {}
    _fred_yc = fred_results[1] if len(fred_results) > 1 else {}
    vix = _fred_vix.get("vix_level") or _fred_vix.get("value", "N/A")
    yc = _fred_yc.get("spread_bps") or _fred_yc.get("value", "N/A")

    # Guard against zero/falsy FRED values that pass the "N/A" check but
    # are clearly stale — e.g. VIX=0.0 or curve=0.0 are physically impossible
    # in live markets.  Mark them stale so the LLM gets an honest signal.
    import logging as _logging

    _dh_log = _logging.getLogger(__name__)
    try:
        if vix != "N/A" and float(vix) == 0.0:
            _dh_log.warning("FRED staleness: VIX returned 0.0 — marking as STALE")
            vix = "STALE"
    except (TypeError, ValueError):
        pass
    try:
        if yc != "N/A" and float(yc) == 0.0:
            _dh_log.warning("FRED staleness: yield-curve spread returned 0.0 — marking as STALE")
            yc = "STALE"
    except (TypeError, ValueError):
        pass

    _fred_live = vix not in ("N/A", "STALE") and yc not in ("N/A", "STALE")
    data_freshness = "LIVE" if _fred_live else "CACHED (stale — FRED unavailable)"

    risk_on = macro.get("risk_on", True)
    macro_summary = f"{'Risk-on' if risk_on else 'Risk-off'}, VIX={vix}, Yield curve={yc}bps"

    perf_ctx = format_winrate_context()
    escalation = get_quality_escalation_rules(timeframe)
    if escalation:
        perf_ctx = perf_ctx + "\n" + escalation if perf_ctx else escalation

    system_prompt, user_prompt = get_decision_prompts(
        timeframe,
        {
            "macro_summary": macro_summary,
            "vix": vix,
            "yc": yc,
            "n": n,
            "snapshots_json": snapshots_json,
            "market_reference_context": _market_reference_context(snapshots_json),
            "data_freshness": data_freshness,
            "performance_context": perf_ctx,
            "few_shot_examples": format_golden_examples(),
        },
    )

    full_prompt = f"SYSTEM:\n{system_prompt}\n\nUSER:\n{user_prompt}"
    return {"prompt": full_prompt, "prompt_version": get_prompt_version()}


def log_ensemble_decision(
    trace_id: str | None,
    *,
    timeframe: str,
    snapshots_json: str,
    llm_response: Any,
    macro: dict[str, Any],
    vix: float,
    regime: str = "unknown",
    market_session: str = "unknown",
) -> None:
    """Log LLM decision I/O to Langfuse without exposing raw signal payloads."""
    if not trace_id:
        return
    try:
        from pipeline_tracing import add_score, tag_trace

        snaps = json.loads(snapshots_json) if snapshots_json else []
        type_counts: dict[str, int] = {}
        for snap in snaps:
            for sig in snap.get("signals", []):
                t = str(sig.get("type", "unknown"))
                type_counts[t] = type_counts.get(t, 0) + 1

        summary = f"symbols={len(snaps)} types={type_counts}"
        add_score(trace_id, "decision_input_summary", float(len(snaps)), comment=summary[:500])

        raw_out = str(llm_response or "")[:10240]
        add_score(trace_id, "decision_raw_response_len", float(len(raw_out)), comment=raw_out[:2000])

        tag_trace(
            trace_id,
            [
                f"timeframe:{timeframe}",
                f"vix:{vix:.1f}",
                f"regime:{regime}",
                f"session:{market_session}",
            ],
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Ensemble decision logging failed (non-blocking): %s", exc)


def validate_and_filter_step(
    timeframe: str,
    llm_response: Any,
    merge_result: dict[str, Any],
    fred_results: list[dict[str, Any]],
    trace_id: str | None,
) -> dict[str, Any]:
    """Validate LLM output, score quality, and capture to dataset.

    Args:
        timeframe: Pipeline timeframe.
        llm_response: Raw LLM output payload.
        merge_result: Output from :func:`merge_snapshots`.
        fred_results: FRED MCP results.
        trace_id: Langfuse trace ID (may be None).

    Returns:
        Dict with ``alerts_json``, ``count``, and optional ``quality``.
    """
    try:
        from pipeline_tracing import add_score, tag_trace
    except ImportError:
        add_score = tag_trace = None  # type: ignore[assignment]

    # Read VIX for server-side gates
    try:
        _vix = float(fred_results[0].get("vix_level") or fred_results[0].get("value", 0))
    except (ValueError, TypeError, IndexError, KeyError):
        _vix = 0.0
    _macro = merge_result.get("macro") or {}

    from validate_and_filter import validate_and_filter as _vf

    _regime = "unknown"
    _session = "unknown"
    try:
        from validate_and_filter import _classify_regime, _market_session_bucket, _signal_surface

        _risk_off = not _macro.get("risk_on", True)
        _snaps = json.loads(merge_result.get("snapshots_json", "[]"))
        _b, _be, _ts = _signal_surface(_snaps)
        _regime = _classify_regime(_vix, _risk_off, _b, _be, _ts)
        _session = _market_session_bucket()
    except Exception:  # noqa: BLE001
        pass

    log_ensemble_decision(
        trace_id,
        timeframe=timeframe,
        snapshots_json=merge_result.get("snapshots_json", "[]"),
        llm_response=llm_response,
        macro=_macro,
        vix=_vix,
        regime=_regime,
        market_session=_session,
    )

    if add_score is not None and trace_id:
        _ps = merge_result.get("prune_stats") or {}
        _in = float(_ps.get("input", 0))
        _kept = float(_ps.get("kept", 0))
        _d_types = float(_ps.get("dropped_low_types", 0))
        _d_strength = float(_ps.get("dropped_low_strength", 0))
        _rescued = float(_ps.get("rescued", 0))
        add_score(trace_id, "pre_llm_candidates_input", _in, comment=f"{timeframe} pre-LLM candidates")
        add_score(trace_id, "pre_llm_candidates_kept", _kept, comment=f"{timeframe} candidates kept")
        add_score(
            trace_id, "pre_llm_pruned_low_types", _d_types, comment="pruned for low signal-type diversity"
        )
        add_score(
            trace_id, "pre_llm_pruned_low_strength", _d_strength, comment="pruned for low weighted strength"
        )
        add_score(
            trace_id,
            "pre_llm_prune_rescued",
            _rescued,
            comment="rescued top candidates when prune emptied set",
        )
        if _in > 0:
            add_score(trace_id, "pre_llm_keep_rate", _kept / _in, comment="pre-LLM candidate keep ratio")

    alerts, alerts_json = _vf(
        llm_response=llm_response,
        snapshots_json=merge_result["snapshots_json"],
        macro=_macro,
        vix=_vix,
        timeframe=timeframe,
        add_score_fn=add_score,
        trace_id=trace_id,
    )
    result: dict[str, Any] = {"alerts_json": alerts_json, "count": len(alerts)}

    # Deep quality scoring
    quality_report = None
    try:
        from alert_quality import post_quality_scores

        quality_report = post_quality_scores(trace_id, alerts)
        result["quality"] = quality_report
    except Exception as qe:
        logger.debug("Quality scoring failed (non-blocking): %s", qe)

    # Capture to Langfuse dataset
    try:
        from langfuse_datasets import capture_decision_run
        from prompt_manager import get_prompt_version

        capture_decision_run(
            timeframe,
            merge_result["snapshots_json"],
            llm_response,
            alerts_json,
            quality_report,
            trace_id=trace_id,
            prompt_version=get_prompt_version(),
        )
    except Exception as de:
        logger.debug("Dataset capture failed (non-blocking): %s", de)

    if tag_trace is not None and trace_id:
        tag_trace(trace_id, [f"alerts:{len(alerts)}"])

    return result
