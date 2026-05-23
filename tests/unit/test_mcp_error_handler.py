"""Unit tests for resilience.mcp_error_handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from resilience.mcp_error_handler import apply_mcp_error_strategy

ERROR_RESULT = {"error": "connection refused"}
OK_RESULT = {"data": 1}


@pytest.mark.parametrize(
    "strategy,expected",
    [
        ("skip", {}),
        ("fail_open", {}),
        ("continue_partial", ERROR_RESULT),
    ],
)
def test_global_strategy_dispatch(strategy: str, expected: dict) -> None:
    cfg = {"on_mcp_error": {"strategy": strategy}}
    assert apply_mcp_error_strategy("some-mcp", "method", ERROR_RESULT, cfg) == expected


def test_per_tool_config_wins_over_global() -> None:
    cfg = {
        "on_mcp_error": {
            "strategy": "continue_partial",
            "rot-mcp": {"strategy": "skip"},
        }
    }
    assert apply_mcp_error_strategy("rot-mcp", "method", ERROR_RESULT, cfg) == {}


def test_fallback_to_cache_returns_cached_value() -> None:
    mock_redis = MagicMock()
    mock_redis.get.return_value = json.dumps({"cached": True})
    cfg = {
        "on_mcp_error": {
            "some-mcp": {
                "strategy": "fallback_to_cache",
                "fallback_key": "universe:equities",
            }
        }
    }
    result = apply_mcp_error_strategy(
        "some-mcp",
        "get",
        ERROR_RESULT,
        cfg,
        get_redis=lambda: mock_redis,
    )
    assert result == {"cached": True}


def test_fallback_to_cache_workflow_level_hardcoded() -> None:
    cfg = {
        "on_mcp_error": {
            "strategy": "fallback_to_cache",
            "hardcoded_fallback": {"equities": ["AAPL"]},
        }
    }
    result = apply_mcp_error_strategy("some-mcp", "get", ERROR_RESULT, cfg, get_redis=None)
    assert result == {"equities": ["AAPL"]}


def test_fallback_to_cache_returns_empty_when_no_cache() -> None:
    mock_redis = MagicMock()
    mock_redis.get.return_value = None
    cfg = {
        "on_mcp_error": {
            "some-mcp": {
                "strategy": "fallback_to_cache",
                "fallback_key": "missing:key",
            }
        }
    }
    result = apply_mcp_error_strategy(
        "some-mcp",
        "get",
        ERROR_RESULT,
        cfg,
        get_redis=lambda: mock_redis,
    )
    assert result == {}


def test_fallback_to_cache_returns_empty_when_no_get_redis() -> None:
    cfg = {"on_mcp_error": {"some-mcp": {"strategy": "fallback_to_cache", "fallback_key": "k"}}}
    result = apply_mcp_error_strategy("some-mcp", "get", ERROR_RESULT, cfg, get_redis=None)
    assert result == {}


def test_non_error_result_passes_through_unchanged() -> None:
    cfg = {"on_mcp_error": {"strategy": "skip"}}
    assert apply_mcp_error_strategy("mcp", "method", OK_RESULT, cfg) == OK_RESULT


def test_continue_partial_returns_original_error_dict() -> None:
    cfg = {"on_mcp_error": {"strategy": "continue_partial"}}
    assert apply_mcp_error_strategy("mcp", "method", ERROR_RESULT, cfg) == ERROR_RESULT
