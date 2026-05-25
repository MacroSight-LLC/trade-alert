#!/usr/bin/env python3
"""FU-012: Trade-alert pipeline stability harness.

Runs repeated validate_and_filter iterations against synthetic alerts,
measures gate pass rate, latency percentiles, and Redis circuit-breaker trips.
Writes ``stability_results.json`` (or ``TEST_RESULTS_FILE`` env override).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# Match tests/unit/conftest.py — avoid time-of-day market-session gate failures.
os.environ.setdefault("MARKET_HOURS_GATES_ENABLED", "0")

DEFAULT_SYMBOLS = ("AAPL", "NVDA", "MSFT", "GOOGL", "TSLA")
DEFAULT_ITERATIONS = 50
PASS_RATE_MIN = float(os.environ.get("STABILITY_PASS_RATE_MIN", "0.80"))
P99_LATENCY_MAX = float(os.environ.get("STABILITY_P99_LATENCY_MAX", "5.0"))


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


def _alert(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "direction": "LONG",
        "edge_probability": 0.80,
        "confidence": 0.85,
        "timeframe": "15m",
        "thesis": "Stability harness synthetic alert.",
        "entry": {"level": 185.0, "stop": 182.0, "target": 195.0},
        "timeframe_rationale": "15m test.",
        "sentiment_context": "Neutral.",
        "unusual_activity": [],
        "macro_regime": "Risk-on.",
        "sources_agree": 4,
    }


def _snap(symbol: str) -> dict[str, Any]:
    ts = datetime.now(UTC).isoformat()
    types = ["technical_trend", "volume_spike", "sentiment_bull", "options_flow"]
    return {
        "symbol": symbol,
        "timeframe": "15m",
        "timestamp": ts,
        "signals": [
            {"source": "stability", "type": t, "score": 1.5, "confidence": 0.8, "reason": t} for t in types
        ],
    }


def _mock_redis() -> MagicMock:
    mock = MagicMock()
    mock.hgetall.return_value = {}
    mock.hget.return_value = None
    mock.get.return_value = None
    mock.exists.return_value = 0
    mock.set.return_value = True
    pipe = MagicMock()
    pipe.execute.return_value = [0, 0, 0, 0]
    mock.pipeline.return_value = pipe
    return mock


def run_stability_suite(
    *,
    iterations: int,
    symbols: tuple[str, ...],
) -> dict[str, Any]:
    from validate_and_filter import validate_and_filter

    latencies: list[float] = []
    passes = 0
    circuit_trips_start = 0
    circuit_trips_end = 0

    try:
        from gates.redis_circuit import get_breaker

        circuit_trips_start = get_breaker().failure_count
    except Exception:
        pass

    mock = _mock_redis()
    with patch("validate_and_filter.get_redis", return_value=mock):
        for i in range(iterations):
            symbol = symbols[i % len(symbols)]
            start = time.monotonic()
            passing, _ = validate_and_filter(
                llm_response=json.dumps([_alert(symbol)]),
                snapshots_json=json.dumps([_snap(symbol)]),
                macro={"risk_on": True},
                vix=14.0,
                timeframe="15m",
            )
            latencies.append(time.monotonic() - start)
            if passing:
                passes += 1

    try:
        from gates.redis_circuit import get_breaker

        circuit_trips_end = get_breaker().failure_count
    except Exception:
        pass

    pass_rate = (passes / iterations) * 100.0 if iterations else 0.0
    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)
    p99 = _percentile(latencies, 99)

    return {
        "iterations": iterations,
        "symbols": list(symbols),
        "pass_count": passes,
        "pass_rate": round(pass_rate, 2),
        "latency_seconds": {
            "p50": round(p50, 4),
            "p95": round(p95, 4),
            "p99": round(p99, 4),
            "mean": round(statistics.mean(latencies) if latencies else 0.0, 4),
        },
        "circuit_breaker_trips": max(0, circuit_trips_end - circuit_trips_start),
        "thresholds": {
            "pass_rate_min_pct": PASS_RATE_MIN * 100,
            "p99_latency_max_seconds": P99_LATENCY_MAX,
        },
        "passed": pass_rate >= PASS_RATE_MIN * 100 and p99 <= P99_LATENCY_MAX,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trade-alert stability harness (FU-012)")
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--output", type=Path, default=Path("stability_results.json"))
    parser.add_argument("--method", choices=("local",), default="local")
    args = parser.parse_args(argv)

    if args.method != "local":
        print(f"Unsupported method: {args.method}", file=sys.stderr)
        return 2

    results = run_stability_suite(iterations=args.iterations, symbols=tuple(args.symbols))
    output = Path(os.environ.get("TEST_RESULTS_FILE", str(args.output)))
    output.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(json.dumps(results, indent=2))
    if not results["passed"]:
        print(
            f"FAIL: pass_rate={results['pass_rate']}% (min {PASS_RATE_MIN * 100}%), "
            f"p99={results['latency_seconds']['p99']}s (max {P99_LATENCY_MAX}s)",
            file=sys.stderr,
        )
        return 1
    print("PASS: stability thresholds met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
