"""Unit tests for pipeline_tracing — Langfuse span instrumentation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pipeline_tracing as pt

# ── create_pipeline_trace ────────────────────────────────────────


class TestCreatePipelineTrace:
    """Tests for root trace creation."""

    @patch("pipeline_tracing.get_langfuse_client", return_value=None)
    def test_returns_none_when_unconfigured(self, _mock: MagicMock) -> None:
        assert pt.create_pipeline_trace("15m") is None

    @patch("pipeline_tracing.get_langfuse_client")
    def test_creates_trace_with_correct_session_id(self, mock_client: MagicMock) -> None:
        trace_obj = MagicMock()
        trace_obj.id = "trace-abc-123"
        lf = MagicMock()
        lf.trace.return_value = trace_obj
        mock_client.return_value = lf

        result = pt.create_pipeline_trace("15m")

        assert result == "trace-abc-123"
        lf.trace.assert_called_once_with(
            name="pipeline-15m",
            session_id="orchestrator-15m",
            metadata={},
            tags=["timeframe:15m", "pipeline"],
        )

    @patch("pipeline_tracing.get_langfuse_client")
    def test_1h_session_id(self, mock_client: MagicMock) -> None:
        trace_obj = MagicMock()
        trace_obj.id = "trace-1h-456"
        lf = MagicMock()
        lf.trace.return_value = trace_obj
        mock_client.return_value = lf

        result = pt.create_pipeline_trace("1h", metadata={"schedule": "hourly"})

        assert result == "trace-1h-456"
        lf.trace.assert_called_once_with(
            name="pipeline-1h",
            session_id="orchestrator-1h",
            metadata={"schedule": "hourly"},
            tags=["timeframe:1h", "pipeline"],
        )

    @patch("pipeline_tracing.get_langfuse_client")
    def test_returns_none_on_sdk_error(self, mock_client: MagicMock) -> None:
        lf = MagicMock()
        lf.trace.side_effect = RuntimeError("SDK broken")
        mock_client.return_value = lf

        assert pt.create_pipeline_trace("15m") is None


# ── span_step ────────────────────────────────────────────────────


class TestSpanStep:
    """Tests for the span context manager."""

    def test_noop_when_trace_id_is_none(self) -> None:
        with pt.span_step(None, "test-step") as ctx:
            ctx["output"] = {"ok": True}
        # Should not raise

    @patch("pipeline_tracing.get_langfuse_client", return_value=None)
    def test_noop_when_client_is_none(self, _mock: MagicMock) -> None:
        with pt.span_step("some-trace-id", "test-step") as ctx:
            ctx["output"] = {"ok": True}
        # Should not raise

    @patch("pipeline_tracing.get_langfuse_client")
    def test_creates_and_ends_span(self, mock_client: MagicMock) -> None:
        span_obj = MagicMock()
        trace_ref = MagicMock()
        trace_ref.span.return_value = span_obj
        lf = MagicMock()
        lf.trace.return_value = trace_ref
        mock_client.return_value = lf

        with pt.span_step("trace-123", "run-collectors", input_data={"n": 5}) as ctx:
            ctx["output"] = {"symbols": 20}
            ctx["status_message"] = "done"

        # Span should have been created and ended
        trace_ref.span.assert_called_once()
        span_call_kwargs = trace_ref.span.call_args[1]
        assert span_call_kwargs["name"] == "run-collectors"
        assert span_call_kwargs["input"] == {"n": 5}

        span_obj.end.assert_called_once()
        end_kwargs = span_obj.end.call_args[1]
        assert end_kwargs["output"] == {"symbols": 20}
        assert end_kwargs["status_message"] == "done"

    @patch("pipeline_tracing.get_langfuse_client")
    def test_span_still_ends_on_exception(self, mock_client: MagicMock) -> None:
        span_obj = MagicMock()
        trace_ref = MagicMock()
        trace_ref.span.return_value = span_obj
        lf = MagicMock()
        lf.trace.return_value = trace_ref
        mock_client.return_value = lf

        try:
            with pt.span_step("trace-123", "failing-step"):
                raise ValueError("boom")
        except ValueError:
            pass

        # Span should still have been closed
        span_obj.end.assert_called_once()


# ── end_pipeline_trace ───────────────────────────────────────────


class TestEndPipelineTrace:
    """Tests for trace finalisation."""

    def test_noop_when_trace_id_is_none(self) -> None:
        pt.end_pipeline_trace(None)  # Should not raise

    @patch("pipeline_tracing.get_langfuse_client", return_value=None)
    def test_noop_when_client_is_none(self, _mock: MagicMock) -> None:
        pt.end_pipeline_trace("trace-123")  # Should not raise

    @patch("pipeline_tracing.get_langfuse_client")
    def test_updates_and_flushes_trace(self, mock_client: MagicMock) -> None:
        trace_ref = MagicMock()
        lf = MagicMock()
        lf.trace.return_value = trace_ref
        mock_client.return_value = lf

        pt.end_pipeline_trace(
            "trace-456",
            output={"status": "completed"},
            metadata={"extra": "data"},
        )

        trace_ref.update.assert_called_once_with(
            output={"status": "completed"},
            metadata={"extra": "data"},
        )
        lf.flush.assert_called_once()

    @patch("pipeline_tracing.get_langfuse_client")
    def test_handles_sdk_error_gracefully(self, mock_client: MagicMock) -> None:
        lf = MagicMock()
        lf.trace.side_effect = RuntimeError("flush failed")
        mock_client.return_value = lf

        # Should not raise
        pt.end_pipeline_trace("trace-789", output={"done": True})
