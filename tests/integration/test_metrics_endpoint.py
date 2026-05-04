"""Integration test for the /metrics Prometheus scrape endpoint.

Verifies that:
  1. The endpoint is reachable and returns 200 with the Prometheus text format.
  2. All counters/histograms defined in metrics.py are registered (their HELP
     lines appear in the output once the modules that own them are imported).
  3. After exercising a metric we observe it in the next scrape.

This is the regression guard requested in the Phase-2 observability work:
without it, counters can silently disappear from the registry on import-order
changes and we only notice when alerts stop firing.
"""

from __future__ import annotations

import pytest

# Skip cleanly in environments without FastAPI deps
fastapi = pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")

# Importing notifier_and_logger / validate_and_filter / chart_gen at module
# load time ensures their metric instances are registered before scrape.
import chart_gen  # noqa: F401
import metrics
import notifier_and_logger  # noqa: F401
import validate_and_filter  # noqa: F401
from dashboard_api import app

EXPECTED_METRIC_NAMES: tuple[str, ...] = (
    "pipeline_runs_total",
    "mcp_call_duration_seconds",
    "mcp_circuit_breaker_trips_total",
    "pipeline_last_run_timestamp",
    "gate_rejections_total",
    "alerts_per_cycle",
    "discord_sends_total",
    "db_inserts_total",
    "chart_gen_duration_seconds",
)


@pytest.fixture()
def client() -> testclient.TestClient:
    """FastAPI TestClient bound to the dashboard app."""
    return testclient.TestClient(app)


def test_metrics_endpoint_returns_prometheus_text(client: testclient.TestClient) -> None:
    """The /metrics endpoint serves the Prometheus text exposition format."""
    resp = client.get("/metrics")
    assert resp.status_code == 200
    ctype = resp.headers.get("content-type", "")
    assert ctype.startswith("text/plain"), f"unexpected content-type: {ctype}"
    assert resp.text.startswith("# HELP") or "# HELP" in resp.text


def test_all_expected_metrics_registered(client: testclient.TestClient) -> None:
    """Every counter/histogram declared in metrics.py is registered."""
    body = client.get("/metrics").text
    missing = [name for name in EXPECTED_METRIC_NAMES if f"# HELP {name}" not in body]
    assert not missing, (
        f"Expected Prometheus metrics missing from /metrics output: {missing}. "
        "Either the owning module was not imported or the metric was renamed."
    )


def test_gate_rejection_counter_increments(client: testclient.TestClient) -> None:
    """Incrementing GATE_REJECTIONS reflects in the next scrape."""
    metrics.GATE_REJECTIONS.labels(gate="ep_below_min").inc()
    body = client.get("/metrics").text
    assert 'gate_rejections_total{gate="ep_below_min"}' in body


def test_discord_sends_counter_increments(client: testclient.TestClient) -> None:
    """Incrementing DISCORD_SENDS reflects in the next scrape."""
    metrics.DISCORD_SENDS.labels(status="success").inc()
    body = client.get("/metrics").text
    assert 'discord_sends_total{status="success"}' in body


def test_alerts_per_cycle_histogram_observes(client: testclient.TestClient) -> None:
    """Observing ALERTS_PER_CYCLE produces _bucket / _count / _sum series."""
    metrics.ALERTS_PER_CYCLE.labels(timeframe="15m").observe(2)
    body = client.get("/metrics").text
    assert 'alerts_per_cycle_count{timeframe="15m"}' in body
    assert 'alerts_per_cycle_sum{timeframe="15m"}' in body
