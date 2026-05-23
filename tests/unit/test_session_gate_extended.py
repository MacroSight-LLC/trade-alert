"""Extended-hours session gate behavior (SSOT §10.2)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import gate_config
import validate_and_filter as vf
from gate_config import EXTENDED_HOURS_CONFIDENCE_PENALTY
from gates.session import _apply_market_session_gate_overlays
from validate_and_filter import validate_and_filter


@pytest.fixture(autouse=True)
def _reset_extended_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate_config, "EXTENDED_HOURS_ALERTS_ENABLED", False)
    monkeypatch.setattr(vf, "EXTENDED_HOURS_ALERTS_ENABLED", False)


def _alert(**overrides: object) -> dict:
    base = {
        "symbol": "AAPL",
        "direction": "LONG",
        "edge_probability": 0.80,
        "confidence": 0.85,
        "timeframe": "15m",
        "thesis": "test",
        "entry": {"level": 100.0, "stop": 95.0, "target": 110.0},
        "timeframe_rationale": "r",
        "sentiment_context": "s",
        "unusual_activity": [],
        "macro_regime": "neutral",
        "sources_agree": 5,
    }
    base.update(overrides)
    return base


def _snap() -> str:
    return json.dumps(
        [
            {
                "symbol": "AAPL",
                "signals": [
                    {"type": "technical_trend", "score": 2.0},
                    {"type": "volume_spike", "score": 1.5},
                    {"type": "sentiment_bull", "score": 1.0},
                    {"type": "relative_strength", "score": 1.2},
                    {"type": "options_flow", "score": 0.8},
                ],
            }
        ]
    )


class TestSessionGateExtended:
    def test_default_pre_market_bumps_thresholds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gate_config, "EXTENDED_HOURS_ALERTS_ENABLED", False)
        monkeypatch.setattr(vf, "_MARKET_HOURS_GATES_ENABLED", True)
        with patch("validate_and_filter._market_session_bucket", return_value="pre"):
            ep, sa, conf, session = _apply_market_session_gate_overlays(0.70, 4, 0.75, "15m")
        assert session == "pre"
        assert ep == pytest.approx(0.73)
        assert sa > 4
        assert conf > 0.75

    def test_extended_hours_applies_confidence_penalty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gate_config, "EXTENDED_HOURS_ALERTS_ENABLED", True)
        monkeypatch.setattr(vf, "EXTENDED_HOURS_ALERTS_ENABLED", True)
        with patch("validate_and_filter._market_session_bucket", return_value="pre"):
            passed, _ = validate_and_filter(
                json.dumps([_alert()]),
                _snap(),
                {"risk_on": True},
                18.0,
                "15m",
            )
        assert passed
        assert passed[0].confidence == pytest.approx(0.85 + EXTENDED_HOURS_CONFIDENCE_PENALTY)

    def test_after_hours_no_threshold_bump(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gate_config, "EXTENDED_HOURS_ALERTS_ENABLED", True)
        with patch("validate_and_filter._market_session_bucket", return_value="after"):
            ep, sa, conf, session = _apply_market_session_gate_overlays(0.70, 4, 0.75, "15m")
        assert session == "after"
        assert ep == 0.70
        assert sa == 4

    def test_closed_still_rejects_directional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vf, "_MARKET_HOURS_GATES_ENABLED", True)
        with patch("validate_and_filter._market_session_bucket", return_value="closed"):
            passed, _ = validate_and_filter(
                json.dumps([_alert(edge_probability=0.90, confidence=0.90, sources_agree=6)]),
                _snap(),
                {"risk_on": True},
                18.0,
                "15m",
            )
        assert not passed
