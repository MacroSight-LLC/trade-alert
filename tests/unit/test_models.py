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
            "options_flow",
            "insider_activity",
            "relative_strength",
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
        # LONG: stop < level < target; SHORT: target < level < stop; WATCH: skips ordering.
        cases: dict[str, dict[str, float]] = {
            "LONG": {"level": 185.0, "stop": 182.0, "target": 192.0},
            "SHORT": {"level": 185.0, "stop": 192.0, "target": 178.0},
            "WATCH": {"level": 185.0, "stop": 182.0, "target": 192.0},
        }
        for d, entry in cases.items():
            alert_data["direction"] = d
            alert_data["entry"] = entry
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

    # ── Parametrized edge cases (Item 71) ──

    @pytest.mark.parametrize(
        "ep",
        [0.0, 0.5, 1.0],
        ids=["ep_zero", "ep_mid", "ep_max"],
    )
    def test_edge_probability_valid_bounds(self, alert_data: dict, ep: float) -> None:
        alert_data["edge_probability"] = ep
        alert = PlaybookAlert(**alert_data)
        assert alert.edge_probability == ep

    @pytest.mark.parametrize("ep", [-0.01, 1.01, 2.0])
    def test_edge_probability_out_of_range(self, alert_data: dict, ep: float) -> None:
        alert_data["edge_probability"] = ep
        with pytest.raises(ValueError, match="edge_probability"):
            PlaybookAlert(**alert_data)

    @pytest.mark.parametrize(
        "conf",
        [0.0, 0.5, 1.0],
        ids=["conf_zero", "conf_mid", "conf_max"],
    )
    def test_confidence_valid_bounds(self, alert_data: dict, conf: float) -> None:
        # Drop edge_probability below the proportional-check threshold so this
        # test exercises only the confidence field validator and doesn't trip
        # the cross-field validate_edge_vs_confidence rule.
        alert_data["edge_probability"] = 0.0
        alert_data["confidence"] = conf
        alert = PlaybookAlert(**alert_data)
        assert alert.confidence == conf

    @pytest.mark.parametrize("conf", [-0.1, 1.1])
    def test_confidence_out_of_range(self, alert_data: dict, conf: float) -> None:
        alert_data["confidence"] = conf
        with pytest.raises(ValueError, match="confidence"):
            PlaybookAlert(**alert_data)

    @pytest.mark.parametrize("sa", [0, 1, 5, 10])
    def test_sources_agree_valid(self, alert_data: dict, sa: int) -> None:
        alert_data["sources_agree"] = sa
        alert = PlaybookAlert(**alert_data)
        assert alert.sources_agree == sa

    def test_sources_agree_negative(self, alert_data: dict) -> None:
        alert_data["sources_agree"] = -1
        with pytest.raises(ValueError, match="sources_agree"):
            PlaybookAlert(**alert_data)

    def test_empty_string_optional_fields(self, alert_data: dict) -> None:
        alert_data["sentiment_context"] = ""
        alert_data["macro_regime"] = ""
        alert_data["timeframe_rationale"] = ""
        alert = PlaybookAlert(**alert_data)
        assert alert.sentiment_context == ""

    # ── validate_edge_vs_confidence (hard + proportional) ──

    def test_edge_vs_confidence_hard_rule_rejects(self, alert_data: dict) -> None:
        """ep > 0.85 with conf < 0.15 must raise."""
        alert_data["edge_probability"] = 0.90
        alert_data["confidence"] = 0.10
        with pytest.raises(ValueError, match="logically inconsistent"):
            PlaybookAlert(**alert_data)

    @pytest.mark.parametrize(
        ("ep", "conf"),
        [
            (0.70, 0.10),  # floor = 0.15 — hard rule does not apply (ep <= 0.85)
            (0.80, 0.05),  # floor = 0.10 — hard rule does not apply
            (0.85, 0.05),  # floor = 0.075 — hard rule does not apply at boundary
        ],
    )
    def test_edge_vs_confidence_proportional_rule_rejects(
        self, alert_data: dict, ep: float, conf: float
    ) -> None:
        """ep in [0.70, 0.85] with conf below proportional floor must raise.

        Above ep=0.85 the hard rule (ep > 0.85 AND conf < 0.15) subsumes the
        proportional rule, so this test focuses on the band where only the
        proportional check applies.
        """
        alert_data["edge_probability"] = ep
        alert_data["confidence"] = conf
        with pytest.raises(ValueError, match="proportional consistency"):
            PlaybookAlert(**alert_data)

    @pytest.mark.parametrize(
        ("ep", "conf"),
        [
            (0.69, 0.05),  # below ep threshold — proportional rule disabled
            (0.70, 0.15),  # exactly at floor for ep=0.70
            (0.80, 0.10),  # exactly at floor for ep=0.80
            (0.95, 0.50),  # well above floor
        ],
    )
    def test_edge_vs_confidence_accepts(
        self, alert_data: dict, ep: float, conf: float
    ) -> None:
        """ep/conf combos at or above the proportional floor must pass."""
        alert_data["edge_probability"] = ep
        alert_data["confidence"] = conf
        alert = PlaybookAlert(**alert_data)
        assert alert.edge_probability == ep
        assert alert.confidence == conf


class TestSignalEdgeCases:
    """Parametrized edge cases for Signal model."""

    @pytest.mark.parametrize("score", [-3.0, -1.5, 0.0, 1.5, 3.0])
    def test_valid_score_range(self, score: float) -> None:
        s = Signal(
            source="test",
            type="technical_trend",
            score=score,
            confidence=0.5,
            reason="x",
        )
        assert s.score == score

    @pytest.mark.parametrize("conf", [0.0, 0.5, 1.0])
    def test_valid_confidence_range(self, conf: float) -> None:
        s = Signal(
            source="test",
            type="technical_trend",
            score=0.0,
            confidence=conf,
            reason="x",
        )
        assert s.confidence == conf

    def test_empty_reason(self) -> None:
        s = Signal(
            source="test",
            type="technical_trend",
            score=0.0,
            confidence=0.5,
            reason="",
        )
        assert s.reason == ""

    def test_empty_raw(self) -> None:
        s = Signal(
            source="test",
            type="technical_trend",
            score=0.0,
            confidence=0.5,
            reason="x",
        )
        assert s.raw == {}
