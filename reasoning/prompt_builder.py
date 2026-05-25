"""Pure prompt transformer: merge + FRED inputs → ReasoningPrompt."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Expected ``mcp_context`` keys passed to ``get_decision_prompts`` (future PromptContext):
#   macro_summary (str), vix (str), yc (str), n (int), snapshots_json (str),
#   market_reference_context (str), data_freshness (str), performance_context (str),
#   few_shot_examples (str)


@dataclass(frozen=True)
class FredContext:
    """Normalized FRED MCP values for decision prompt template vars."""

    vix: str
    yield_curve_bps: str
    data_freshness: str


@dataclass(frozen=True)
class ReasoningPrompt:
    """Fully-rendered decision prompt components (immutable record)."""

    system: str
    user: str
    prompt_version: str
    timeframe: str
    mcp_context: dict[str, Any]

    def to_llm_prompt(self) -> str:
        return f"SYSTEM:\n{self.system}\n\nUSER:\n{self.user}"

    def to_workflow_result(self) -> dict[str, str]:
        return {"prompt": self.to_llm_prompt(), "prompt_version": self.prompt_version}


def market_reference_context(snaps_json: str, limit: int = 20) -> str:
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


def parse_fred_context(fred_results: list[dict[str, Any]]) -> FredContext:
    """Parse VIX and yield-curve from FRED MCP parallel_tool_calls results."""
    fred_vix = fred_results[0] if len(fred_results) > 0 else {}
    fred_yc = fred_results[1] if len(fred_results) > 1 else {}
    vix = fred_vix.get("vix_level") or fred_vix.get("value", "N/A")
    yc = fred_yc.get("spread_bps") or fred_yc.get("value", "N/A")

    try:
        if vix != "N/A" and float(vix) == 0.0:
            logger.warning("FRED staleness: VIX returned 0.0 — marking as STALE")
            vix = "STALE"
    except (TypeError, ValueError):
        pass
    try:
        if yc != "N/A" and float(yc) == 0.0:
            logger.warning("FRED staleness: yield-curve spread returned 0.0 — marking as STALE")
            yc = "STALE"
    except (TypeError, ValueError):
        pass

    fred_live = vix not in ("N/A", "STALE") and yc not in ("N/A", "STALE")
    data_freshness = "LIVE" if fred_live else "CACHED (stale — FRED unavailable)"
    return FredContext(vix=str(vix), yield_curve_bps=str(yc), data_freshness=data_freshness)


def build_prompt(
    timeframe: str,
    merge_result: dict[str, Any],
    fred_results: list[dict[str, Any]],
) -> ReasoningPrompt:
    """Build the ensemble prompt for the decision LLM."""
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

    fred = parse_fred_context(fred_results)
    risk_on = macro.get("risk_on", True)
    macro_summary = (
        f"{'Risk-on' if risk_on else 'Risk-off'}, "
        f"VIX={fred.vix}, Yield curve={fred.yield_curve_bps}bps"
    )

    perf_ctx = format_winrate_context()
    escalation = get_quality_escalation_rules(timeframe)
    if escalation:
        perf_ctx = perf_ctx + "\n" + escalation if perf_ctx else escalation

    mcp_context: dict[str, Any] = {
        "macro_summary": macro_summary,
        "vix": fred.vix,
        "yc": fred.yield_curve_bps,
        "n": n,
        "snapshots_json": snapshots_json,
        "market_reference_context": market_reference_context(snapshots_json),
        "data_freshness": fred.data_freshness,
        "performance_context": perf_ctx,
        "few_shot_examples": format_golden_examples(),
    }

    system_prompt, user_prompt = get_decision_prompts(timeframe, mcp_context)

    return ReasoningPrompt(
        system=system_prompt,
        user=user_prompt,
        prompt_version=get_prompt_version(),
        timeframe=timeframe,
        mcp_context=mcp_context,
    )


class PromptBuilder:
    """Namespace for prompt construction (stateless)."""

    @staticmethod
    def build(
        timeframe: str,
        merge_result: dict[str, Any],
        fred_results: list[dict[str, Any]],
    ) -> ReasoningPrompt:
        return build_prompt(timeframe, merge_result, fred_results)
