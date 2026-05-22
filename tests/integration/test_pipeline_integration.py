"""Integration tests for the full validate-and-filter pipeline path.

Covers the server-side gate cascade from LLM JSON to filtered alerts.
All external dependencies (Redis, Postgres, LLM) are mocked.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from models import PlaybookAlert

try:
    import redis as _redis_check  # noqa: F401

    _HAS_REDIS = True
except ImportError:
    _HAS_REDIS = False

# ── Helpers ────────────────────────────────────────────────────────


def _make_alert_dict(**overrides: object) -> dict:
    """Build a valid PlaybookAlert dict with sensible defaults."""
    base = {
        "symbol": "AAPL",
        "direction": "LONG",
        "edge_probability": 0.80,
        "confidence": 0.85,
        "timeframe": "15m",
        "thesis": "Bollinger squeeze with 2.8x volume and options sweep.",
        "entry": {"level": 185.0, "stop": 182.0, "target": 195.0},
        "timeframe_rationale": "15m breakout with 1h confirmation.",
        "sentiment_context": "Retail bullish, institutional neutral.",
        "unusual_activity": ["IV spike 2x avg"],
        "macro_regime": "Risk-on, VIX 14.",
        "sources_agree": 4,
    }
    base.update(overrides)
    return base


def _make_snapshot(symbol: str, signal_types: list[str]) -> dict:
    """Build a snapshot dict with the given signal types."""
    return {
        "symbol": symbol,
        "timeframe": "15m",
        "timestamp": "2026-03-12T14:00:00Z",
        "signals": [
            {
                "source": "test",
                "type": st,
                "score": 1.5,
                "confidence": 0.8,
                "reason": f"Test {st}",
            }
            for st in signal_types
        ],
    }


def _run_filter(
    alerts: list[dict],
    snapshots: list[dict] | None = None,
    vix: float = 14.0,
    macro: dict | None = None,
    timeframe: str = "15m",
) -> tuple[list[PlaybookAlert], str]:
    """Run validate_and_filter with sensible defaults."""
    from validate_and_filter import validate_and_filter

    if snapshots is None:
        snapshots = [
            _make_snapshot(
                "AAPL",
                [
                    "technical_trend",
                    "volume_spike",
                    "sentiment_bull",
                    "options_flow",
                ],
            ),
        ]
    if macro is None:
        macro = {"risk_on": True}

    return validate_and_filter(
        llm_response=json.dumps(alerts),
        snapshots_json=json.dumps(snapshots),
        macro=macro,
        vix=vix,
        timeframe=timeframe,
    )


# ── Test Cases ─────────────────────────────────────────────────────


class TestEpCeiling:
    """Group 2a: EP ceiling by actual source count."""

    def test_ep_ceiling_applied(self) -> None:
        """Signal with 3 sources + EP=0.90 → EP capped to 0.75."""
        snapshots_3 = [
            _make_snapshot(
                "AAPL",
                [
                    "technical_trend",
                    "volume_spike",
                    "sentiment_bull",
                ],
            ),
        ]
        alert_3 = _make_alert_dict(
            edge_probability=0.90,
            sources_agree=3,
        )
        results, _ = _run_filter([alert_3], snapshots=snapshots_3)
        assert len(results) == 1
        # EP ceiling for 3 sources = 0.75
        assert results[0].edge_probability == 0.75


class TestVixHardGate:
    """Group 2b: VIX > 30 universal hard gate."""

    def test_vix_hard_gate_rejects(self) -> None:
        """VIX=32, LONG signal → rejected."""
        alert = _make_alert_dict(direction="LONG")
        results, _ = _run_filter([alert], vix=32.0)
        assert len(results) == 0

    def test_vix_gate_skips_if_no_data(self) -> None:
        """VIX key missing (vix=0.0) → alert passes (gate skipped)."""
        alert = _make_alert_dict()
        results, _ = _run_filter([alert], vix=0.0)
        assert len(results) == 1

    def test_vix_hard_gate_allows_watch(self) -> None:
        """VIX=32, WATCH signal → allowed through."""
        alert = _make_alert_dict(
            direction="WATCH",
            entry={"level": 185.0, "stop": 185.0, "target": 185.0},
        )
        results, _ = _run_filter([alert], vix=32.0)
        assert len(results) == 1


class TestSourceHallucination:
    """Group 2c: Source-hallucination detection."""

    def test_source_hallucination_hard_reject(self) -> None:
        """LLM claims 5 sources, snapshot has 2 → delta=3 → rejected."""
        snapshots = [
            _make_snapshot("AAPL", ["technical_trend", "volume_spike"]),
        ]
        alert = _make_alert_dict(sources_agree=5)
        results, _ = _run_filter([alert], snapshots=snapshots)
        assert len(results) == 0

    def test_source_mismatch_downgrade(self) -> None:
        """LLM claims 4 sources, snapshot has 3 → downgraded, not rejected."""
        snapshots = [
            _make_snapshot(
                "AAPL",
                [
                    "technical_trend",
                    "volume_spike",
                    "sentiment_bull",
                ],
            ),
        ]
        alert = _make_alert_dict(sources_agree=4, edge_probability=0.75)
        results, _ = _run_filter([alert], snapshots=snapshots)
        assert len(results) == 1
        # sources_agree should be overridden to the actual count (3)
        assert results[0].sources_agree == 3


@pytest.mark.skipif(not _HAS_REDIS, reason="redis not installed")
class TestAtomicDedup:
    """Group 1a: Atomic dedup via SET NX."""

    def test_atomic_dedup(self) -> None:
        """Two concurrent calls with same dedup key → only one fires."""
        from notifier_and_logger import _is_duplicate_alert

        mock_redis = MagicMock()

        # First call: SET NX succeeds (returns True) → not a duplicate
        # Second call: SET NX fails (returns None) → is a duplicate
        # get() returns same thesis → high similarity → suppressed as dupe
        mock_redis.set.side_effect = [True, None, None]
        mock_redis.get.return_value = "some thesis"

        with patch("notifier_and_logger.get_redis", return_value=mock_redis):
            first = _is_duplicate_alert("AAPL", "LONG", "15m", "some thesis")
            assert first is False  # first call succeeds

            second = _is_duplicate_alert("AAPL", "LONG", "15m", "some thesis")
            assert second is True  # second call suppressed (similar thesis)


@pytest.mark.skipif(not _HAS_REDIS, reason="redis not installed")
class TestPostgresBeforeDiscord:
    """Group 1b: Persist-first ordering."""

    def test_postgres_before_discord_ordering(self) -> None:
        """Postgres raises exception → Discord NOT called."""
        from notifier_and_logger import notify

        alert = _make_alert_dict()
        alerts_json = json.dumps([alert])

        with (
            patch("notifier_and_logger._is_duplicate_alert", return_value=False),
            patch("notifier_and_logger.format_embed", return_value={"embeds": [{}]}),
            patch("notifier_and_logger.generate_chart", return_value=None),
            patch(
                "notifier_and_logger.insert_alert",
                side_effect=RuntimeError("DB down"),
            ),
            patch("notifier_and_logger.send_discord_embed") as mock_discord,
        ):
            result = notify(alerts_json)

        # Discord should NOT have been called since Postgres failed
        mock_discord.assert_not_called()
        assert result == 0

    def test_discord_failure_non_fatal(self) -> None:
        """Discord raises exception after insert → no exception, insert preserved."""
        from notifier_and_logger import notify

        alert = _make_alert_dict()
        alerts_json = json.dumps([alert])

        with (
            patch("notifier_and_logger._is_duplicate_alert", return_value=False),
            patch("notifier_and_logger.format_embed", return_value={"embeds": [{}]}),
            patch("notifier_and_logger.generate_chart", return_value=None),
            patch("notifier_and_logger.insert_alert") as mock_insert,
            patch("notifier_and_logger.send_discord_embed", return_value=False),
        ):
            result = notify(alerts_json)

        # Insert was called (alert persisted), but Discord failed → 0 sent
        mock_insert.assert_called_once()
        assert result == 0
