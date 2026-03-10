"""Unit tests for all 5 normalizers (SSOT §7)."""

from __future__ import annotations

from normalizers.flow_normalizer import normalize as flow_normalize
from normalizers.macro_normalizer import normalize as macro_normalize
from normalizers.market_normalizer import normalize as market_normalize
from normalizers.sentiment_normalizer import normalize as sentiment_normalize
from normalizers.ta_normalizer import normalize as ta_normalize

# ── TA Normalizer ───────────────────────────────────────────────


class TestTaNormalizer:
    """Tests for ta_normalizer.normalize."""

    def test_basic_signal(self) -> None:
        raw = {"AAPL": {"rating": 2.0, "patterns": [], "indicators": {}}}
        result = ta_normalize(raw, timeframe="15m")
        assert len(result) == 1
        assert result[0].symbol == "AAPL"
        assert result[0].signals[0].type == "technical_trend"
        assert result[0].signals[0].score == 2.0

    def test_none_rating_skipped(self) -> None:
        raw = {"BAD": {"rating": None, "patterns": [], "indicators": {}}}
        result = ta_normalize(raw, timeframe="15m")
        assert len(result) == 0

    def test_score_clamped(self) -> None:
        raw = {"X": {"rating": 5.0, "patterns": [], "indicators": {}}}
        result = ta_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score == 3.0

    def test_negative_score_clamped(self) -> None:
        raw = {"X": {"rating": -5.0, "patterns": [], "indicators": {}}}
        result = ta_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score == -3.0

    def test_bb_squeeze_reason(self) -> None:
        raw = {"X": {"rating": 1.0, "patterns": [], "indicators": {"bb_squeeze": True}}}
        result = ta_normalize(raw, timeframe="15m")
        assert "BB squeeze" in result[0].signals[0].reason

    def test_trend_change_reason(self) -> None:
        raw = {"X": {"rating": 1.0, "patterns": ["trend_change"], "indicators": {}}}
        result = ta_normalize(raw, timeframe="15m")
        assert "trend change" in result[0].signals[0].reason

    def test_confidence_bounded(self) -> None:
        raw = {"X": {"rating": 3.0, "patterns": [], "indicators": {}}}
        result = ta_normalize(raw, timeframe="15m")
        assert 0.0 <= result[0].signals[0].confidence <= 1.0

    def test_empty_input(self) -> None:
        assert ta_normalize({}, timeframe="15m") == []

    def test_timeframe_passed_through(self) -> None:
        raw = {"X": {"rating": 1.0, "patterns": [], "indicators": {}}}
        result = ta_normalize(raw, timeframe="1h")
        assert result[0].timeframe == "1h"

    def test_multiple_symbols(self) -> None:
        raw = {
            "AAPL": {"rating": 1.0, "patterns": [], "indicators": {}},
            "TSLA": {"rating": -2.0, "patterns": [], "indicators": {}},
        }
        result = ta_normalize(raw, timeframe="15m")
        assert len(result) == 2
        symbols = {s.symbol for s in result}
        assert symbols == {"AAPL", "TSLA"}


# ── Flow Normalizer ─────────────────────────────────────────────


class TestFlowNormalizer:
    """Tests for flow_normalizer.normalize."""

    def test_volume_spike_low(self) -> None:
        raw = {"X": {"volume_multiple": 2.0}}
        result = flow_normalize(raw, timeframe="15m")
        assert len(result) == 1
        assert result[0].signals[0].type == "volume_spike"
        assert result[0].signals[0].score == 1.0

    def test_volume_spike_medium(self) -> None:
        raw = {"X": {"volume_multiple": 3.5}}
        result = flow_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score == 2.5

    def test_volume_spike_high(self) -> None:
        raw = {"X": {"volume_multiple": 6.0}}
        result = flow_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score == 3.0

    def test_volume_below_threshold(self) -> None:
        raw = {"X": {"volume_multiple": 1.2}}
        result = flow_normalize(raw, timeframe="15m")
        assert len(result) == 0

    def test_boundary_1_5(self) -> None:
        raw = {"X": {"volume_multiple": 1.5}}
        result = flow_normalize(raw, timeframe="15m")
        assert len(result) == 1
        assert result[0].signals[0].score == 1.0

    def test_boundary_3_0(self) -> None:
        raw = {"X": {"volume_multiple": 3.0}}
        result = flow_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score == 2.5

    def test_boundary_5_0(self) -> None:
        raw = {"X": {"volume_multiple": 5.0}}
        result = flow_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score == 3.0

    def test_order_imbalance_long(self) -> None:
        raw = {"BTC": {"volume_multiple": 0.5, "imbalance": 0.8}}
        result = flow_normalize(raw, timeframe="15m")
        assert len(result) == 1
        assert result[0].signals[0].type == "order_imbalance_long"

    def test_order_imbalance_short(self) -> None:
        raw = {"ETH": {"volume_multiple": 0.5, "imbalance": -0.6}}
        result = flow_normalize(raw, timeframe="15m")
        assert len(result) == 1
        assert result[0].signals[0].type == "order_imbalance_short"

    def test_both_volume_and_imbalance(self) -> None:
        raw = {"BTC": {"volume_multiple": 4.0, "imbalance": 0.5}}
        result = flow_normalize(raw, timeframe="15m")
        assert len(result[0].signals) == 2

    def test_zero_imbalance_skipped(self) -> None:
        raw = {"X": {"volume_multiple": 0.5, "imbalance": 0.0}}
        result = flow_normalize(raw, timeframe="15m")
        assert len(result) == 0

    def test_unusual_options_in_reason(self) -> None:
        raw = {"X": {"volume_multiple": 2.0, "unusual_options": ["$190c sweep"]}}
        result = flow_normalize(raw, timeframe="15m")
        assert "$190c sweep" in result[0].signals[0].reason

    def test_empty_input(self) -> None:
        assert flow_normalize({}, timeframe="15m") == []


# ── Sentiment Normalizer ────────────────────────────────────────


class TestSentimentNormalizer:
    """Tests for sentiment_normalizer.normalize."""

    def test_finnhub_positive(self) -> None:
        raw = {"AAPL": {"finnhub_score": 0.7, "spam_filtered": False}}
        result = sentiment_normalize(raw, timeframe="15m")
        assert len(result) == 1
        assert result[0].signals[0].type == "sentiment_bull"
        assert result[0].signals[0].score > 0

    def test_finnhub_negative(self) -> None:
        raw = {"TSLA": {"finnhub_score": -0.5, "spam_filtered": False}}
        result = sentiment_normalize(raw, timeframe="15m")
        assert result[0].signals[0].type == "sentiment_bear"
        assert result[0].signals[0].score < 0

    def test_finnhub_score_clamped(self) -> None:
        raw = {"X": {"finnhub_score": 1.0, "spam_filtered": False}}
        result = sentiment_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score <= 2.0

    def test_rot_strong_bullish(self) -> None:
        raw = {"X": {"rot_signal": "strong_bullish", "spam_filtered": False}}
        result = sentiment_normalize(raw, timeframe="15m")
        assert result[0].signals[0].type == "sentiment_bull"
        assert result[0].signals[0].score == 2.5

    def test_rot_strong_bearish(self) -> None:
        raw = {"X": {"rot_signal": "strong_bearish", "spam_filtered": False}}
        result = sentiment_normalize(raw, timeframe="15m")
        assert result[0].signals[0].type == "sentiment_bear"
        assert result[0].signals[0].score == -2.5

    def test_spam_filtered_skipped(self) -> None:
        raw = {"X": {"finnhub_score": 0.9, "spam_filtered": True}}
        result = sentiment_normalize(raw, timeframe="15m")
        assert len(result) == 0

    def test_neutral_rot_skipped(self) -> None:
        raw = {"X": {"rot_signal": "neutral", "spam_filtered": False}}
        result = sentiment_normalize(raw, timeframe="15m")
        assert len(result) == 0

    def test_both_finnhub_and_rot(self) -> None:
        raw = {
            "X": {
                "finnhub_score": 0.5,
                "rot_signal": "bullish",
                "spam_filtered": False,
            }
        }
        result = sentiment_normalize(raw, timeframe="15m")
        assert len(result[0].signals) == 2

    def test_empty_input(self) -> None:
        assert sentiment_normalize({}, timeframe="15m") == []


# ── Market Normalizer ───────────────────────────────────────────


class TestMarketNormalizer:
    """Tests for market_normalizer.normalize."""

    def test_large_positive_change(self) -> None:
        raw = {"X": {"price_change_24h": 12.0}}
        result = market_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score == 2.5

    def test_moderate_positive_change(self) -> None:
        raw = {"X": {"price_change_24h": 7.0}}
        result = market_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score == 1.5

    def test_large_negative_change(self) -> None:
        raw = {"X": {"price_change_24h": -12.0}}
        result = market_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score == -2.5

    def test_small_change_no_signal(self) -> None:
        raw = {"X": {"price_change_24h": 2.0}}
        result = market_normalize(raw, timeframe="15m")
        assert len(result) == 0

    def test_insider_buying(self) -> None:
        raw = {"X": {"insider_activity": "buying"}}
        result = market_normalize(raw, timeframe="15m")
        assert result[0].signals[0].type == "sentiment_bull"

    def test_insider_selling(self) -> None:
        raw = {"X": {"insider_activity": "selling"}}
        result = market_normalize(raw, timeframe="15m")
        assert result[0].signals[0].type == "sentiment_bear"

    def test_insider_none_skipped(self) -> None:
        raw = {"X": {"insider_activity": "none"}}
        result = market_normalize(raw, timeframe="15m")
        assert len(result) == 0

    def test_empty_input(self) -> None:
        assert market_normalize({}, timeframe="15m") == []


# ── Macro Normalizer ────────────────────────────────────────────


class TestMacroNormalizer:
    """Tests for macro_normalizer.normalize."""

    def test_extreme_vix(self) -> None:
        raw = {"vix": 40.0, "yield_curve_slope": 50.0, "risk_on": False}
        result = macro_normalize(raw, timeframe="15m")
        assert len(result) == 1
        assert result[0].symbol == "__GLOBAL_MACRO__"
        scores = [s.score for s in result[0].signals]
        assert 3.0 in scores  # VIX extreme

    def test_elevated_vix(self) -> None:
        raw = {"vix": 28.0, "yield_curve_slope": 50.0, "risk_on": True}
        result = macro_normalize(raw, timeframe="15m")
        signals = result[0].signals
        vix_sig = [s for s in signals if "VIX" in s.reason][0]
        assert vix_sig.score == 2.0

    def test_inverted_curve(self) -> None:
        raw = {"vix": 15.0, "yield_curve_slope": -60.0, "risk_on": True}
        result = macro_normalize(raw, timeframe="15m")
        curve_sig = [s for s in result[0].signals if "curve" in s.reason.lower()][0]
        assert curve_sig.score == 1.5

    def test_risk_off_flag(self) -> None:
        raw = {"vix": 15.0, "yield_curve_slope": 50.0, "risk_on": False}
        result = macro_normalize(raw, timeframe="15m")
        assert len(result) == 1
        assert result[0].signals[0].reason == "FRED risk-on flag is False"

    def test_calm_market_no_signals(self) -> None:
        raw = {"vix": 15.0, "yield_curve_slope": 50.0, "risk_on": True}
        result = macro_normalize(raw, timeframe="15m")
        assert len(result) == 0

    def test_empty_input(self) -> None:
        result = macro_normalize({}, timeframe="15m")
        assert len(result) == 0

    def test_timeframe_passed(self) -> None:
        raw = {"vix": 40.0, "risk_on": False}
        result = macro_normalize(raw, timeframe="1h")
        assert result[0].timeframe == "1h"
