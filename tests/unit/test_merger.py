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
    multi_source: bool = True,
) -> Snapshot:
    signals = [
        Signal(
            source=source,
            type=sig_type,
            score=score,
            confidence=confidence,
            reason="test",
        )
    ]
    if multi_source:
        signals.extend(
            [
                Signal(source="polygon", type="volume_spike", score=1.2, confidence=0.75, reason="test"),
                Signal(source="finnhub", type="sentiment_bull", score=1.0, confidence=0.7, reason="test"),
            ]
        )
    return Snapshot(
        symbol=symbol,
        timeframe=timeframe,
        timestamp="2026-03-07T00:00:00Z",
        signals=signals,
    )


def _mock_redis_conn() -> MagicMock:
    """Return a MagicMock that acts as a Redis connection."""
    return MagicMock()


class TestMerge:
    """Tests for merger.merge function."""

    @patch("merger._get_redis")
    def test_empty_queue(self, mock_get_redis: MagicMock) -> None:
        mock_conn = _mock_redis_conn()
        mock_get_redis.return_value = mock_conn
        mock_conn.lrange.return_value = []

        from merger import merge

        result = merge("15m", limit=20)
        assert result == []

    @patch("merger._get_redis")
    def test_single_snapshot(self, mock_get_redis: MagicMock) -> None:
        snap = _make_snapshot()
        mock_conn = _mock_redis_conn()
        mock_get_redis.return_value = mock_conn
        mock_conn.lrange.return_value = [snap.model_dump_json()]

        from merger import merge

        result = merge("15m", limit=20)
        assert len(result) == 1
        assert result[0].symbol == "AAPL"

    @patch("merger._get_redis")
    def test_dedup_same_source_type(self, mock_get_redis: MagicMock) -> None:
        """Same (source, type) → keep highest abs(score)."""
        snap1 = _make_snapshot(score=1.0)
        snap2 = _make_snapshot(score=2.5)
        mock_conn = _mock_redis_conn()
        mock_get_redis.return_value = mock_conn
        mock_conn.lrange.return_value = [
            snap1.model_dump_json(),
            snap2.model_dump_json(),
        ]

        from merger import merge

        result = merge("15m", limit=20)
        assert len(result) == 1
        # 3 distinct (source, type) combos after dedup: tradingview/technical_trend
        # keeps highest score (2.5), plus polygon/volume_spike and finnhub/sentiment_bull
        assert len(result[0].signals) == 3
        ta_signals = [s for s in result[0].signals if s.type == "technical_trend"]
        assert len(ta_signals) == 1
        assert ta_signals[0].score == 2.5

    @patch("merger._get_redis")
    def test_different_sources_kept(self, mock_get_redis: MagicMock) -> None:
        """Different sources → both signals kept."""
        snap1 = _make_snapshot(source="tradingview", score=1.5)
        snap2 = _make_snapshot(source="rot", sig_type="options_flow", score=2.0)
        mock_conn = _mock_redis_conn()
        mock_get_redis.return_value = mock_conn
        mock_conn.lrange.return_value = [
            snap1.model_dump_json(),
            snap2.model_dump_json(),
        ]

        from merger import merge

        result = merge("15m", limit=20)
        assert len(result) == 1
        # 4 distinct (source, type) combos: tradingview/technical_trend,
        # polygon/volume_spike, finnhub/sentiment_bull, rot/options_flow
        assert len(result[0].signals) == 4

    @patch("merger._get_redis")
    def test_limit_respected(self, mock_get_redis: MagicMock) -> None:
        snaps = []
        for i in range(5):
            snaps.append(_make_snapshot(symbol=f"SYM{i}", score=min(float(i + 1), 3.0)).model_dump_json())
        mock_conn = _mock_redis_conn()
        mock_get_redis.return_value = mock_conn
        mock_conn.lrange.return_value = snaps

        from merger import merge

        result = merge("15m", limit=3)
        assert len(result) == 3

    @patch("merger._get_redis")
    def test_sorted_by_aggregate_strength(self, mock_get_redis: MagicMock) -> None:
        weak = _make_snapshot(symbol="WEAK", score=0.5, confidence=0.3)
        strong = _make_snapshot(symbol="STRONG", score=3.0, confidence=0.9)
        mock_conn = _mock_redis_conn()
        mock_get_redis.return_value = mock_conn
        mock_conn.lrange.return_value = [
            weak.model_dump_json(),
            strong.model_dump_json(),
        ]

        from merger import merge

        result = merge("15m", limit=20)
        assert result[0].symbol == "STRONG"
        assert result[1].symbol == "WEAK"

    @patch("merger._get_redis")
    def test_redis_error_returns_empty(self, mock_get_redis: MagicMock) -> None:
        mock_get_redis.side_effect = redis_lib.RedisError("down")

        from merger import merge

        result = merge("15m")
        assert result == []

    @patch("merger._get_redis")
    def test_malformed_entry_skipped(self, mock_get_redis: MagicMock) -> None:
        good = _make_snapshot()
        mock_conn = _mock_redis_conn()
        mock_get_redis.return_value = mock_conn
        mock_conn.lrange.return_value = [
            "NOT VALID JSON{{{",
            good.model_dump_json(),
        ]

        from merger import merge

        result = merge("15m", limit=20)
        assert len(result) == 1


class TestGetMacroRegime:
    """Tests for merger.get_macro_regime."""

    @patch("merger._get_redis")
    def test_returns_parsed_json(self, mock_get_redis: MagicMock) -> None:
        mock_conn = _mock_redis_conn()
        mock_get_redis.return_value = mock_conn
        mock_conn.get.return_value = '{"risk_on": false, "vix": 30}'

        from merger import get_macro_regime

        result = get_macro_regime()
        assert result["risk_on"] is False
        assert result["vix"] == 30

    @patch("merger._get_redis")
    def test_missing_key_defaults_risk_on(self, mock_get_redis: MagicMock) -> None:
        mock_conn = _mock_redis_conn()
        mock_get_redis.return_value = mock_conn
        mock_conn.get.return_value = None

        from merger import get_macro_regime

        result = get_macro_regime()
        assert result == {"risk_on": True, "is_stale": True}

    @patch("merger._get_redis")
    def test_redis_error_defaults_risk_on(self, mock_get_redis: MagicMock) -> None:
        mock_get_redis.side_effect = redis_lib.RedisError("down")

        from merger import get_macro_regime

        result = get_macro_regime()
        assert result == {"risk_on": True, "is_stale": True}


class TestSignAwareDedup:
    """Conflicting-sign signals from same (source, type) must both be preserved."""

    @patch("merger._get_redis")
    def test_conflicting_signs_both_preserved(self, mock_get_redis: MagicMock) -> None:
        """Bullish +2.0 and bearish -2.5 from same source+type → both kept."""
        bull = _make_snapshot(source="finnhub", sig_type="sentiment_bull", score=2.0, multi_source=False)
        bear = _make_snapshot(source="finnhub", sig_type="sentiment_bull", score=-2.5, multi_source=False)
        # Need extra signal types so the symbol passes the pre-LLM filter (>= 3 types)
        extra1 = _make_snapshot(source="polygon", sig_type="volume_spike", score=1.5, multi_source=False)
        extra2 = _make_snapshot(
            source="tradingview", sig_type="technical_trend", score=1.0, multi_source=False
        )
        mock_conn = _mock_redis_conn()
        mock_get_redis.return_value = mock_conn
        mock_conn.lrange.return_value = [
            bull.model_dump_json(),
            bear.model_dump_json(),
            extra1.model_dump_json(),
            extra2.model_dump_json(),
        ]

        from merger import merge

        result = merge("15m", limit=20)
        assert len(result) == 1
        scores = sorted(s.score for s in result[0].signals if s.type == "sentiment_bull")
        assert scores == [-2.5, 2.0], f"Expected both signs preserved, got {scores}"

    @patch("merger._get_redis")
    def test_same_sign_deduped_to_strongest(self, mock_get_redis: MagicMock) -> None:
        """Two positive signals from same source+type → keep highest."""
        weak = _make_snapshot(source="tradingview", sig_type="technical_trend", score=1.0, multi_source=False)
        strong = _make_snapshot(
            source="tradingview", sig_type="technical_trend", score=2.5, multi_source=False
        )
        extra1 = _make_snapshot(source="polygon", sig_type="volume_spike", score=1.5, multi_source=False)
        extra2 = _make_snapshot(source="finnhub", sig_type="sentiment_bull", score=1.0, multi_source=False)
        mock_conn = _mock_redis_conn()
        mock_get_redis.return_value = mock_conn
        mock_conn.lrange.return_value = [
            weak.model_dump_json(),
            strong.model_dump_json(),
            extra1.model_dump_json(),
            extra2.model_dump_json(),
        ]

        from merger import merge

        result = merge("15m", limit=20)
        assert len(result) == 1
        trend_signals = [s for s in result[0].signals if s.type == "technical_trend"]
        assert len(trend_signals) == 1
        assert trend_signals[0].score == 2.5
