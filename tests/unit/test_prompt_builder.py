"""Unit tests for reasoning.prompt_builder."""

from __future__ import annotations

import dataclasses
import json
from unittest.mock import patch

import pytest

from reasoning.prompt_builder import (
    PromptContext,
    ReasoningPrompt,
    build_prompt,
    market_reference_context,
    parse_fred_context,
)

_MERGE_RESULT = {
    "macro": {"risk_on": True},
    "snapshots_json": "[]",
    "n": 0,
}

_FRED_LIVE = [{"vix_level": 18.5}, {"spread_bps": 42}]


class TestBuildPromptHappyPath:
    def test_returns_reasoning_prompt_and_workflow_dict(self) -> None:
        with (
            patch(
                "prompt_manager.format_golden_examples",
                return_value="examples",
            ),
            patch(
                "prompt_manager.format_winrate_context",
                return_value="winrate",
            ),
            patch(
                "prompt_manager.get_quality_escalation_rules",
                return_value="",
            ),
            patch(
                "prompt_manager.get_prompt_version",
                return_value="v1.2.3",
            ),
            patch(
                "prompt_manager.get_decision_prompts",
                return_value=("System text", "User text"),
            ) as mock_get,
        ):
            result = build_prompt("15m", _MERGE_RESULT, _FRED_LIVE)

        assert result.system == "System text"
        assert result.user == "User text"
        assert result.prompt_version == "v1.2.3"
        assert result.timeframe == "15m"
        assert isinstance(result.context, PromptContext)
        assert result.context.vix == "18.5"
        assert result.context.yc == "42"
        assert result.context.data_freshness == "LIVE"
        mock_get.assert_called_once_with("15m", result.context.as_template_vars())

        workflow = result.to_workflow_result()
        assert set(workflow.keys()) == {"prompt", "prompt_version"}
        assert workflow["prompt_version"] == "v1.2.3"
        assert workflow["prompt"] == result.to_llm_prompt()


class TestParseFredContext:
    def test_empty_list_returns_na(self) -> None:
        ctx = parse_fred_context([])
        assert ctx.vix == "N/A"
        assert ctx.yield_curve_bps == "N/A"
        assert "CACHED" in ctx.data_freshness

    def test_single_element_yield_curve_na(self) -> None:
        ctx = parse_fred_context([{"vix_level": 22.0}])
        assert ctx.vix == "22.0"
        assert ctx.yield_curve_bps == "N/A"

    def test_zero_vix_level_marked_stale(self) -> None:
        ctx = parse_fred_context([{"vix_level": 0.0}, {"spread_bps": 10}])
        assert ctx.vix == "STALE"
        assert ctx.yield_curve_bps == "10"

    def test_zero_yield_curve_marked_stale(self) -> None:
        ctx = parse_fred_context([{"vix_level": 15.0}, {"spread_bps": 0.0}])
        assert ctx.vix == "15.0"
        assert ctx.yield_curve_bps == "STALE"


class TestMarketReferenceContext:
    def test_bad_json_returns_empty(self) -> None:
        assert market_reference_context("not-json") == ""

    def test_key_fallback_order(self) -> None:
        snaps = [
            {
                "symbol": "aapl",
                "signals": [{"raw": {"price": 150.0}}],
            }
        ]
        result = market_reference_context(json.dumps(snaps))
        assert "- AAPL: $150.00" in result

    def test_limit_caps_symbols(self) -> None:
        snaps = [{"symbol": f"S{i}", "signals": [{"raw": {"price": float(i + 1)}}]} for i in range(25)]
        lines = market_reference_context(json.dumps(snaps), limit=20)
        assert lines.count("- ") == 20


class TestPromptContext:
    def test_as_template_vars_keys(self) -> None:
        ctx = PromptContext(
            macro_summary="Risk-on",
            vix="18",
            yc="42",
            n=3,
            snapshots_json="[]",
            market_reference_context="",
            data_freshness="LIVE",
            performance_context="",
            few_shot_examples="",
        )
        vars_ = ctx.as_template_vars()
        assert vars_["vix"] == "18"
        assert vars_["n"] == 3


class TestReasoningPromptContract:
    def test_to_llm_prompt_llm_client_compatibility(self) -> None:
        context = PromptContext(
            macro_summary="m",
            vix="18",
            yc="42",
            n=0,
            snapshots_json="[]",
            market_reference_context="",
            data_freshness="LIVE",
            performance_context="",
            few_shot_examples="",
        )
        prompt = ReasoningPrompt(
            system="SYS",
            user="USR",
            prompt_version="v1",
            timeframe="15m",
            context=context,
        )
        result = prompt.to_llm_prompt()
        system_part, user_part = result.split("\n\nUSER:\n", 1)
        assert system_part == "SYSTEM:\nSYS"
        assert user_part == "USR"

    def test_frozen_immutability(self) -> None:
        context = PromptContext(
            macro_summary="m",
            vix="18",
            yc="42",
            n=0,
            snapshots_json="[]",
            market_reference_context="",
            data_freshness="LIVE",
            performance_context="",
            few_shot_examples="",
        )
        prompt = ReasoningPrompt(
            system="S",
            user="U",
            prompt_version="v",
            timeframe="1h",
            context=context,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            prompt.system = "changed"  # type: ignore[misc]
