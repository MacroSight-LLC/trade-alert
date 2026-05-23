"""Centralized Prometheus metrics for the trade-alert pipeline.

Import the relevant counters / histograms in each module.
The ``/metrics`` endpoint is served by ``dashboard_api.py``.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ── Pipeline Runner ────────────────────────────────────────────────

PIPELINE_RUNS = Counter(
    "pipeline_runs_total",
    "Total pipeline workflow executions",
    ["workflow", "status"],
)

MCP_CALL_DURATION = Histogram(
    "mcp_call_duration_seconds",
    "MCP tool call latency",
    ["tool", "method"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
)

MCP_CIRCUIT_BREAKER_TRIPS = Counter(
    "mcp_circuit_breaker_trips_total",
    "Circuit breaker activations by MCP endpoint",
    ["endpoint"],
)

PIPELINE_LAST_RUN = Gauge(
    "pipeline_last_run_timestamp",
    "Unix timestamp of last pipeline run",
    ["workflow"],
)

# ── Validation Gates ───────────────────────────────────────────────

GATE_REJECTIONS = Counter(
    "gate_rejections_total",
    "Alert rejections by validation gate",
    ["gate"],
)

ALERTS_PER_CYCLE = Histogram(
    "alerts_per_cycle",
    "Number of alerts produced per validation cycle",
    ["timeframe"],
    buckets=(0, 1, 2, 3, 5, 8, 12, 20, 50),
)

# ── Notifier & Logger ──────────────────────────────────────────────

DISCORD_SENDS = Counter(
    "discord_sends_total",
    "Discord alert notifications sent",
    ["status"],
)

DB_INSERTS = Counter(
    "db_inserts_total",
    "Postgres alert inserts",
    ["status"],
)

CHART_GEN_DURATION = Histogram(
    "chart_gen_duration_seconds",
    "Time to generate candlestick chart",
    buckets=(0.1, 0.5, 1, 2, 5, 10),
)

# ── Redis Circuit Breaker (WATCH decay) ────────────────────────────
# Grafana panel: "Redis Circuit Breaker State" — 1=open (WATCH decay disabled), 0=closed

REDIS_CIRCUIT_OPEN = Gauge(
    "trade_alert_redis_circuit_open",
    "1 when Redis circuit breaker is open",
)

WATCH_DECAY_SKIPPED = Counter(
    "trade_alert_watch_decay_skipped_total",
    "WATCH alerts that bypassed decay due to Redis outage",
)
