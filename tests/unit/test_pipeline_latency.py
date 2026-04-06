"""Latency smoke tests for hot-path pipeline operations.

Ensures Pydantic validation, JSON parsing, and the full
validate_and_filter gate cascade stay within acceptable
wall-clock budgets.
"""

from __future__ import annotations

import json
import time

from models import PlaybookAlert, Snapshot
from validate_and_filter import validate_and_filter


def _make_alert_dict(symbol: str = "AAPL", idx: int = 0) -> dict:
    """Return a minimal valid PlaybookAlert dict."""
    return {
        "symbol": symbol,
        "direction": "LONG",
        "edge_probability": 0.72,
        "confidence": 0.80,
        "timeframe": "15m",
        "thesis": f"Test thesis #{idx}",
        "entry": {"level": 185.0, "stop": 182.0, "target": 192.0},
        "timeframe_rationale": "Breakout alignment.",
        "sentiment_context": "Neutral.",
        "unusual_activity": [],
        "macro_regime": "Risk-on.",
        "sources_agree": 4,
    }


def _make_snapshot_dict(symbol: str = "AAPL") -> dict:
    """Return a minimal valid Snapshot dict with one signal."""
    return {
        "symbol": symbol,
        "timeframe": "15m",
        "timestamp": "2025-01-01T00:00:00Z",
        "signals": [
            {
                "source": "tradingview",
                "type": "technical_trend",
                "score": 2.0,
                "confidence": 0.8,
                "reason": "EMA crossover",
            },
            {
                "source": "polygon",
                "type": "volume_spike",
                "score": 1.5,
                "confidence": 0.7,
                "reason": "2x avg volume",
            },
            {
                "source": "finnhub",
                "type": "sentiment_bull",
                "score": 1.0,
                "confidence": 0.6,
                "reason": "Positive news flow",
            },
            {
                "source": "discord",
                "type": "sentiment_bull",
                "score": 1.2,
                "confidence": 0.65,
                "reason": "Community bullish",
            },
        ],
    }


class TestPlaybookAlertValidationLatency:
    """Pydantic validation of 20 PlaybookAlerts should stay under 100 ms."""

    def test_playbook_alert_validation_20_alerts(self) -> None:
        dicts = [_make_alert_dict("AAPL", i) for i in range(20)]

        start = time.monotonic()
        alerts = [PlaybookAlert(**d) for d in dicts]
        elapsed_ms = (time.monotonic() - start) * 1000

        assert len(alerts) == 20
        assert elapsed_ms < 100, f"Pydantic validation took {elapsed_ms:.1f}ms (budget: 100ms)"


class TestSnapshotJsonParseLatency:
    """JSON parsing + Snapshot validation for 100 symbols should stay under 200 ms."""

    def test_snapshot_json_parse_100_symbols(self) -> None:
        snapshots = [_make_snapshot_dict(f"SYM{i:03d}") for i in range(100)]
        blob = json.dumps(snapshots)

        start = time.monotonic()
        parsed = json.loads(blob)
        validated = [Snapshot(**s) for s in parsed]
        elapsed_ms = (time.monotonic() - start) * 1000

        assert len(validated) == 100
        assert elapsed_ms < 200, f"Snapshot parse took {elapsed_ms:.1f}ms (budget: 200ms)"


class TestValidateAndFilterLatency:
    """Full validate_and_filter on 20 alerts should stay under 500 ms."""

    def test_validate_and_filter_20_alerts(self) -> None:
        alerts = [_make_alert_dict("AAPL", i) for i in range(20)]
        llm_response = json.dumps(alerts)
        snapshots = [_make_snapshot_dict("AAPL")]
        snapshots_json = json.dumps(snapshots)
        macro = {"risk_on": True}
        vix = 15.0

        start = time.monotonic()
        result_alerts, result_json = validate_and_filter(
            llm_response=llm_response,
            snapshots_json=snapshots_json,
            macro=macro,
            vix=vix,
            timeframe="15m",
        )
        elapsed_ms = (time.monotonic() - start) * 1000

        assert isinstance(result_alerts, list)
        assert elapsed_ms < 500, f"validate_and_filter took {elapsed_ms:.1f}ms (budget: 500ms)"
