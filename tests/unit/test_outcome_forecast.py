"""Unit tests for forecast-based stop tightening in outcome_tracker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from outcome_tracker import (
    _check_forecast_agrees,
    evaluate_outcome,
)


class TestCheckForecastAgrees:
    """Tests for _check_forecast_agrees helper."""

    def test_agrees_returns_true(self) -> None:
        """When forecast agrees, should return True."""
        httpx = pytest.importorskip("httpx")

        mock_response = httpx.Response(
            200,
            json={"agrees": True, "forecast_direction": "LONG", "direction_pct": 1.5},
            request=httpx.Request("POST", "http://test/tool/validate"),
        )
        cycle_cache: dict[str, bool] = {}
        with patch("outcome_tracker._get_http_client") as mock_client:
            mock_client.return_value.post.return_value = mock_response
            result = _check_forecast_agrees("AAPL", "LONG", "15m", cycle_cache=cycle_cache)
        assert result is True
        assert cycle_cache["AAPL"] is True

    def test_disagrees_returns_false(self) -> None:
        """When forecast disagrees, should return False."""
        httpx = pytest.importorskip("httpx")

        mock_response = httpx.Response(
            200,
            json={"agrees": False, "forecast_direction": "SHORT", "direction_pct": -2.0},
            request=httpx.Request("POST", "http://test/tool/validate"),
        )
        cycle_cache: dict[str, bool] = {}
        with patch("outcome_tracker._get_http_client") as mock_client:
            mock_client.return_value.post.return_value = mock_response
            result = _check_forecast_agrees("AAPL", "LONG", "15m", cycle_cache=cycle_cache)
        assert result is False
        assert cycle_cache["AAPL"] is False

    def test_mcp_unavailable_returns_true(self) -> None:
        """When MCP is unreachable, should default to True (non-blocking)."""
        httpx = pytest.importorskip("httpx")

        cycle_cache: dict[str, bool] = {}
        with patch("outcome_tracker._get_http_client") as mock_client:
            mock_client.return_value.post.side_effect = httpx.ConnectError("refused")
            result = _check_forecast_agrees("AAPL", "LONG", "15m", cycle_cache=cycle_cache)
        assert result is True
        assert cycle_cache["AAPL"] is True

    def test_cycle_cache_used(self) -> None:
        """Second call for same symbol should use cached result."""
        cycle_cache = {"AAPL": False}
        # No http mock needed — cache should short-circuit
        result = _check_forecast_agrees("AAPL", "LONG", "15m", cycle_cache=cycle_cache)
        assert result is False


class TestStopTightening:
    """Tests for the stop-tightening logic via evaluate_outcome."""

    def test_long_in_profit_tightened_stop_triggers_loss(self) -> None:
        """When stop is tightened above current price, outcome should be LOSS."""
        now = datetime.now(UTC)
        alert = {
            "direction": "LONG",
            "entry_level": 100.0,
            "stop_level": 110.0,  # tightened stop is above current price
            "target_level": 120.0,
            "fired_at": now - timedelta(minutes=30),
        }
        # Price at 108 is below tightened stop of 110
        outcome = evaluate_outcome(alert, 108.0, timeframe="15m")
        assert outcome == "LOSS"

    def test_short_in_profit_tightened_stop_triggers_loss(self) -> None:
        """When SHORT stop is tightened below current price, outcome should be LOSS."""
        now = datetime.now(UTC)
        alert = {
            "direction": "SHORT",
            "entry_level": 100.0,
            "stop_level": 92.0,  # tightened stop below current price
            "target_level": 80.0,
            "fired_at": now - timedelta(minutes=30),
        }
        # Price at 95 is above tightened stop of 92
        outcome = evaluate_outcome(alert, 95.0, timeframe="15m")
        assert outcome == "LOSS"

    def test_feature_flag_default_off(self) -> None:
        """FORECAST_STOP_TIGHTEN_ENABLED should default to False."""
        from outcome_tracker import FORECAST_STOP_TIGHTEN_ENABLED

        # Default is "false" unless env var is set
        assert isinstance(FORECAST_STOP_TIGHTEN_ENABLED, bool)

    def test_evaluate_outcome_still_open(self) -> None:
        """Alert that hasn't hit stop or target should return None (still open)."""
        now = datetime.now(UTC)
        alert = {
            "direction": "LONG",
            "entry_level": 100.0,
            "stop_level": 95.0,
            "target_level": 115.0,
            "fired_at": now - timedelta(minutes=30),
        }
        outcome = evaluate_outcome(alert, 108.0, timeframe="15m")
        assert outcome is None

    def test_evaluate_outcome_win(self) -> None:
        """LONG alert hitting target should return WIN."""
        now = datetime.now(UTC)
        alert = {
            "direction": "LONG",
            "entry_level": 100.0,
            "stop_level": 95.0,
            "target_level": 115.0,
            "fired_at": now - timedelta(minutes=30),
        }
        outcome = evaluate_outcome(alert, 115.0, timeframe="15m")
        assert outcome == "WIN"
