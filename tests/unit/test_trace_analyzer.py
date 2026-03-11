"""Unit tests for trace_analyzer self-healing module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from models import TraceAnalysis
from trace_analyzer import (
    analyze_pipeline_trace,
    check_cost,
    check_latency,
    check_output_validity,
)

# ── Fixtures ─────────────────────────────────────────────────────


def _valid_alert_json() -> str:
    """Return a valid PlaybookAlert JSON string."""
    return (
        '{"symbol":"AAPL","direction":"LONG","edge_probability":0.75,'
        '"confidence":0.80,"timeframe":"15m",'
        '"thesis":"BB squeeze with volume.",'
        '"entry":{"level":185.0,"stop":182.0,"target":192.0},'
        '"timeframe_rationale":"15m trend.",'
        '"sentiment_context":"Bullish.",'
        '"unusual_activity":["IV spike"],'
        '"macro_regime":"Risk-on.",'
        '"sources_agree":4}'
    )


def _valid_alert_dict() -> dict:
    """Return a valid PlaybookAlert as a dict."""
    return {
        "symbol": "AAPL",
        "direction": "LONG",
        "edge_probability": 0.75,
        "confidence": 0.80,
        "timeframe": "15m",
        "thesis": "BB squeeze with volume.",
        "entry": {"level": 185.0, "stop": 182.0, "target": 192.0},
        "timeframe_rationale": "15m trend.",
        "sentiment_context": "Bullish.",
        "unusual_activity": ["IV spike"],
        "macro_regime": "Risk-on.",
        "sources_agree": 4,
    }


def _make_trace(
    *,
    output: str | dict | None = None,
    cost: float = 0.10,
    latency: float = 30.0,
) -> dict:
    """Build a mock Langfuse trace dict."""
    return {
        "id": "trace-abc-123",
        "output": output,
        "calculatedTotalCost": cost,
        "latency": latency,
        "totalTokens": 5000,
    }


# ── check_output_validity ───────────────────────────────────────


class TestCheckOutputValidity:
    """Tests for LLM output validation."""

    def test_valid_json_string(self) -> None:
        trace = _make_trace(output=_valid_alert_json())
        issues = check_output_validity(trace)
        assert issues == []

    def test_valid_dict(self) -> None:
        trace = _make_trace(output=_valid_alert_dict())
        issues = check_output_validity(trace)
        assert issues == []

    def test_invalid_json(self) -> None:
        trace = _make_trace(output='{"symbol": "AAPL"}')
        issues = check_output_validity(trace)
        assert len(issues) == 1
        assert "PlaybookAlert validation failed" in issues[0]

    def test_no_output(self) -> None:
        trace = _make_trace(output=None)
        issues = check_output_validity(trace)
        assert issues == []

    def test_unexpected_type(self) -> None:
        trace = _make_trace(output=12345)
        issues = check_output_validity(trace)
        assert len(issues) == 1
        assert "Unexpected output type" in issues[0]


# ── check_cost ───────────────────────────────────────────────────


class TestCheckCost:
    """Tests for cost budget validation."""

    def test_within_budget(self) -> None:
        trace = _make_trace(cost=0.10)
        assert check_cost(trace, budget=0.50) == []

    def test_exceeds_budget(self) -> None:
        trace = _make_trace(cost=0.75)
        issues = check_cost(trace, budget=0.50)
        assert len(issues) == 1
        assert "exceeds budget" in issues[0]

    def test_exact_budget(self) -> None:
        trace = _make_trace(cost=0.50)
        assert check_cost(trace, budget=0.50) == []

    def test_zero_cost(self) -> None:
        trace = {"calculatedTotalCost": 0.0}
        assert check_cost(trace, budget=0.50) == []

    def test_missing_cost(self) -> None:
        trace = {}
        assert check_cost(trace, budget=0.50) == []


# ── check_latency ────────────────────────────────────────────────


class TestCheckLatency:
    """Tests for latency threshold validation."""

    def test_within_threshold(self) -> None:
        trace = _make_trace(latency=30.0)
        assert check_latency(trace, max_seconds=120.0) == []

    def test_exceeds_threshold(self) -> None:
        trace = _make_trace(latency=150.0)
        issues = check_latency(trace, max_seconds=120.0)
        assert len(issues) == 1
        assert "exceeds max" in issues[0]

    def test_exact_threshold(self) -> None:
        trace = _make_trace(latency=120.0)
        assert check_latency(trace, max_seconds=120.0) == []

    def test_missing_latency(self) -> None:
        trace = {}
        assert check_latency(trace, max_seconds=120.0) == []


# ── TraceAnalysis model ──────────────────────────────────────────


class TestTraceAnalysisModel:
    """Tests for the TraceAnalysis Pydantic model."""

    def test_healthy(self) -> None:
        ta = TraceAnalysis(
            trace_id="abc",
            is_healthy=True,
            timestamp="2026-03-10T00:00:00Z",
        )
        assert ta.issues == []
        assert ta.cost_usd == 0.0

    def test_unhealthy_with_issues(self) -> None:
        ta = TraceAnalysis(
            trace_id="abc",
            is_healthy=False,
            issues=["cost exceeded", "latency exceeded"],
            cost_usd=1.50,
            latency_s=200.0,
            timestamp="2026-03-10T00:00:00Z",
        )
        assert not ta.is_healthy
        assert len(ta.issues) == 2


# ── analyze_pipeline_trace (integration with mocks) ─────────────


class TestAnalyzePipelineTrace:
    """Tests for the main analysis entry point."""

    @patch("trace_analyzer.get_prompt_version", return_value="local-fallback")
    @patch("trace_analyzer.fetch_latest_trace", return_value=None)
    def test_no_trace_available(self, mock_fetch: MagicMock, _mock_pv: MagicMock) -> None:
        result = analyze_pipeline_trace("15m")
        assert result.trace_id == ""
        assert result.is_healthy is True
        assert "no_trace_available" in result.issues

    @patch("trace_analyzer.get_prompt_version", return_value="local-fallback")
    @patch("trace_analyzer.score_trace")
    @patch("trace_analyzer.fetch_latest_trace")
    def test_healthy_trace(self, mock_fetch: MagicMock, mock_score: MagicMock, _mock_pv: MagicMock) -> None:
        mock_fetch.return_value = {
            "id": "trace-123",
            "output": _valid_alert_dict(),
            "calculatedTotalCost": 0.10,
            "latency": 30.0,
            "totalTokens": 5000,
        }
        result = analyze_pipeline_trace("15m")
        assert result.is_healthy is True
        assert result.issues == []
        assert result.trace_id == "trace-123"
        mock_score.assert_called_once()
        # Score should be 1.0 for healthy
        assert mock_score.call_args[0][1] == 1.0

    @patch("trace_analyzer.get_prompt_version", return_value="local-fallback")
    @patch("trace_analyzer.send_ops_message", create=True)
    @patch("trace_analyzer.score_trace")
    @patch("trace_analyzer.fetch_latest_trace")
    def test_unhealthy_cost(
        self,
        mock_fetch: MagicMock,
        mock_score: MagicMock,
        mock_ops: MagicMock,
        _mock_pv: MagicMock,
    ) -> None:
        mock_fetch.return_value = {
            "id": "trace-456",
            "output": _valid_alert_dict(),
            "calculatedTotalCost": 1.50,
            "latency": 30.0,
            "totalTokens": 5000,
        }
        result = analyze_pipeline_trace("1h")
        assert result.is_healthy is False
        assert any("exceeds budget" in i for i in result.issues)
        # Score should be < 1.0 since there's an issue
        assert mock_score.call_args[0][1] < 1.0

    @patch("trace_analyzer.get_prompt_version", return_value="local-fallback")
    @patch("trace_analyzer.score_trace")
    @patch("trace_analyzer.fetch_latest_trace")
    def test_unhealthy_latency(
        self, mock_fetch: MagicMock, mock_score: MagicMock, _mock_pv: MagicMock
    ) -> None:
        mock_fetch.return_value = {
            "id": "trace-789",
            "output": _valid_alert_dict(),
            "calculatedTotalCost": 0.10,
            "latency": 200.0,
            "totalTokens": 5000,
        }
        result = analyze_pipeline_trace("15m")
        assert result.is_healthy is False
        assert any("exceeds max" in i for i in result.issues)

    @patch("trace_analyzer.get_prompt_version", return_value="local-fallback")
    @patch("trace_analyzer.score_trace")
    @patch("trace_analyzer.fetch_latest_trace")
    def test_invalid_output(self, mock_fetch: MagicMock, mock_score: MagicMock, _mock_pv: MagicMock) -> None:
        mock_fetch.return_value = {
            "id": "trace-bad",
            "output": '{"symbol": "AAPL"}',
            "calculatedTotalCost": 0.10,
            "latency": 30.0,
            "totalTokens": 5000,
        }
        result = analyze_pipeline_trace("15m")
        assert result.is_healthy is False
        assert any("PlaybookAlert validation failed" in i for i in result.issues)
