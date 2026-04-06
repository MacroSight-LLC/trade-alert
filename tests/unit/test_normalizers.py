"""Unit tests for all 7 normalizers (SSOT §7)."""

from __future__ import annotations

import pytest

from normalizers import safe_float
from normalizers.events_normalizer import normalize as events_normalize
from normalizers.flow_normalizer import normalize as flow_normalize
from normalizers.macro_normalizer import normalize as macro_normalize
from normalizers.market_normalizer import normalize as market_normalize
from normalizers.sentiment_normalizer import normalize as sentiment_normalize
from normalizers.si_normalizer import normalize as si_normalize
from normalizers.ta_normalizer import normalize as ta_normalize

# ── safe_float utility ──────────────────────────────────────────


class TestSafeFloat:
    """Tests for normalizers.safe_float NaN/Inf guard."""

    def test_normal_value(self) -> None:
        assert safe_float(1.5) == 1.5

    def test_nan_returns_default(self) -> None:
        assert safe_float(float("nan")) == 0.0

    def test_inf_returns_default(self) -> None:
        assert safe_float(float("inf")) == 0.0

    def test_neg_inf_returns_default(self) -> None:
        assert safe_float(float("-inf")) == 0.0

    def test_none_returns_default(self) -> None:
        assert safe_float(None) == 0.0

    def test_custom_default(self) -> None:
        assert safe_float(float("nan"), default=-1.0) == -1.0

    def test_zero_preserved(self) -> None:
        assert safe_float(0.0) == 0.0

    def test_int_value(self) -> None:
        assert safe_float(3) == 3.0


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

    def test_nan_rating_skipped(self) -> None:
        raw = {"X": {"rating": float("nan"), "patterns": [], "indicators": {}}}
        result = ta_normalize(raw, timeframe="15m")
        assert len(result) == 0

    def test_inf_rating_skipped(self) -> None:
        raw = {"X": {"rating": float("inf"), "patterns": [], "indicators": {}}}
        result = ta_normalize(raw, timeframe="15m")
        assert len(result) == 0


# ── Flow Normalizer ─────────────────────────────────────────────


class TestFlowNormalizer:
    """Tests for flow_normalizer.normalize."""

    def test_volume_spike_low(self) -> None:
        raw = {"X": {"volume_multiple": 2.0}}
        result = flow_normalize(raw, timeframe="15m")
        assert len(result) == 1
        assert result[0].signals[0].type == "volume_spike"
        assert result[0].signals[0].score == pytest.approx(1.333, abs=0.01)

    def test_volume_spike_medium(self) -> None:
        raw = {"X": {"volume_multiple": 3.5}}
        result = flow_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score == pytest.approx(2.25, abs=0.01)

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
        assert result[0].signals[0].score == pytest.approx(2.0, abs=0.01)

    def test_boundary_5_0(self) -> None:
        raw = {"X": {"volume_multiple": 5.0}}
        result = flow_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score == 3.0

    def test_unusual_options_in_reason(self) -> None:
        raw = {"X": {"volume_multiple": 2.0, "unusual_options": ["$190c sweep"]}}
        result = flow_normalize(raw, timeframe="15m")
        assert "$190c sweep" in result[0].signals[0].reason

    def test_empty_input(self) -> None:
        assert flow_normalize({}, timeframe="15m") == []

    def test_nan_volume_no_spike(self) -> None:
        raw = {"X": {"volume_multiple": float("nan")}}
        result = flow_normalize(raw, timeframe="15m")
        assert len(result) == 0


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

    def test_nan_finnhub_score_treated_as_zero(self) -> None:
        raw = {"X": {"finnhub_score": float("nan"), "spam_filtered": False}}
        result = sentiment_normalize(raw, timeframe="15m")
        # safe_float converts NaN → 0.0, then 0.0 is filtered (no signal value)
        assert len(result) == 0


# ── Market Normalizer ───────────────────────────────────────────


class TestMarketNormalizer:
    """Tests for market_normalizer.normalize."""

    def test_large_positive_change(self) -> None:
        raw = {"X": {"price_change_24h": 12.0}}
        result = market_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score == pytest.approx(2.8, abs=0.01)

    def test_moderate_positive_change(self) -> None:
        raw = {"X": {"price_change_24h": 7.0}}
        result = market_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score == pytest.approx(2.32, abs=0.01)

    def test_large_negative_change(self) -> None:
        raw = {"X": {"price_change_24h": -12.0}}
        result = market_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score == pytest.approx(-2.8, abs=0.01)

    def test_small_change_no_signal(self) -> None:
        raw = {"X": {"price_change_24h": 1.0}}
        result = market_normalize(raw, timeframe="15m")
        assert len(result) == 0

    def test_insider_buying(self) -> None:
        raw = {"X": {"insider_activity": "buying"}}
        result = market_normalize(raw, timeframe="15m")
        assert result[0].signals[0].type == "insider_activity"

    def test_insider_selling(self) -> None:
        raw = {"X": {"insider_activity": "selling"}}
        result = market_normalize(raw, timeframe="15m")
        assert result[0].signals[0].type == "insider_activity"

    def test_insider_none_skipped(self) -> None:
        raw = {"X": {"insider_activity": "none"}}
        result = market_normalize(raw, timeframe="15m")
        assert len(result) == 0

    def test_relative_strength_bullish(self) -> None:
        raw = {"AAPL": {"price_change_24h": 5.0, "spy_pct_change": 1.0}}
        result = market_normalize(raw, timeframe="15m")
        rs_sigs = [s for snap in result for s in snap.signals if s.type == "relative_strength"]
        assert len(rs_sigs) == 1
        assert rs_sigs[0].score > 0

    def test_relative_strength_bearish(self) -> None:
        raw = {"AAPL": {"price_change_24h": -1.0, "spy_pct_change": 2.0}}
        result = market_normalize(raw, timeframe="15m")
        rs_sigs = [s for snap in result for s in snap.signals if s.type == "relative_strength"]
        assert len(rs_sigs) == 1
        assert rs_sigs[0].score < 0

    def test_relative_strength_below_threshold(self) -> None:
        raw = {"AAPL": {"price_change_24h": 1.5, "spy_pct_change": 1.0}}
        result = market_normalize(raw, timeframe="15m")
        rs_sigs = [s for snap in result for s in snap.signals if s.type == "relative_strength"]
        assert len(rs_sigs) == 0

    def test_empty_input(self) -> None:
        assert market_normalize({}, timeframe="15m") == []


# ── Options Flow (Sentiment Normalizer) ─────────────────────────


class TestOptionsFlow:
    """Tests for options_flow signal from sentiment_normalizer."""

    def test_options_flow_large_sweep(self) -> None:
        raw = {
            "AAPL": {
                "rot_options_flow": [{"contracts": 600, "premium": 1_200_000, "sweep_type": "call_sweep"}],
                "spam_filtered": False,
            }
        }
        result = sentiment_normalize(raw, timeframe="15m")
        of_sigs = [s for snap in result for s in snap.signals if s.type == "options_flow"]
        assert len(of_sigs) == 1
        assert of_sigs[0].score == 2.5
        assert of_sigs[0].confidence == 0.85

    def test_options_flow_medium_sweep(self) -> None:
        raw = {
            "AAPL": {
                "rot_options_flow": [{"contracts": 250, "premium": 600_000, "sweep_type": "call_sweep"}],
                "spam_filtered": False,
            }
        }
        result = sentiment_normalize(raw, timeframe="15m")
        of_sigs = [s for snap in result for s in snap.signals if s.type == "options_flow"]
        assert len(of_sigs) == 1
        assert of_sigs[0].score == 2.0

    def test_options_flow_small_sweep(self) -> None:
        raw = {
            "AAPL": {
                "rot_options_flow": [{"contracts": 60, "premium": 120_000, "sweep_type": "call_sweep"}],
                "spam_filtered": False,
            }
        }
        result = sentiment_normalize(raw, timeframe="15m")
        of_sigs = [s for snap in result for s in snap.signals if s.type == "options_flow"]
        assert len(of_sigs) == 1
        assert of_sigs[0].score == 1.0

    def test_options_flow_put_bearish(self) -> None:
        raw = {
            "AAPL": {
                "rot_options_flow": [{"contracts": 600, "premium": 1_200_000, "sweep_type": "put_sweep"}],
                "spam_filtered": False,
            }
        }
        result = sentiment_normalize(raw, timeframe="15m")
        of_sigs = [s for snap in result for s in snap.signals if s.type == "options_flow"]
        assert len(of_sigs) == 1
        assert of_sigs[0].score == -2.5


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
        # Continuous interpolation: 28.0 is 30% between 25 and 35
        assert 2.0 <= vix_sig.score <= 3.0

    def test_inverted_curve(self) -> None:
        raw = {"vix": 15.0, "yield_curve_slope": -60.0, "risk_on": True}
        result = macro_normalize(raw, timeframe="15m")
        curve_sig = [s for s in result[0].signals if "curve" in s.reason.lower()][0]
        assert curve_sig.score == 1.5

    def test_risk_off_flag(self) -> None:
        raw = {"vix": 15.0, "yield_curve_slope": 50.0, "risk_on": False}
        result = macro_normalize(raw, timeframe="15m")
        assert len(result) == 1
        reasons = [s.reason for s in result[0].signals]
        assert any("risk-on flag" in r.lower() for r in reasons)

    def test_calm_market_emits_risk_on(self) -> None:
        raw = {"vix": 15.0, "yield_curve_slope": 50.0, "risk_on": True}
        result = macro_normalize(raw, timeframe="15m")
        assert len(result) == 1
        # Calm VIX should emit risk-on signal (negative risk_off score)
        vix_sig = [s for s in result[0].signals if "calm" in s.reason.lower()]
        assert len(vix_sig) == 1
        assert vix_sig[0].score < 0

    def test_empty_input(self) -> None:
        result = macro_normalize({}, timeframe="15m")
        assert len(result) == 1
        # Should emit neutral macro snapshot
        assert result[0].symbol == "__GLOBAL_MACRO__"
        assert result[0].signals[0].confidence == 0.0

    def test_timeframe_passed(self) -> None:
        raw = {"vix": 40.0, "risk_on": False}
        result = macro_normalize(raw, timeframe="1h")
        assert result[0].timeframe == "1h"

    def test_nan_vix_ignored(self) -> None:
        raw = {"vix": float("nan"), "yield_curve_slope": 50.0, "risk_on": True}
        result = macro_normalize(raw, timeframe="15m")
        # Always emits a snapshot; NaN VIX produces neutral macro
        assert len(result) == 1
        # No VIX-based signals (NaN is ignored), only neutral
        vix_sigs = [s for s in result[0].signals if "VIX" in s.reason]
        assert len(vix_sigs) == 0

    def test_inf_curve_slope_ignored(self) -> None:
        raw = {"vix": 15.0, "yield_curve_slope": float("inf"), "risk_on": True}
        result = macro_normalize(raw, timeframe="15m")
        # Always emits a snapshot; calm VIX produces risk-on signal
        assert len(result) == 1
        curve_sigs = [s for s in result[0].signals if "curve" in s.reason.lower()]
        assert len(curve_sigs) == 0


# ── Events Normalizer ───────────────────────────────────────────


class TestEventsNormalizer:
    """Tests for events_normalizer.normalize."""

    def test_earnings_tomorrow(self) -> None:
        from datetime import datetime, timedelta, timezone

        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        raw = {"AAPL": {"earnings_date": tomorrow, "hour": "bmo"}}
        result = events_normalize(raw, timeframe="15m")
        assert len(result) == 1
        sig = result[0].signals[0]
        assert sig.type == "catalyst_event"
        # Continuous scoring: ~24h out gives high score close to 2.5
        assert 2.0 <= sig.score <= 2.5
        assert sig.confidence >= 0.80
        assert "[BMO]" in sig.reason

    def test_earnings_in_3_days(self) -> None:
        from datetime import datetime, timedelta, timezone

        dt_obj = datetime.now(timezone.utc) + timedelta(days=3)
        dt = dt_obj.strftime("%Y-%m-%d")
        raw = {"TSLA": {"earnings_date": dt}}
        result = events_normalize(raw, timeframe="15m")
        sig = result[0].signals[0]
        # Continuous interpolation: score depends on actual days_until
        earnings_dt = datetime.strptime(dt, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days_until = (earnings_dt - datetime.now(timezone.utc)).days
        t = days_until / 7.0
        assert sig.score == pytest.approx(2.5 - t * 2.0, abs=0.05)
        assert sig.confidence == pytest.approx(0.90 - t * 0.40, abs=0.05)

    def test_earnings_in_5_days(self) -> None:
        from datetime import datetime, timedelta, timezone

        dt_obj = datetime.now(timezone.utc) + timedelta(days=5)
        dt = dt_obj.strftime("%Y-%m-%d")
        raw = {"MSFT": {"earnings_date": dt}}
        result = events_normalize(raw, timeframe="15m")
        sig = result[0].signals[0]
        # Continuous interpolation: score depends on actual days_until
        earnings_dt = datetime.strptime(dt, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days_until = (earnings_dt - datetime.now(timezone.utc)).days
        t = days_until / 7.0
        assert sig.score == pytest.approx(2.5 - t * 2.0, abs=0.05)
        assert sig.confidence == pytest.approx(0.90 - t * 0.40, abs=0.05)

    def test_earnings_too_far(self) -> None:
        from datetime import datetime, timedelta, timezone

        dt = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%d")
        raw = {"GOOG": {"earnings_date": dt}}
        result = events_normalize(raw, timeframe="15m")
        assert len(result) == 0

    def test_recent_8k(self) -> None:
        raw = {"AMZN": {"recent_8k": True}}
        result = events_normalize(raw, timeframe="1h")
        assert len(result) == 1
        sig = result[0].signals[0]
        assert sig.type == "catalyst_event"
        assert sig.source == "edgar"
        assert sig.score == 2.0
        assert sig.confidence == 0.75

    def test_multiple_filings(self) -> None:
        raw = {"META": {"filing_count": 3}}
        result = events_normalize(raw, timeframe="15m")
        assert len(result) == 1
        sig = result[0].signals[0]
        assert sig.score == 1.0
        assert sig.confidence == 0.60

    def test_single_filing_ignored(self) -> None:
        raw = {"NVDA": {"filing_count": 1}}
        result = events_normalize(raw, timeframe="15m")
        assert len(result) == 0

    def test_empty_input(self) -> None:
        result = events_normalize({}, timeframe="15m")
        assert len(result) == 0

    def test_bad_earnings_date_skipped(self) -> None:
        raw = {"BAD": {"earnings_date": "not-a-date"}}
        result = events_normalize(raw, timeframe="15m")
        assert len(result) == 0

    def test_eps_estimate_in_reason(self) -> None:
        from datetime import datetime, timedelta, timezone

        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        raw = {"AAPL": {"earnings_date": tomorrow, "eps_estimate": 1.52}}
        result = events_normalize(raw, timeframe="15m")
        assert "EPS est $1.52" in result[0].signals[0].reason


# ── SI Normalizer ───────────────────────────────────────────────


class TestSiNormalizer:
    """Tests for si_normalizer.normalize."""

    def test_extreme_si(self) -> None:
        raw = {"GME": {"si_pct_float": 0.30}}
        result = si_normalize(raw, timeframe="15m")
        assert len(result) == 1
        sig = result[0].signals[0]
        assert sig.type == "short_interest"
        assert sig.score == pytest.approx(2.667, abs=0.01)
        assert sig.confidence == pytest.approx(0.883, abs=0.01)

    def test_elevated_si(self) -> None:
        raw = {"AMC": {"si_pct_float": 0.18}}
        result = si_normalize(raw, timeframe="15m")
        sig = result[0].signals[0]
        assert sig.score == pytest.approx(2.01, abs=0.01)
        assert sig.confidence == pytest.approx(0.759, abs=0.01)

    def test_notable_si(self) -> None:
        raw = {"BBBY": {"si_pct_float": 0.12}}
        result = si_normalize(raw, timeframe="15m")
        sig = result[0].signals[0]
        assert sig.score == pytest.approx(1.32, abs=0.01)
        assert sig.confidence == pytest.approx(0.648, abs=0.01)

    def test_below_threshold_ignored(self) -> None:
        raw = {"AAPL": {"si_pct_float": 0.05}}
        result = si_normalize(raw, timeframe="15m")
        assert len(result) == 0

    def test_short_ratio_boost(self) -> None:
        raw = {"GME": {"si_pct_float": 0.25, "short_ratio": 7.0}}
        result = si_normalize(raw, timeframe="15m")
        sig = result[0].signals[0]
        assert sig.score == 3.0  # 2.5 + 0.5 boost
        assert "days-to-cover" in sig.reason

    def test_short_ratio_no_boost_below_5(self) -> None:
        raw = {"AMC": {"si_pct_float": 0.25, "short_ratio": 3.0}}
        result = si_normalize(raw, timeframe="15m")
        sig = result[0].signals[0]
        assert sig.score == 2.5  # no boost

    def test_none_si_pct_skipped(self) -> None:
        raw = {"AAPL": {"si_pct_float": None}}
        result = si_normalize(raw, timeframe="15m")
        assert len(result) == 0

    def test_nan_si_pct_skipped(self) -> None:
        raw = {"AAPL": {"si_pct_float": float("nan")}}
        result = si_normalize(raw, timeframe="15m")
        assert len(result) == 0

    def test_empty_input(self) -> None:
        result = si_normalize({}, timeframe="15m")
        assert len(result) == 0

    def test_timeframe_passed(self) -> None:
        raw = {"GME": {"si_pct_float": 0.30}}
        result = si_normalize(raw, timeframe="1h")
        assert result[0].timeframe == "1h"

    def test_shares_short_in_reason(self) -> None:
        raw = {"GME": {"si_pct_float": 0.25, "shares_short": 12_000_000}}
        result = si_normalize(raw, timeframe="15m")
        assert "12,000,000 shares short" in result[0].signals[0].reason

    def test_boundary_25_pct(self) -> None:
        raw = {"X": {"si_pct_float": 0.25}}
        result = si_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score == 2.5

    def test_boundary_15_pct(self) -> None:
        raw = {"X": {"si_pct_float": 0.15}}
        result = si_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score == pytest.approx(1.8, abs=0.01)

    def test_boundary_10_pct(self) -> None:
        raw = {"X": {"si_pct_float": 0.10}}
        result = si_normalize(raw, timeframe="15m")
        assert result[0].signals[0].score == 1.0


# ── Interpolate utility ─────────────────────────────────────────


class TestInterpolate:
    """Tests for normalizers.interpolate continuous scoring utility."""

    def test_exact_breakpoint(self) -> None:
        from normalizers import interpolate

        bp = [(2.0, 1.0, 0.55), (5.0, 2.0, 0.70), (10.0, 2.8, 0.90)]
        score, conf = interpolate(2.0, bp)
        assert score == 1.0
        assert conf == 0.55

    def test_midpoint_interpolation(self) -> None:
        from normalizers import interpolate

        bp = [(2.0, 1.0, 0.55), (5.0, 2.0, 0.70)]
        score, conf = interpolate(3.5, bp)
        assert 1.0 < score < 2.0
        assert 0.55 < conf < 0.70

    def test_above_highest_breakpoint(self) -> None:
        from normalizers import interpolate

        bp = [(2.0, 1.0, 0.55), (5.0, 2.0, 0.70)]
        score, conf = interpolate(10.0, bp)
        assert score == 2.0
        assert conf == 0.70

    def test_below_lowest_breakpoint(self) -> None:
        from normalizers import interpolate

        bp = [(2.0, 1.0, 0.55), (5.0, 2.0, 0.70)]
        result = interpolate(1.0, bp)
        assert result is None

    def test_at_lowest_breakpoint(self) -> None:
        from normalizers import interpolate

        bp = [(2.0, 1.0, 0.55)]
        score, conf = interpolate(2.0, bp)
        assert score == 1.0
        assert conf == 0.55


# ── TA Normalizer Graceful Degradation ──────────────────────────


class TestTaGracefulDegradation:
    """TA normalizer emits a low-confidence signal when rating is None but patterns exist."""

    def test_none_rating_with_bb_gets_signal(self) -> None:
        raw = {"AAPL": {"rating": None, "patterns": [], "indicators": {"bb_squeeze": True}}}
        result = ta_normalize(raw, timeframe="15m")
        assert len(result) == 1
        assert result[0].signals[0].confidence <= 0.30

    def test_none_rating_no_bb_emits_degraded(self) -> None:
        """None rating, no BB data, no patterns → no signal emitted."""
        raw = {"AAPL": {"rating": None, "patterns": [], "indicators": {}}}
        result = ta_normalize(raw, timeframe="15m")
        assert len(result) == 0


# ── Market Normalizer Sell-Cluster ──────────────────────────────


class TestSellCluster:
    """EDGAR sell-cluster detection (symmetric with buy-cluster)."""

    def test_sell_cluster_detected(self) -> None:
        raw = {"AAPL": {"insider_activity": "selling", "insiders_selling": 4}}
        result = market_normalize(raw, timeframe="15m")
        insider_sigs = [s for snap in result for s in snap.signals if s.type == "insider_activity"]
        assert len(insider_sigs) == 1
        assert insider_sigs[0].score < 0  # negative for sell cluster

    def test_no_sell_cluster_below_threshold(self) -> None:
        raw = {"AAPL": {"insider_activity": "selling", "insiders_selling": 1}}
        result = market_normalize(raw, timeframe="15m")
        insider_sigs = [s for snap in result for s in snap.signals if s.type == "insider_activity"]
        if insider_sigs:
            # Still a sell signal but without cluster boost
            assert insider_sigs[0].score >= -1.5


# ── Market Normalizer SPY Fallback ──────────────────────────────


class TestSpyFallback:
    """Relative strength emits low-confidence signal when SPY data missing."""

    def test_no_spy_still_emits_rs(self) -> None:
        raw = {"AAPL": {"price_change_24h": 5.0}}
        result = market_normalize(raw, timeframe="15m")
        rs_sigs = [s for snap in result for s in snap.signals if s.type == "relative_strength"]
        assert len(rs_sigs) == 1
        assert rs_sigs[0].confidence == 0.20
