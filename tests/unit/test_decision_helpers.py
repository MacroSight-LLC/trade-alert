"""Unit tests for decision_helpers.py — merge_snapshots and build_prompt."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from decision_helpers import merge_snapshots


class TestMergeSnapshots:
    """Tests for merge_snapshots function."""

    def test_uses_orchestrator_inputs_when_present(self) -> None:
        inputs = {
            "merged_snapshots_json": '[{"symbol": "AAPL"}]',
            "merged_macro": {"risk_on": True},
            "merged_n": 1,
        }
        result = merge_snapshots("15m", inputs)
        assert result["skip"] is False
        assert result["n"] == 1
        assert "AAPL" in result["snapshots_json"]
        assert result["macro"] == {"risk_on": True}

    def test_skip_when_n_is_zero(self) -> None:
        inputs = {
            "merged_snapshots_json": "[]",
            "merged_macro": {},
            "merged_n": 0,
        }
        result = merge_snapshots("15m", inputs)
        assert result["skip"] is True
        assert result["n"] == 0

    def test_none_inputs_falls_through_to_redis(self) -> None:
        mock_merger = MagicMock()
        mock_merger.merge.return_value = []
        mock_merger.get_macro_regime.return_value = {}
        with patch.dict(sys.modules, {"merger": mock_merger}):
            result = merge_snapshots("15m", None)
            assert result["skip"] is True
            assert result["n"] == 0

    def test_empty_inputs_falls_through_to_redis(self) -> None:
        mock_merger = MagicMock()
        mock_merger.merge.return_value = []
        mock_merger.get_macro_regime.return_value = {}
        with patch.dict(sys.modules, {"merger": mock_merger}):
            result = merge_snapshots("15m", {})
            assert result["skip"] is True

    def test_invalid_merged_n_type(self) -> None:
        inputs = {
            "merged_snapshots_json": '[{"symbol": "AAPL"}]',
            "merged_macro": {},
            "merged_n": "not_a_number",
        }
        result = merge_snapshots("15m", inputs)
        assert result["skip"] is True
        assert result["n"] == 0

    def test_missing_merged_macro_defaults_empty(self) -> None:
        inputs = {
            "merged_snapshots_json": '[{"symbol": "AAPL"}]',
            "merged_n": 1,
        }
        result = merge_snapshots("15m", inputs)
        assert result["macro"] == {}

    @pytest.mark.parametrize("timeframe", ["15m", "1h"])
    def test_both_timeframes(self, timeframe: str) -> None:
        inputs = {
            "merged_snapshots_json": "[]",
            "merged_macro": {},
            "merged_n": 0,
        }
        result = merge_snapshots(timeframe, inputs)
        assert result["skip"] is True
