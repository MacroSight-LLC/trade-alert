"""Unit tests for merger.py snapshot merging and deduplication (SSOT §9)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import redis as redis_lib

from models import Signal, Snapshot


def _make_snapshot(
    symbol: str = "AAPL",
    timeframe: str = "15m",
    source: str = "tradingview",
    sig_type: str = "technical_trend",
    score: float = 1.5,
    confidence: float = 0.8,
) -> Snapshot:
    return Snapshot(
        symbol=symbol,
        timeframe=timeframe,
        timestamp="2026-03-07T00:00:00Z",
        signals=[
            Signal(
                source=source,
                type=sig_type,
                score=score,
                confidence=confidence,
                reason="test",
            )
        ],
    )


class TestMerge:
    """Tests for merger.merge function."""

    @patch("merger.redis")
    def test_empty_queue(self, mock_redis: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_redis.from_url.return_value = mock_conn
        mock_conn.lrange.return_value = []

        from merger import merge

        result = merge("15m", limit=20)
        assert result == []

    @patch("merger.redis")
    def test_single_snapshot(self, mock_redis: MagicMock) -> None:
        snap = _make_snapshot()
        mock_conn = MagicMock()
        mock_redis.from_url.return_value = mock_conn
        mock_conn.lrange.return_value = [snap.model_dump_json()]

        from merger import merge

        result = merge("15m", limit=20)
        assert len(result) == 1
        assert result[0].symbol == "AAPL"

    @patch("merger.redis")
    def test_dedup_same_source_type(self, mock_redis: MagicMock) -> None:
        """Same (source, type) → keep highest abs(score)."""
        snap1 = _make_snapshot(score=1.0)
        snap2 = _make_snapshot(score=2.5)
        mock_conn = MagicMock()
        mock_redis.from_url.return_value = mock_conn
        mock_conn.lrange.return_value = [
            snap1.model_dump_json(),
            snap2.model_dump_json(),
        ]

        from merger import merge

        result = merge("15m", limit=20)
        assert len(result) == 1
        assert len(result[0].signals) == 1
        assert result[0].signals[0].score == 2.5

    @patch("merger.redis")
    def test_different_sources_kept(self, mock_redis: MagicMock) -> None:
        """Different sources → both signals kept."""
        snap1 = _make_snapshot(source="tradingview", score=1.5)
        snap2 = _make_snapshot(source="polygon", sig_type="volume_spike", score=2.0)
        mock_conn = MagicMock()
        mock_redis.from_url.return_value = mock_conn
        mock_conn.lrange.return_value = [
            snap1.model_dump_json(),
            snap2.model_dump_json(),
        ]

        from merger import merge

        result = merge("15m", limit=20)
        assert len(result) == 1
        assert len(result[0].signals) == 2

    @patch("merger.redis")
    def test_limit_respected(self, mock_redis: MagicMock) -> None:
        snaps = []
        for i in range(5):
            snaps.append(_make_snapshot(symbol=f"SYM{i}", score=min(float(i + 1), 3.0)).model_dump_json())
        mock_conn = MagicMock()
        mock_redis.from_url.return_value = mock_conn
        mock_conn.lrange.return_value = snaps

        from merger import merge

        result = merge("15m", limit=3)
        assert len(result) == 3

    @patch("merger.redis")
    def test_sorted_by_aggregate_strength(self, mock_redis: MagicMock) -> None:
        weak = _make_snapshot(symbol="WEAK", score=0.5, confidence=0.3)
        strong = _make_snapshot(symbol="STRONG", score=3.0, confidence=0.9)
        mock_conn = MagicMock()
        mock_redis.from_url.return_value = mock_conn
        mock_conn.lrange.return_value = [
            weak.model_dump_json(),
            strong.model_dump_json(),
        ]

        from merger import merge

        result = merge("15m", limit=20)
        assert result[0].symbol == "STRONG"
        assert result[1].symbol == "WEAK"

    @patch("merger.redis.from_url")
    def test_redis_error_returns_empty(self, mock_from_url: MagicMock) -> None:
        mock_from_url.side_effect = redis_lib.RedisError("down")

        from merger import merge

        result = merge("15m")
        assert result == []

    @patch("merger.redis")
    def test_malformed_entry_skipped(self, mock_redis: MagicMock) -> None:
        good = _make_snapshot()
        mock_conn = MagicMock()
        mock_redis.from_url.return_value = mock_conn
        mock_conn.lrange.return_value = [
            "NOT VALID JSON{{{",
            good.model_dump_json(),
        ]

        from merger import merge

        result = merge("15m", limit=20)
        assert len(result) == 1


class TestGetMacroRegime:
    """Tests for merger.get_macro_regime."""

    @patch("merger.redis")
    def test_returns_parsed_json(self, mock_redis: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_redis.from_url.return_value = mock_conn
        mock_conn.get.return_value = '{"risk_on": false, "vix": 30}'

        from merger import get_macro_regime

        result = get_macro_regime()
        assert result["risk_on"] is False
        assert result["vix"] == 30

    @patch("merger.redis")
    def test_missing_key_defaults_risk_on(self, mock_redis: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_redis.from_url.return_value = mock_conn
        mock_conn.get.return_value = None

        from merger import get_macro_regime

        result = get_macro_regime()
        assert result == {"risk_on": True}

    @patch("merger.redis.from_url")
    def test_redis_error_defaults_risk_on(self, mock_from_url: MagicMock) -> None:
        mock_from_url.side_effect = redis_lib.RedisError("down")

        from merger import get_macro_regime

        result = get_macro_regime()
        assert result == {"risk_on": True}
