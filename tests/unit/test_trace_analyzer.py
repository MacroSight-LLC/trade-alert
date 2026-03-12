"""Unit tests for trace_analyzer self-healing module."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from models import TraceAnalysis
from trace_analyzer import (
    _sum_tokens,
    analyze_pipeline_trace,
    check_cost,
    check_latency,
    check_output_validity,
    fetch_latest_trace,
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
        "total_cost": cost,
        "latency": latency,
        "observations": [
            {"usage": {"total": 2500}},
            {"usage": {"total": 2500}},
        ],
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
        trace = {"total_cost": 0.0}
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
            "output": {**_valid_alert_dict(), "merger_candidates": 10},
            "total_cost": 0.10,
            "latency": 30.0,
            "observations": [
                {"usage": {"total": 2500}},
                {"usage": {"total": 2500}},
            ],
        }
        result = analyze_pipeline_trace("15m")
        assert result.is_healthy is True
        assert result.issues == []
        assert result.trace_id == "trace-123"
        assert result.total_tokens == 5000
        assert result.llm_calls == 2
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
            "total_cost": 1.50,
            "latency": 30.0,
            "observations": [],
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
            "total_cost": 0.10,
            "latency": 200.0,
            "observations": [],
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
            "total_cost": 0.10,
            "latency": 30.0,
            "observations": [],
        }
        result = analyze_pipeline_trace("15m")
        assert result.is_healthy is False
        assert any("PlaybookAlert validation failed" in i for i in result.issues)


# ── _sum_tokens ───────────────────────────────────────────────────────


class TestSumTokens:
    """Tests for _sum_tokens helper."""

    def test_empty_observations(self) -> None:
        assert _sum_tokens([]) == 0

    def test_sums_dict_observations(self) -> None:
        obs = [
            {"usage": {"total": 100}},
            {"usage": {"total": 200}},
        ]
        assert _sum_tokens(obs) == 300

    def test_skips_none_usage(self) -> None:
        obs = [{"usage": None}, {"usage": {"total": 50}}]
        assert _sum_tokens(obs) == 50

    def test_skips_missing_usage(self) -> None:
        obs = [{"name": "span"}, {"usage": {"total": 75}}]
        assert _sum_tokens(obs) == 75

    def test_handles_object_style(self) -> None:
        obs = [
            SimpleNamespace(usage=SimpleNamespace(total=120)),
            SimpleNamespace(usage=SimpleNamespace(total=80)),
        ]
        assert _sum_tokens(obs) == 200


# ── fetch_latest_trace ─────────────────────────────────────────────────


class TestFetchLatestTrace:
    """Tests for fetch_latest_trace."""

    @patch("trace_analyzer.get_langfuse_client", return_value=None)
    def test_returns_none_when_no_client(self, _m: MagicMock) -> None:
        assert fetch_latest_trace("orchestrator-15m") is None

    @patch("trace_analyzer.get_langfuse_client")
    def test_returns_none_when_no_traces(self, mock_get: MagicMock) -> None:
        lf = MagicMock()
        lf.fetch_traces.return_value = SimpleNamespace(data=[])
        mock_get.return_value = lf
        assert fetch_latest_trace("orchestrator-15m") is None

    @patch("trace_analyzer.get_langfuse_client")
    def test_fetches_full_trace_by_id(self, mock_get: MagicMock) -> None:
        lf = MagicMock()
        lf.fetch_traces.return_value = SimpleNamespace(data=[SimpleNamespace(id="t-1")])
        full = SimpleNamespace(
            id="t-1",
            total_cost=0.03,
            latency=1.5,
            output=None,
            observations=[],
        )
        lf.fetch_trace.return_value = SimpleNamespace(data=full)
        mock_get.return_value = lf

        result = fetch_latest_trace("orchestrator-15m")
        lf.fetch_trace.assert_called_once_with("t-1")
        assert result["id"] == "t-1"
        assert result["total_cost"] == 0.03
        assert result["latency"] == 1.5
        assert result["observations"] == []

    @patch("trace_analyzer.get_langfuse_client")
    def test_returns_none_on_exception(self, mock_get: MagicMock) -> None:
        lf = MagicMock()
        lf.fetch_traces.side_effect = RuntimeError("network")
        mock_get.return_value = lf
        assert fetch_latest_trace("orchestrator-15m") is None
