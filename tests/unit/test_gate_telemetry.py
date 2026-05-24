"""Unit tests for gate_telemetry.py (stabilization sprint)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from gate_telemetry import (
    log_decision_gate_summary,
    record_langfuse_gate_scores,
    record_prometheus_gate_metrics,
)
from validate_and_filter import GateRejection


def _pre_post_dist() -> tuple[dict[str, float], dict[str, float]]:
    pre = {"median_ep": 0.72, "median_conf": 0.78, "median_rr": 2.1, "median_sa": 4.0}
    post = {"median_ep": 0.80, "median_conf": 0.82, "median_rr": 2.5, "median_sa": 5.0}
    return pre, post


class TestRecordPrometheusGateMetrics:
    def test_increments_rejection_counters_and_observes_alerts(self) -> None:
        rejections = [
            ("AAPL", GateRejection.EP_THRESHOLD),
            ("MSFT", GateRejection.EP_THRESHOLD),
            ("TSLA", GateRejection.CONF_THRESHOLD),
        ]
        with patch("gate_telemetry.GATE_REJECTIONS") as mock_rejections:
            ep_labels = MagicMock()
            conf_labels = MagicMock()
            mock_rejections.labels.side_effect = lambda gate: {
                "ep_threshold": ep_labels,
                "conf_threshold": conf_labels,
            }[gate]
            with patch("gate_telemetry.ALERTS_PER_CYCLE") as mock_cycle:
                observe = MagicMock()
                mock_cycle.labels.return_value = observe
                record_prometheus_gate_metrics(
                    timeframe="15m",
                    alerts=[MagicMock()],
                    rejections=rejections,
                )
        ep_labels.inc.assert_called_once_with(2)
        conf_labels.inc.assert_called_once_with(1)
        mock_cycle.labels.assert_called_once_with(timeframe="15m")
        observe.observe.assert_called_once_with(1)

    def test_empty_rejections_only_observes_alerts(self) -> None:
        with patch("gate_telemetry.GATE_REJECTIONS") as mock_rejections:
            with patch("gate_telemetry.ALERTS_PER_CYCLE") as mock_cycle:
                observe = MagicMock()
                mock_cycle.labels.return_value = observe
                record_prometheus_gate_metrics(timeframe="1h", alerts=[], rejections=[])
        mock_rejections.labels.assert_not_called()
        observe.observe.assert_called_once_with(0)


class TestRecordLangfuseGateScores:
    def test_pushes_all_expected_scores(self) -> None:
        pre, post = _pre_post_dist()
        scores: list[tuple[str, float, str]] = []

        def _add_score(tid: str, name: str, val: float, comment: str = "") -> None:
            scores.append((name, val, comment))

        rejections = [("AAPL", GateRejection.EP_THRESHOLD)]
        record_langfuse_gate_scores(
            add_score_fn=_add_score,
            trace_id="trace-1",
            raw_count=4,
            alerts=[MagicMock(), MagicMock()],
            rejections=rejections,
            pre_dist=pre,
            post_dist=post,
        )
        names = {s[0] for s in scores}
        assert "alert_pass_rate" in names
        assert "alerts_fired" in names
        assert "gate_rejection_rate" in names
        assert "candidate_median_ep_pre" in names
        assert "candidate_median_conf_post" in names
        assert "gate_reject_ep_threshold" in names
        pass_rate = next(v for n, v, _ in scores if n == "alert_pass_rate")
        assert pass_rate == pytest.approx(0.5)

    def test_high_rejection_rate_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        pre, post = _pre_post_dist()
        rejections = [("S", GateRejection.EP_THRESHOLD) for _ in range(5)]
        with caplog.at_level(logging.WARNING):
            record_langfuse_gate_scores(
                add_score_fn=MagicMock(),
                trace_id="trace-2",
                raw_count=5,
                alerts=[],
                rejections=rejections,
                pre_dist=pre,
                post_dist=post,
            )
        assert any("exceeds 90%" in r.message for r in caplog.records)

    def test_zero_raw_count_avoids_division_error(self) -> None:
        pre, post = _pre_post_dist()
        scores: list[tuple[str, float, str]] = []

        def _add_score(_tid: str, name: str, val: float, comment: str = "") -> None:
            scores.append((name, val, comment))

        record_langfuse_gate_scores(
            add_score_fn=_add_score,
            trace_id="trace-3",
            raw_count=0,
            alerts=[],
            rejections=[],
            pre_dist=pre,
            post_dist=post,
        )
        pass_rate = next(v for n, v, _ in scores if n == "alert_pass_rate")
        assert pass_rate == pytest.approx(0.0)


class TestGracefulFailure:
    """Telemetry helpers tolerate missing/broken downstream sinks."""

    def test_prometheus_inc_failure_propagates_from_caller(self) -> None:
        """Document current behavior: Prometheus errors bubble to validate_and_filter."""
        with patch("gate_telemetry.GATE_REJECTIONS") as mock_rejections:
            mock_rejections.labels.return_value.inc.side_effect = RuntimeError("registry down")
            with pytest.raises(RuntimeError, match="registry down"):
                record_prometheus_gate_metrics(
                    timeframe="15m",
                    alerts=[],
                    rejections=[("X", GateRejection.EP_THRESHOLD)],
                )

    def test_langfuse_score_fn_failure_propagates(self) -> None:
        add_score = MagicMock(side_effect=RuntimeError("langfuse unavailable"))
        pre, post = _pre_post_dist()
        with pytest.raises(RuntimeError, match="langfuse unavailable"):
            record_langfuse_gate_scores(
                add_score_fn=add_score,
                trace_id="trace-4",
                raw_count=1,
                alerts=[],
                rejections=[],
                pre_dist=pre,
                post_dist=post,
            )

    def test_log_summary_with_empty_gate_samples(self, caplog: pytest.LogCaptureFixture) -> None:
        pre, post = _pre_post_dist()
        with caplog.at_level(logging.INFO):
            reason = log_decision_gate_summary(
                timeframe="15m",
                raw_count=1,
                candidates_count=1,
                alerts=[],
                directional_alerts=[],
                watch_alerts=[],
                rejections=[],
                directional_rejections=[],
                watch_rejections=[],
                regime="neutral",
                market_session="regular",
                trend_strength=0.5,
                bulls=3,
                bears=2,
                ep_gate=0.70,
                base_ep_gate=0.70,
                sa_gate=4,
                base_sa_gate=4,
                conf_gate=0.75,
                base_conf_gate=0.75,
                pre_dist=pre,
                post_dist=post,
            )
        assert reason == "no_actionable_candidates"

    def test_log_summary_llm_zero_candidates(self) -> None:
        pre, post = _pre_post_dist()
        reason = log_decision_gate_summary(
            timeframe="15m",
            raw_count=0,
            candidates_count=0,
            alerts=[],
            directional_alerts=[],
            watch_alerts=[],
            rejections=[],
            directional_rejections=[],
            watch_rejections=[],
            regime="neutral",
            market_session="regular",
            trend_strength=0.5,
            bulls=0,
            bears=0,
            ep_gate=0.70,
            base_ep_gate=0.70,
            sa_gate=4,
            base_sa_gate=4,
            conf_gate=0.75,
            base_conf_gate=0.75,
            pre_dist=pre,
            post_dist=post,
        )
        assert reason == "llm_zero_candidates"

    def test_log_summary_gate_filtered_reason(self) -> None:
        pre, post = _pre_post_dist()
        rejections = [("AAPL", GateRejection.EP_THRESHOLD), ("MSFT", GateRejection.EP_THRESHOLD)]
        reason = log_decision_gate_summary(
            timeframe="15m",
            raw_count=2,
            candidates_count=2,
            alerts=[],
            directional_alerts=[],
            watch_alerts=[],
            rejections=rejections,
            directional_rejections=rejections,
            watch_rejections=[],
            regime="neutral",
            market_session="regular",
            trend_strength=0.5,
            bulls=2,
            bears=1,
            ep_gate=0.70,
            base_ep_gate=0.70,
            sa_gate=4,
            base_sa_gate=4,
            conf_gate=0.75,
            base_conf_gate=0.75,
            pre_dist=pre,
            post_dist=post,
        )
        assert reason == "gate_filtered:ep_threshold"
