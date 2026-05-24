"""Unit tests for metrics.py — all Prometheus counters/gauges/histograms."""

from __future__ import annotations

import metrics


class TestPipelineMetrics:
    def test_pipeline_runs_counter(self) -> None:
        before = metrics.PIPELINE_RUNS.labels(workflow="decision-15m", status="success")._value.get()
        metrics.PIPELINE_RUNS.labels(workflow="decision-15m", status="success").inc()
        after = metrics.PIPELINE_RUNS.labels(workflow="decision-15m", status="success")._value.get()
        assert after == before + 1

    def test_mcp_call_duration_histogram(self) -> None:
        metrics.MCP_CALL_DURATION.labels(tool="polygon", method="get_quote").observe(0.25)

    def test_mcp_circuit_breaker_trips(self) -> None:
        before = metrics.MCP_CIRCUIT_BREAKER_TRIPS.labels(endpoint="polygon-mcp")._value.get()
        metrics.MCP_CIRCUIT_BREAKER_TRIPS.labels(endpoint="polygon-mcp").inc()
        after = metrics.MCP_CIRCUIT_BREAKER_TRIPS.labels(endpoint="polygon-mcp")._value.get()
        assert after == before + 1

    def test_pipeline_last_run_gauge(self) -> None:
        metrics.PIPELINE_LAST_RUN.labels(workflow="decision-1h").set(1_700_000_000.0)
        assert metrics.PIPELINE_LAST_RUN.labels(workflow="decision-1h")._value.get() == 1_700_000_000.0


class TestGateMetrics:
    def test_gate_rejections_counter(self) -> None:
        before = metrics.GATE_REJECTIONS.labels(gate="ep_threshold")._value.get()
        metrics.GATE_REJECTIONS.labels(gate="ep_threshold").inc(2)
        after = metrics.GATE_REJECTIONS.labels(gate="ep_threshold")._value.get()
        assert after == before + 2

    def test_alerts_per_cycle_histogram(self) -> None:
        metrics.ALERTS_PER_CYCLE.labels(timeframe="15m").observe(3)


class TestNotifierMetrics:
    def test_discord_sends_counter(self) -> None:
        before = metrics.DISCORD_SENDS.labels(status="success")._value.get()
        metrics.DISCORD_SENDS.labels(status="success").inc()
        after = metrics.DISCORD_SENDS.labels(status="success")._value.get()
        assert after == before + 1

    def test_db_inserts_counter(self) -> None:
        before = metrics.DB_INSERTS.labels(status="success")._value.get()
        metrics.DB_INSERTS.labels(status="success").inc()
        after = metrics.DB_INSERTS.labels(status="success")._value.get()
        assert after == before + 1

    def test_chart_gen_duration_histogram(self) -> None:
        metrics.CHART_GEN_DURATION.observe(0.5)


class TestRedisCircuitMetrics:
    def test_redis_circuit_open_gauge(self) -> None:
        metrics.REDIS_CIRCUIT_OPEN.set(1)
        assert metrics.REDIS_CIRCUIT_OPEN._value.get() == 1.0
        metrics.REDIS_CIRCUIT_OPEN.set(0)
        assert metrics.REDIS_CIRCUIT_OPEN._value.get() == 0.0

    def test_watch_decay_skipped_counter(self) -> None:
        before = metrics.WATCH_DECAY_SKIPPED._value.get()
        metrics.WATCH_DECAY_SKIPPED.inc()
        after = metrics.WATCH_DECAY_SKIPPED._value.get()
        assert after == before + 1


class TestMetricRegistration:
    """Verify every exported metric object is the expected Prometheus type."""

    def test_all_metrics_exported(self) -> None:
        from prometheus_client import Counter, Gauge, Histogram

        assert isinstance(metrics.PIPELINE_RUNS, Counter)
        assert isinstance(metrics.MCP_CALL_DURATION, Histogram)
        assert isinstance(metrics.MCP_CIRCUIT_BREAKER_TRIPS, Counter)
        assert isinstance(metrics.PIPELINE_LAST_RUN, Gauge)
        assert isinstance(metrics.GATE_REJECTIONS, Counter)
        assert isinstance(metrics.ALERTS_PER_CYCLE, Histogram)
        assert isinstance(metrics.DISCORD_SENDS, Counter)
        assert isinstance(metrics.DB_INSERTS, Counter)
        assert isinstance(metrics.CHART_GEN_DURATION, Histogram)
        assert isinstance(metrics.REDIS_CIRCUIT_OPEN, Gauge)
        assert isinstance(metrics.WATCH_DECAY_SKIPPED, Counter)
