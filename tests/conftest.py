"""Shared pytest fixtures for trade-alert test suite."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from models import PlaybookAlert, Signal, Snapshot

# ── Model fixtures ──────────────────────────────────────────────


@pytest.fixture()
def sample_signal() -> Signal:
    """A representative Signal for use across tests."""
    return Signal(
        source="technicals",
        type="RSI_oversold",
        score=2.0,
        confidence=0.8,
        detail="RSI at 28 on 15m",
    )


@pytest.fixture()
def sample_snapshot(sample_signal: Signal) -> Snapshot:
    """A representative Snapshot carrying one signal."""
    return Snapshot(
        symbol="AAPL",
        timeframe="15m",
        timestamp=datetime.now(timezone.utc).isoformat(),
        signals=[sample_signal],
    )


@pytest.fixture()
def sample_alert() -> PlaybookAlert:
    """A fully-populated PlaybookAlert for use across tests."""
    return PlaybookAlert(
        symbol="NVDA",
        direction="LONG",
        edge_probability=0.82,
        confidence=0.85,
        timeframe="15m",
        thesis="Multi-source confluence on momentum breakout.",
        entry={"level": 875.0, "stop": 865.0, "target": 900.0},
        timeframe_rationale="15m breakout aligning with 1h structure.",
        sentiment_context="Strong retail + institutional.",
        unusual_activity=["IV spike 2.1x avg"],
        macro_regime="Risk-on. VIX 13.2.",
        sources_agree=5,
    )


@pytest.fixture()
def long_alert_row() -> dict:
    """A dict mimicking a mapped DB row for a LONG alert (outcome_tracker)."""
    return {
        "id": 1,
        "symbol": "AAPL",
        "direction": "LONG",
        "entry_level": 185.0,
        "stop_level": 182.0,
        "target_level": 192.0,
        "fired_at": datetime.now(timezone.utc) - timedelta(hours=1),
        "outcome": None,
    }


@pytest.fixture()
def short_alert_row() -> dict:
    """A dict mimicking a mapped DB row for a SHORT alert."""
    return {
        "id": 2,
        "symbol": "TSLA",
        "direction": "SHORT",
        "entry_level": 250.0,
        "stop_level": 255.0,
        "target_level": 240.0,
        "fired_at": datetime.now(timezone.utc) - timedelta(hours=1),
        "outcome": None,
    }


@pytest.fixture()
def expired_alert_row(long_alert_row: dict) -> dict:
    """A LONG alert whose window has already elapsed."""
    return {
        **long_alert_row,
        "fired_at": datetime.now(timezone.utc) - timedelta(hours=5),
    }
