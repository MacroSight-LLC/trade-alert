"""Unit tests for langfuse_datasets.py — dataset capture and golden examples."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# capture_decision_run
# ---------------------------------------------------------------------------


class TestCaptureDecisionRun:
    """Tests for capture_decision_run."""

    @patch("langfuse_datasets.get_langfuse_client")
    def test_no_client_returns_early(self, mock_get: MagicMock) -> None:
        """Should silently return when Langfuse is not configured."""
        mock_get.return_value = None
        from langfuse_datasets import capture_decision_run

        # Should not raise
        capture_decision_run("15m", "[]", "", "[]")

    @patch("langfuse_datasets.get_langfuse_client")
    def test_successful_capture(self, mock_get: MagicMock) -> None:
        lf = MagicMock()
        mock_get.return_value = lf

        from langfuse_datasets import capture_decision_run

        snapshots = json.dumps([{"symbol": "AAPL", "signals": []}])
        alerts = json.dumps([{"symbol": "AAPL", "direction": "LONG"}])

        capture_decision_run(
            "15m",
            snapshots,
            "raw llm response",
            alerts,
            quality_report={"batch": {"score": 0.8}, "per_alert": []},
            trace_id="trace-123",
            prompt_version="v2",
        )

        lf.create_dataset_item.assert_called_once()
        call_kwargs = lf.create_dataset_item.call_args[1]
        assert call_kwargs["dataset_name"] == "decision-runs"
        assert call_kwargs["input"]["timeframe"] == "15m"
        assert call_kwargs["expected_output"]["alert_count"] == 1
        lf.flush.assert_called_once()

    @patch("langfuse_datasets.get_langfuse_client")
    def test_empty_alerts(self, mock_get: MagicMock) -> None:
        lf = MagicMock()
        mock_get.return_value = lf

        from langfuse_datasets import capture_decision_run

        capture_decision_run("1h", "[]", "", "[]")
        call_kwargs = lf.create_dataset_item.call_args[1]
        assert call_kwargs["expected_output"]["alert_count"] == 0

    @patch("langfuse_datasets.get_langfuse_client")
    def test_malformed_alerts_json(self, mock_get: MagicMock) -> None:
        """Malformed alerts JSON should not crash — gracefully produces empty alerts."""
        lf = MagicMock()
        mock_get.return_value = lf

        from langfuse_datasets import capture_decision_run

        capture_decision_run("15m", "[]", "", "not-valid-json")
        call_kwargs = lf.create_dataset_item.call_args[1]
        assert call_kwargs["expected_output"]["alert_count"] == 0

    @patch("langfuse_datasets.get_langfuse_client")
    def test_alerts_as_list_not_string(self, mock_get: MagicMock) -> None:
        """When alerts_json is already a list, should handle it."""
        lf = MagicMock()
        mock_get.return_value = lf

        from langfuse_datasets import capture_decision_run

        alerts_list = [{"symbol": "TSLA"}]
        capture_decision_run("15m", "[]", "", alerts_list)  # type: ignore[arg-type]
        call_kwargs = lf.create_dataset_item.call_args[1]
        assert call_kwargs["expected_output"]["alert_count"] == 1

    @patch("langfuse_datasets.get_langfuse_client")
    def test_langfuse_api_error_is_swallowed(self, mock_get: MagicMock) -> None:
        """API errors should be logged but not raised."""
        lf = MagicMock()
        lf.create_dataset_item.side_effect = RuntimeError("Langfuse 500")
        mock_get.return_value = lf

        from langfuse_datasets import capture_decision_run

        # Should not raise
        capture_decision_run("15m", "[]", "", "[]")


# ---------------------------------------------------------------------------
# promote_to_golden
# ---------------------------------------------------------------------------


class TestPromoteToGolden:
    """Tests for promote_to_golden."""

    @patch("langfuse_datasets.get_langfuse_client")
    def test_no_client(self, mock_get: MagicMock) -> None:
        mock_get.return_value = None
        from langfuse_datasets import promote_to_golden

        promote_to_golden("item-123")  # should not raise

    @patch("langfuse_datasets.get_langfuse_client")
    def test_successful_promotion(self, mock_get: MagicMock) -> None:
        lf = MagicMock()
        source_item = SimpleNamespace(
            input={"timeframe": "15m", "snapshots": []},
            expected_output={"alerts": [{"symbol": "AAPL"}], "alert_count": 1},
            metadata={"trace_id": "t1"},
        )
        lf.get_dataset_item.return_value = source_item
        mock_get.return_value = lf

        from langfuse_datasets import promote_to_golden

        promote_to_golden("item-123")
        lf.create_dataset_item.assert_called_once()
        call_kwargs = lf.create_dataset_item.call_args[1]
        assert call_kwargs["dataset_name"] == "decision-golden"

    @patch("langfuse_datasets.get_langfuse_client")
    def test_custom_expected_output(self, mock_get: MagicMock) -> None:
        lf = MagicMock()
        source_item = SimpleNamespace(
            input={"timeframe": "1h"},
            expected_output={"alerts": []},
            metadata={},
        )
        lf.get_dataset_item.return_value = source_item
        mock_get.return_value = lf

        from langfuse_datasets import promote_to_golden

        custom = {"alerts": [{"symbol": "NVDA"}], "alert_count": 1}
        promote_to_golden("item-456", expected_output=custom)
        call_kwargs = lf.create_dataset_item.call_args[1]
        assert call_kwargs["expected_output"] == custom


# ---------------------------------------------------------------------------
# get_golden_examples
# ---------------------------------------------------------------------------


class TestGetGoldenExamples:
    """Tests for get_golden_examples."""

    @patch("langfuse_datasets.get_langfuse_client")
    def test_no_client_returns_empty(self, mock_get: MagicMock) -> None:
        mock_get.return_value = None
        from langfuse_datasets import get_golden_examples

        assert get_golden_examples() == []

    @patch("langfuse_datasets.get_langfuse_client")
    def test_empty_dataset(self, mock_get: MagicMock) -> None:
        lf = MagicMock()
        lf.get_dataset.return_value = SimpleNamespace(items=[])
        mock_get.return_value = lf

        from langfuse_datasets import get_golden_examples

        assert get_golden_examples() == []

    @patch("langfuse_datasets.get_langfuse_client")
    def test_returns_examples(self, mock_get: MagicMock) -> None:
        lf = MagicMock()
        items = [
            SimpleNamespace(
                input={"timeframe": "15m"},
                expected_output={"alerts": [{"symbol": "TSLA"}], "alert_count": 1},
            ),
        ]
        lf.get_dataset.return_value = SimpleNamespace(items=items)
        mock_get.return_value = lf

        from langfuse_datasets import get_golden_examples

        result = get_golden_examples(n=5)
        assert len(result) == 1
        assert result[0]["input"]["timeframe"] == "15m"

    @patch("langfuse_datasets.get_langfuse_client")
    def test_dataset_error_returns_empty(self, mock_get: MagicMock) -> None:
        lf = MagicMock()
        lf.get_dataset.side_effect = RuntimeError("not found")
        mock_get.return_value = lf

        from langfuse_datasets import get_golden_examples

        assert get_golden_examples() == []
