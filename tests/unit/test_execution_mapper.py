"""Unit tests for the PlaybookAlert → ExecutionTriggerV1 mapper."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from execution_mapper import (
    _alert_class,
    _compute_risk_reward,
    _conviction_band,
    map_to_execution_trigger,
)
from models import PlaybookAlert


@pytest.fixture()
def short_alert() -> PlaybookAlert:
    return PlaybookAlert(
        symbol="TSLA",
        direction="SHORT",
        edge_probability=0.78,
        confidence=0.80,
        timeframe="1h",
        thesis="Bearish divergence on 1h with distribution volume.",
        entry={"level": 250.0, "stop": 256.0, "target": 235.0},
        timeframe_rationale="1h structure break confirmed.",
        sentiment_context="Retail bearish, institutional neutral.",
        unusual_activity=["Put sweep $240 1W"],
        macro_regime="Risk-off. VIX 18.",
        sources_agree=5,
    )


@pytest.fixture()
def watch_alert() -> PlaybookAlert:
    return PlaybookAlert(
        symbol="SPY",
        direction="WATCH",
        edge_probability=0.60,
        confidence=0.65,
        timeframe="1h",
        thesis="Watching for breakout confirmation.",
        entry={"level": 500.0, "stop": 495.0, "target": 510.0},
        timeframe_rationale="1h consolidation.",
        sentiment_context="Neutral.",
        unusual_activity=[],
        macro_regime="Mixed.",
        sources_agree=3,
    )


# ── _compute_risk_reward ───────────────────────────────────────────────────


def test_rr_long_alert(sample_alert: PlaybookAlert):
    # sample_alert: entry=875, stop=865, target=900
    # risk=10, reward=25 → rr=2.5
    rr = _compute_risk_reward(sample_alert)
    assert rr == 2.5


def test_rr_short_alert(short_alert: PlaybookAlert):
    # entry=250, stop=256, target=235
    # risk = stop - level = 6, reward = level - target = 15 → rr=2.5
    rr = _compute_risk_reward(short_alert)
    assert rr == 2.5


def test_rr_watch_is_zero(watch_alert: PlaybookAlert):
    assert _compute_risk_reward(watch_alert) == 0.0


# ── _conviction_band ───────────────────────────────────────────────────────


def test_conviction_band_extreme():
    assert _conviction_band(0.70, "LONG") == "extreme"
    assert _conviction_band(0.81, "LONG") == "extreme"
    assert _conviction_band(1.0, "SHORT") == "extreme"


def test_conviction_band_high():
    assert _conviction_band(0.60, "LONG") == "high"
    assert _conviction_band(0.69, "SHORT") == "high"


def test_conviction_band_base():
    assert _conviction_band(0.50, "LONG") == "base"
    assert _conviction_band(0.59, "SHORT") == "base"


def test_conviction_band_low():
    assert _conviction_band(0.0, "LONG") == "low"
    assert _conviction_band(0.49, "SHORT") == "low"


def test_conviction_band_watch_always_watch():
    assert _conviction_band(0.99, "WATCH") == "watch"
    assert _conviction_band(0.0, "WATCH") == "watch"


# ── _alert_class ───────────────────────────────────────────────────────────


def test_alert_class_long_is_execute():
    assert _alert_class("LONG") == "execute"


def test_alert_class_short_is_execute():
    assert _alert_class("SHORT") == "execute"


def test_alert_class_watch_is_watch():
    assert _alert_class("WATCH") == "watch"


# ── map_to_execution_trigger ───────────────────────────────────────────────


def test_map_version_and_source(sample_alert: PlaybookAlert):
    trigger = map_to_execution_trigger(sample_alert)
    assert trigger.version == "v1"
    assert trigger.source == "trade-alert"


def test_map_full_long_alert_fields(sample_alert: PlaybookAlert):
    trigger = map_to_execution_trigger(sample_alert)
    assert trigger.symbol == "NVDA"
    assert trigger.direction == "LONG"
    assert trigger.alert_class == "execute"
    assert trigger.timeframe == "15m"
    assert trigger.thesis_summary == sample_alert.thesis
    assert trigger.entry.price == 875.0
    assert trigger.entry.stop == 865.0
    assert trigger.entry.target == 900.0


def test_map_strategy_id_from_timeframe(sample_alert: PlaybookAlert):
    trigger = map_to_execution_trigger(sample_alert)
    assert trigger.strategy_id == f"cuga-playbook-{sample_alert.timeframe}"


def test_map_strategy_id_1h_timeframe(short_alert: PlaybookAlert):
    trigger = map_to_execution_trigger(short_alert)
    assert trigger.strategy_id == "cuga-playbook-1h"


def test_map_watch_is_non_executable(watch_alert: PlaybookAlert):
    trigger = map_to_execution_trigger(watch_alert)
    assert trigger.alert_class == "watch"
    assert trigger.conviction_band == "watch"
    assert trigger.direction == "WATCH"


def test_map_conviction_score_is_product(sample_alert: PlaybookAlert):
    trigger = map_to_execution_trigger(sample_alert)
    expected = round(sample_alert.edge_probability * sample_alert.confidence, 4)
    assert trigger.conviction_score == expected


def test_map_correlation_id_is_stable(sample_alert: PlaybookAlert):
    t1 = map_to_execution_trigger(sample_alert)
    t2 = map_to_execution_trigger(sample_alert)
    assert t1.correlation_id == t2.correlation_id


def test_map_event_id_is_unique_per_call(sample_alert: PlaybookAlert):
    t1 = map_to_execution_trigger(sample_alert)
    t2 = map_to_execution_trigger(sample_alert)
    assert t1.event_id != t2.event_id


def test_map_expires_at_after_generated_at(sample_alert: PlaybookAlert):
    trigger = map_to_execution_trigger(sample_alert, expiry_seconds=900)
    gen = datetime.fromisoformat(trigger.generated_at)
    exp = datetime.fromisoformat(trigger.expires_at)
    assert exp > gen


def test_map_custom_expiry_seconds(sample_alert: PlaybookAlert):
    trigger = map_to_execution_trigger(sample_alert, expiry_seconds=300)
    gen = datetime.fromisoformat(trigger.generated_at)
    exp = datetime.fromisoformat(trigger.expires_at)
    delta = (exp - gen).total_seconds()
    assert abs(delta - 300) < 2  # allow up to 2s drift from now()


def test_map_metadata_contains_expected_keys(sample_alert: PlaybookAlert):
    trigger = map_to_execution_trigger(sample_alert)
    for key in (
        "sources_agree",
        "macro_regime",
        "sentiment_context",
        "unusual_activity",
        "timeframe_rationale",
    ):
        assert key in trigger.metadata


def test_map_metadata_sources_agree(sample_alert: PlaybookAlert):
    trigger = map_to_execution_trigger(sample_alert)
    assert trigger.metadata["sources_agree"] == sample_alert.sources_agree


def test_map_short_alert_class_and_rr(short_alert: PlaybookAlert):
    trigger = map_to_execution_trigger(short_alert)
    assert trigger.alert_class == "execute"
    assert trigger.direction == "SHORT"
    assert trigger.entry.risk_reward > 0
