"""Unit tests for core Pydantic models (SSOT §4)."""

from __future__ import annotations

import pytest

from models import PlaybookAlert, Signal, Snapshot

# ── Signal ──────────────────────────────────────────────────────


class TestSignal:
    """Tests for Signal model validation."""

    def test_valid_signal(self) -> None:
        s = Signal(
            source="tradingview",
            type="technical_trend",
            score=1.5,
            confidence=0.8,
            reason="BB squeeze",
        )
        assert s.score == 1.5
        assert s.raw == {}

    def test_score_lower_bound(self) -> None:
        s = Signal(
            source="test",
            type="volume_spike",
            score=-3.0,
            confidence=0.5,
            reason="x",
        )
        assert s.score == -3.0

    def test_score_upper_bound(self) -> None:
        s = Signal(
            source="test",
            type="volume_spike",
            score=3.0,
            confidence=1.0,
            reason="x",
        )
        assert s.score == 3.0

    def test_score_too_low(self) -> None:
        with pytest.raises(ValueError, match="score"):
            Signal(
                source="test",
                type="volume_spike",
                score=-3.1,
                confidence=0.5,
                reason="x",
            )

    def test_score_too_high(self) -> None:
        with pytest.raises(ValueError, match="score"):
            Signal(
                source="test",
                type="volume_spike",
                score=3.1,
                confidence=0.5,
                reason="x",
            )

    def test_confidence_too_low(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            Signal(
                source="test",
                type="volume_spike",
                score=0.0,
                confidence=-0.1,
                reason="x",
            )

    def test_confidence_too_high(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            Signal(
                source="test",
                type="volume_spike",
                score=0.0,
                confidence=1.1,
                reason="x",
            )

    def test_invalid_type(self) -> None:
        with pytest.raises(ValueError):
            Signal(
                source="test",
                type="invalid_type",
                score=0.0,
                confidence=0.5,
                reason="x",
            )

    def test_all_signal_types(self) -> None:
        valid_types = [
            "technical_trend",
            "volume_spike",
            "sentiment_bull",
            "sentiment_bear",
            "order_imbalance_long",
            "order_imbalance_short",
            "macro_risk_off",
        ]
        for t in valid_types:
            s = Signal(source="test", type=t, score=1.0, confidence=0.5, reason="x")
            assert s.type == t


# ── Snapshot ────────────────────────────────────────────────────


class TestSnapshot:
    """Tests for Snapshot model."""

    def test_valid_snapshot(self) -> None:
        sig = Signal(
            source="test",
            type="technical_trend",
            score=1.0,
            confidence=0.8,
            reason="x",
        )
        snap = Snapshot(
            symbol="AAPL",
            timeframe="15m",
            timestamp="2026-03-07T00:00:00Z",
            signals=[sig],
        )
        assert snap.symbol == "AAPL"
        assert len(snap.signals) == 1

    def test_valid_timeframes(self) -> None:
        sig = Signal(
            source="test",
            type="volume_spike",
            score=1.0,
            confidence=0.5,
            reason="x",
        )
        for tf in ["5m", "15m", "1h", "4h", "1D"]:
            snap = Snapshot(
                symbol="BTC",
                timeframe=tf,
                timestamp="2026-03-07T00:00:00Z",
                signals=[sig],
            )
            assert snap.timeframe == tf

    def test_invalid_timeframe(self) -> None:
        sig = Signal(
            source="test",
            type="volume_spike",
            score=1.0,
            confidence=0.5,
            reason="x",
        )
        with pytest.raises(ValueError):
            Snapshot(
                symbol="BTC",
                timeframe="2h",
                timestamp="2026-03-07T00:00:00Z",
                signals=[sig],
            )


# ── PlaybookAlert ───────────────────────────────────────────────


class TestPlaybookAlert:
    """Tests for PlaybookAlert model."""

    @pytest.fixture()
    def alert_data(self) -> dict:
        return {
            "symbol": "AAPL",
            "direction": "LONG",
            "edge_probability": 0.78,
            "confidence": 0.85,
            "timeframe": "15m",
            "thesis": "Multi-source confluence",
            "entry": {"level": 185.0, "stop": 182.0, "target": 192.0},
            "timeframe_rationale": "15m breakout",
            "sentiment_context": "Bullish retail + institutional",
            "unusual_activity": ["IV spike 2x"],
            "macro_regime": "Risk-on",
            "sources_agree": 4,
        }

    def test_valid_alert(self, alert_data: dict) -> None:
        alert = PlaybookAlert(**alert_data)
        assert alert.symbol == "AAPL"
        assert alert.direction == "LONG"

    def test_all_directions(self, alert_data: dict) -> None:
        for d in ["LONG", "SHORT", "WATCH"]:
            alert_data["direction"] = d
            alert = PlaybookAlert(**alert_data)
            assert alert.direction == d

    def test_invalid_direction(self, alert_data: dict) -> None:
        alert_data["direction"] = "BUY"
        with pytest.raises(ValueError):
            PlaybookAlert(**alert_data)

    def test_entry_keys(self, alert_data: dict) -> None:
        alert = PlaybookAlert(**alert_data)
        assert "level" in alert.entry
        assert "stop" in alert.entry
        assert "target" in alert.entry

    def test_empty_unusual_activity(self, alert_data: dict) -> None:
        alert_data["unusual_activity"] = []
        alert = PlaybookAlert(**alert_data)
        assert alert.unusual_activity == []

    def test_serialization_roundtrip(self, alert_data: dict) -> None:
        alert = PlaybookAlert(**alert_data)
        json_str = alert.model_dump_json()
        restored = PlaybookAlert.model_validate_json(json_str)
        assert restored == alert
