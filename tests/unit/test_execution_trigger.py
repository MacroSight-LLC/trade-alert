"""Unit tests for the ExecutionTriggerV1 schema (execution_trigger.py)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from execution_trigger import AlertClass, ConvictionBand, EntryV1, ExecutionTriggerV1


def _make_trigger(**overrides) -> ExecutionTriggerV1:
    """Build a valid ExecutionTriggerV1 with sensible defaults."""
    defaults: dict = dict(
        event_id="evt-test-001",
        correlation_id="corr-test-001",
        generated_at="2026-04-15T12:00:00+00:00",
        expires_at="2026-04-15T12:15:00+00:00",
        symbol="AAPL",
        direction="LONG",
        alert_class="execute",
        entry=EntryV1(price=185.0, stop=182.0, target=192.0, risk_reward=2.3),
        timeframe="15m",
        strategy_id="cuga-playbook-15m",
        conviction_score=0.70,
        conviction_band="extreme",
        thesis_summary="Breakout with volume.",
    )
    defaults.update(overrides)
    return ExecutionTriggerV1(**defaults)


# ── Version & source defaults ──────────────────────────────────────────────


def test_version_is_v1():
    t = _make_trigger()
    assert t.version == "v1"


def test_source_is_trade_alert():
    t = _make_trigger()
    assert t.source == "trade-alert"


# ── Direction variants ─────────────────────────────────────────────────────


def test_direction_long():
    t = _make_trigger(direction="LONG", alert_class="execute")
    assert t.direction == "LONG"


def test_direction_short():
    t = _make_trigger(direction="SHORT", alert_class="execute")
    assert t.direction == "SHORT"


def test_direction_watch():
    t = _make_trigger(direction="WATCH", alert_class="watch")
    assert t.direction == "WATCH"


def test_invalid_direction_rejected():
    with pytest.raises(ValidationError):
        _make_trigger(direction="BULLISH")


# ── Alert class ────────────────────────────────────────────────────────────


def test_alert_class_execute_accepted():
    t = _make_trigger(alert_class="execute")
    assert t.alert_class == "execute"


def test_alert_class_watch_accepted():
    t = _make_trigger(direction="WATCH", alert_class="watch")
    assert t.alert_class == "watch"


def test_alert_class_info_accepted():
    t = _make_trigger(alert_class="info")
    assert t.alert_class == "info"


def test_invalid_alert_class_rejected():
    with pytest.raises(ValidationError):
        _make_trigger(alert_class="aggressive")


# ── Conviction score bounds ────────────────────────────────────────────────


def test_conviction_score_zero_boundary():
    t = _make_trigger(conviction_score=0.0, conviction_band="low")
    assert t.conviction_score == 0.0


def test_conviction_score_one_boundary():
    t = _make_trigger(conviction_score=1.0, conviction_band="extreme")
    assert t.conviction_score == 1.0


def test_conviction_score_above_one_rejected():
    with pytest.raises(ValidationError):
        _make_trigger(conviction_score=1.1)


def test_conviction_score_below_zero_rejected():
    with pytest.raises(ValidationError):
        _make_trigger(conviction_score=-0.01)


# ── All conviction bands accepted ─────────────────────────────────────────


@pytest.mark.parametrize("band", ["watch", "low", "base", "high", "extreme"])
def test_all_conviction_bands_accepted(band: str):
    t = _make_trigger(conviction_band=band)
    assert t.conviction_band == band


def test_invalid_conviction_band_rejected():
    with pytest.raises(ValidationError):
        _make_trigger(conviction_band="mega")


# ── EntryV1 ────────────────────────────────────────────────────────────────


def test_entry_v1_fields_stored():
    e = EntryV1(price=150.0, stop=145.0, target=165.0, risk_reward=3.0)
    assert e.price == 150.0
    assert e.stop == 145.0
    assert e.target == 165.0
    assert e.risk_reward == 3.0


def test_entry_v1_zero_risk_reward_accepted():
    e = EntryV1(price=100.0, stop=100.0, target=110.0, risk_reward=0.0)
    assert e.risk_reward == 0.0


# ── Metadata ───────────────────────────────────────────────────────────────


def test_metadata_defaults_to_empty_dict():
    t = _make_trigger()
    assert isinstance(t.metadata, dict)
    assert t.metadata == {}


def test_metadata_populated_and_accessible():
    t = _make_trigger(metadata={"sources_agree": 5, "macro_regime": "risk-on"})
    assert t.metadata["sources_agree"] == 5
    assert t.metadata["macro_regime"] == "risk-on"
