"""Unit tests for collector-sentiment SpamShield workflow path."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW_PATH = Path(__file__).resolve().parents[2] / "workflows" / "collector-sentiment.yaml"


def _collect_code_blocks(steps: list) -> list[str]:
    blocks: list[str] = []
    for step in steps or []:
        for field in ("code", "if_true", "if_false"):
            val = step.get(field)
            if isinstance(val, dict):
                blocks.append(val.get("code", "") or "")
            elif isinstance(val, str):
                blocks.append(val)
    return blocks


def test_no_mcp_call_in_code_steps() -> None:
    wf = yaml.safe_load(WORKFLOW_PATH.read_text())
    for block in _collect_code_blocks(wf.get("steps", [])):
        assert "mcp_call(" not in block, "mcp_call() found in code: step — use tool_call/parallel_tool_calls"


def _aggregate_spam_results(
    text_items: list,
    raw_results: list,
    *,
    has_raw: bool,
) -> dict:
    if not has_raw:
        return {"spam_symbols": [], "spam_filtered_count": 0}
    spam_symbols: set[str] = set()
    for item, res in zip(text_items, raw_results):
        if not isinstance(res, dict):
            continue
        is_spam = res.get("is_spam", False) or res.get("label") == "spam"
        if is_spam:
            sym = item.get("symbol") or item.get("ticker", "")
            if sym:
                spam_symbols.add(sym.upper())
    return {"spam_symbols": list(spam_symbols), "spam_filtered_count": len(spam_symbols)}


def test_aggregate_spam_fail_open_on_error_result() -> None:
    text_items = [{"symbol": "AAPL", "headline": "test"}]
    raw_results = [{"error": "connection refused"}]
    result = _aggregate_spam_results(text_items, raw_results, has_raw=True)
    assert result["spam_symbols"] == []
    assert result["spam_filtered_count"] == 0


def test_aggregate_spam_skips_when_no_raw_text() -> None:
    result = _aggregate_spam_results([], [], has_raw=False)
    assert result == {"spam_symbols": [], "spam_filtered_count": 0}


def test_aggregate_spam_captures_spam_symbol() -> None:
    text_items = [{"symbol": "AAPL", "headline": "spam text"}]
    raw_results = [{"is_spam": True, "label": "spam"}]
    result = _aggregate_spam_results(text_items, raw_results, has_raw=True)
    assert "AAPL" in result["spam_symbols"]
    assert result["spam_filtered_count"] == 1


def test_workflow_defines_spamshield_parallel_steps() -> None:
    wf = yaml.safe_load(WORKFLOW_PATH.read_text())
    names = {s.get("name") for s in wf.get("steps", [])}
    assert "build-spam-calls" in names
    assert "classify-spam-parallel" in names
    assert "aggregate-spam-results" in names
    assert "classify-spam" not in names
