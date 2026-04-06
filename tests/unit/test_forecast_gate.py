"""Unit tests for the FORECAST_CONTRADICTS gate in validate_and_filter."""

from __future__ import annotations

import json

from validate_and_filter import GateRejection, validate_and_filter


def _make_alert(
    symbol: str = "AAPL",
    direction: str = "LONG",
    ep: float = 0.80,
    sa: int = 4,
    conf: float = 0.85,
    entry_level: float = 150.0,
    stop: float = 145.0,
    target: float = 165.0,
) -> dict:
    """Build a minimal PlaybookAlert dict."""
    return {
        "symbol": symbol,
        "direction": direction,
        "edge_probability": ep,
        "confidence": conf,
        "timeframe": "15m",
        "thesis": "Test thesis",
        "entry": {"level": entry_level, "stop": stop, "target": target},
        "timeframe_rationale": "test",
        "sentiment_context": "neutral",
        "unusual_activity": [],
        "macro_regime": "risk-on",
        "sources_agree": sa,
    }


def _make_snapshot(
    symbol: str = "AAPL",
    signal_type: str = "technical_trend",
    score: float = 2.0,
    confidence: float = 0.8,
    source: str = "tradingview",
) -> dict:
    """Build a minimal Snapshot dict."""
    return {
        "symbol": symbol,
        "timeframe": "15m",
        "timestamp": "2026-01-01T00:00:00Z",
        "signals": [
            {
                "source": source,
                "type": signal_type,
                "score": score,
                "confidence": confidence,
                "reason": "test",
            }
        ],
    }


def _run_gate(
    alerts: list[dict],
    snapshots: list[dict],
    vix: float = 15.0,
    macro: dict | None = None,
    timeframe: str = "15m",
) -> tuple[list, str]:
    """Run validate_and_filter and return results."""
    if macro is None:
        macro = {"risk_on": True}
    return validate_and_filter(
        llm_response=json.dumps(alerts),
        snapshots_json=json.dumps(snapshots),
        macro=macro,
        vix=vix,
        timeframe=timeframe,
    )


class TestForecastContradictionGate:
    """Tests for FORECAST_CONTRADICTS gate logic."""

    def test_long_with_bearish_forecast_rejected(self) -> None:
        """LONG alert with strongly bearish forecast should be rejected."""
        alert = _make_alert(direction="LONG", sa=4, ep=0.80)
        snapshots = [
            # 4 signal types to pass SA gate
            _make_snapshot(signal_type="technical_trend", score=2.0),
            _make_snapshot(signal_type="volume_spike", score=1.5, source="polygon"),
            _make_snapshot(signal_type="sentiment_bull", score=1.0, source="finnhub"),
            _make_snapshot(signal_type="options_flow", score=1.8, source="flow"),
            # Bearish forecast contradicts LONG
            _make_snapshot(
                signal_type="price_forecast",
                score=-1.5,
                source="timesfm",
            ),
        ]
        passed, _ = _run_gate([alert], snapshots)
        assert len(passed) == 0

    def test_short_with_bullish_forecast_rejected(self) -> None:
        """SHORT alert with strongly bullish forecast should be rejected."""
        alert = _make_alert(
            direction="SHORT",
            sa=4,
            ep=0.80,
            stop=155.0,
            target=135.0,
        )
        snapshots = [
            _make_snapshot(signal_type="technical_trend", score=-2.0),
            _make_snapshot(signal_type="volume_spike", score=-1.5, source="polygon"),
            _make_snapshot(signal_type="sentiment_bear", score=-1.0, source="finnhub"),
            _make_snapshot(signal_type="options_flow", score=-1.8, source="flow"),
            # Bullish forecast contradicts SHORT
            _make_snapshot(
                signal_type="price_forecast",
                score=1.5,
                source="timesfm",
            ),
        ]
        passed, _ = _run_gate([alert], snapshots)
        assert len(passed) == 0

    def test_long_with_bullish_forecast_passes(self) -> None:
        """LONG alert with bullish forecast should pass the gate."""
        alert = _make_alert(direction="LONG", sa=4, ep=0.80)
        snapshots = [
            _make_snapshot(signal_type="technical_trend", score=2.0),
            _make_snapshot(signal_type="volume_spike", score=1.5, source="polygon"),
            _make_snapshot(signal_type="sentiment_bull", score=1.0, source="finnhub"),
            _make_snapshot(signal_type="options_flow", score=1.8, source="flow"),
            # Bullish forecast agrees with LONG
            _make_snapshot(
                signal_type="price_forecast",
                score=1.5,
                source="timesfm",
            ),
        ]
        passed, _ = _run_gate([alert], snapshots)
        assert len(passed) == 1
        assert passed[0].symbol == "AAPL"

    def test_no_forecast_signal_passes(self) -> None:
        """Alert with no price_forecast signal should pass (non-blocking)."""
        alert = _make_alert(direction="LONG", sa=4, ep=0.80)
        snapshots = [
            _make_snapshot(signal_type="technical_trend", score=2.0),
            _make_snapshot(signal_type="volume_spike", score=1.5, source="polygon"),
            _make_snapshot(signal_type="sentiment_bull", score=1.0, source="finnhub"),
            _make_snapshot(signal_type="options_flow", score=1.8, source="flow"),
        ]
        passed, _ = _run_gate([alert], snapshots)
        assert len(passed) == 1

    def test_high_conviction_bypass(self) -> None:
        """High-conviction alert (SA>=5, EP>=0.85) should bypass forecast contradiction."""
        alert = _make_alert(direction="LONG", sa=5, ep=0.88)
        snapshots = [
            _make_snapshot(signal_type="technical_trend", score=2.0),
            _make_snapshot(signal_type="volume_spike", score=1.5, source="polygon"),
            _make_snapshot(signal_type="sentiment_bull", score=1.0, source="finnhub"),
            _make_snapshot(signal_type="options_flow", score=1.8, source="flow"),
            _make_snapshot(signal_type="catalyst_event", score=2.0, source="edgar"),
            # Bearish forecast, but high conviction should bypass
            _make_snapshot(
                signal_type="price_forecast",
                score=-2.0,
                source="timesfm",
            ),
        ]
        passed, _ = _run_gate([alert], snapshots)
        assert len(passed) == 1

    def test_watch_alert_exempt(self) -> None:
        """WATCH alerts should not be subject to forecast gate."""
        alert = _make_alert(direction="WATCH", sa=4, ep=0.80)
        snapshots = [
            _make_snapshot(signal_type="technical_trend", score=2.0),
            _make_snapshot(signal_type="volume_spike", score=1.5, source="polygon"),
            _make_snapshot(signal_type="sentiment_bull", score=1.0, source="finnhub"),
            _make_snapshot(signal_type="options_flow", score=1.8, source="flow"),
            _make_snapshot(
                signal_type="price_forecast",
                score=-2.0,
                source="timesfm",
            ),
        ]
        passed, _ = _run_gate([alert], snapshots)
        assert len(passed) == 1

    def test_weak_forecast_does_not_trigger(self) -> None:
        """Forecast score within threshold (abs < 0.5) should not trigger rejection."""
        alert = _make_alert(direction="LONG", sa=4, ep=0.80)
        snapshots = [
            _make_snapshot(signal_type="technical_trend", score=2.0),
            _make_snapshot(signal_type="volume_spike", score=1.5, source="polygon"),
            _make_snapshot(signal_type="sentiment_bull", score=1.0, source="finnhub"),
            _make_snapshot(signal_type="options_flow", score=1.8, source="flow"),
            # Weak bearish forecast — within threshold
            _make_snapshot(
                signal_type="price_forecast",
                score=-0.3,
                source="timesfm",
            ),
        ]
        passed, _ = _run_gate([alert], snapshots)
        assert len(passed) == 1

    def test_gate_rejection_enum_value(self) -> None:
        """FORECAST_CONTRADICTS should exist in GateRejection enum."""
        assert GateRejection.FORECAST_CONTRADICTS == "forecast_contradicts"
