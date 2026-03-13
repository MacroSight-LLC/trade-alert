"""Unit tests for alert_quality.py — per-alert and batch quality scoring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from alert_quality import (
    _TECHNICAL_TERMS,
    _VAGUE_PHRASES,
    post_quality_scores,
    score_alert,
    score_batch,
    score_confidence_calibration,
    score_rr_ratio,
    score_signal_coverage,
    score_thesis_quality,
)
from models import PlaybookAlert

# ── Helpers ──────────────────────────────────────────────────────


def _make_alert(
    *,
    symbol: str = "AAPL",
    direction: str = "LONG",
    ep: float = 0.80,
    conf: float = 0.85,
    sa: int = 5,
    thesis: str = "RSI divergence at support with volume spike driven by momentum breakout.",
    entry_level: float = 185.0,
    stop: float = 180.0,
    target: float = 200.0,
    timeframe: str = "15m",
) -> PlaybookAlert:
    """Build a PlaybookAlert with sensible defaults for testing."""
    return PlaybookAlert(
        symbol=symbol,
        direction=direction,
        edge_probability=ep,
        confidence=conf,
        timeframe=timeframe,
        thesis=thesis,
        entry={"level": entry_level, "stop": stop, "target": target},
        timeframe_rationale="Testing.",
        sentiment_context="Neutral.",
        unusual_activity=[],
        macro_regime="Risk-on.",
        sources_agree=sa,
    )


# ── score_thesis_quality ────────────────────────────────────────


class TestScoreThesisQuality:
    """Tests for thesis specificity scoring."""

    def test_empty_thesis(self) -> None:
        assert score_thesis_quality("") == 0.0

    def test_short_generic_thesis(self) -> None:
        score = score_thesis_quality("Buy AAPL now.")
        assert score < 0.3

    def test_long_specific_thesis(self) -> None:
        thesis = (
            "RSI divergence at 28 on 15m with bollinger band squeeze, "
            "confirmed by volume spike 2.3x average, leading to a breakout "
            "above resistance at 185.50."
        )
        score = score_thesis_quality(thesis)
        assert score >= 0.75

    def test_numeric_values_boost(self) -> None:
        base = "The stock shows a breakout pattern with momentum divergence."
        with_nums = "The stock shows a breakout pattern at 185.50 with 2.3x volume."
        assert score_thesis_quality(with_nums) > score_thesis_quality(base)

    def test_technical_terms_boost(self) -> None:
        plain = "The chart looks promising with some movement."
        technical = "MACD divergence with RSI oversold and bollinger squeeze confirmed."
        assert score_thesis_quality(technical) > score_thesis_quality(plain)

    def test_vague_phrases_penalty(self) -> None:
        good = "RSI at 28 with volume divergence driven by momentum."
        vague = "Strong signals from multiple sources look good and appear strong."
        assert score_thesis_quality(good) > score_thesis_quality(vague)

    def test_causal_language_boost(self) -> None:
        no_causal = "RSI divergence at support with volume spike breakout momentum."
        causal = "RSI divergence at support because volume spike drove breakout."
        assert score_thesis_quality(causal) >= score_thesis_quality(no_causal)

    def test_score_clamped_at_one(self) -> None:
        # A thesis with every quality signal shouldn't exceed 1.0
        thesis = (
            "RSI divergence at 28 on 15m because bollinger band squeeze "
            "was confirmed by 3.5x vwap volume spike, leading to breakout "
            "above 185.50 resistance with macd crossover and ema support."
        )
        assert score_thesis_quality(thesis) <= 1.0

    def test_score_floored_at_zero(self) -> None:
        # All vague phrases, should get penalized but not below 0
        thesis = "Strong signals positive outlook looks good."
        assert score_thesis_quality(thesis) >= 0.0

    def test_medium_length_gets_partial_credit(self) -> None:
        # 10-14 words: partial credit for length
        thesis = "RSI divergence breakout with volume spike above key level."
        score = score_thesis_quality(thesis)
        assert 0.0 < score < 1.0


# ── score_rr_ratio ──────────────────────────────────────────────


class TestScoreRRRatio:
    """Tests for reward:risk ratio scoring."""

    def test_excellent_rr(self) -> None:
        # 3:1 R:R → 1.0
        entry = {"level": 100.0, "stop": 97.0, "target": 109.0}
        assert score_rr_ratio(entry, "LONG") == 1.0

    def test_above_3_to_1(self) -> None:
        # 4:1 → still 1.0
        entry = {"level": 100.0, "stop": 95.0, "target": 120.0}
        assert score_rr_ratio(entry, "LONG") == 1.0

    def test_exactly_2_to_1(self) -> None:
        entry = {"level": 100.0, "stop": 95.0, "target": 110.0}
        score = score_rr_ratio(entry, "LONG")
        assert score == 0.5

    def test_below_2_to_1(self) -> None:
        entry = {"level": 100.0, "stop": 95.0, "target": 105.0}
        score = score_rr_ratio(entry, "LONG")
        assert 0.0 < score < 0.5

    def test_short_direction(self) -> None:
        entry = {"level": 100.0, "stop": 103.0, "target": 91.0}
        assert score_rr_ratio(entry, "SHORT") == 1.0

    def test_watch_direction(self) -> None:
        assert score_rr_ratio({}, "WATCH") == 0.5

    def test_zero_risk(self) -> None:
        entry = {"level": 100.0, "stop": 100.0, "target": 110.0}
        assert score_rr_ratio(entry, "LONG") == 0.0

    def test_missing_keys(self) -> None:
        assert score_rr_ratio({"level": 100.0}, "LONG") == 0.0

    def test_empty_dict(self) -> None:
        assert score_rr_ratio({}, "LONG") == 0.0


# ── score_signal_coverage ───────────────────────────────────────


class TestScoreSignalCoverage:
    """Tests for signal source coverage scoring."""

    def test_five_or_more_sources(self) -> None:
        assert score_signal_coverage(5) == 1.0
        assert score_signal_coverage(8) == 1.0

    def test_four_sources(self) -> None:
        assert score_signal_coverage(4) == 0.85

    def test_three_sources(self) -> None:
        assert score_signal_coverage(3) == 0.6

    def test_two_sources(self) -> None:
        assert score_signal_coverage(2) == pytest.approx(0.4)

    def test_one_source(self) -> None:
        assert score_signal_coverage(1) == pytest.approx(0.2)

    def test_zero_sources(self) -> None:
        assert score_signal_coverage(0) == 0.0


# ── score_confidence_calibration ────────────────────────────────


class TestScoreConfidenceCalibration:
    """Tests for EP-vs-evidence calibration scoring."""

    def test_well_calibrated(self) -> None:
        # EP ≤ 0.70 + 5*0.05 = 0.95 (EP=0.80 with 5 sources) → 1.0
        assert score_confidence_calibration(0.80, 0.85, 5) == 1.0

    def test_very_high_ep_low_sources(self) -> None:
        # EP > 0.90, sa < 4 → 0.3
        assert score_confidence_calibration(0.92, 0.85, 3) == 0.3

    def test_high_ep_low_sources(self) -> None:
        # EP > 0.85, sa < 4 → 0.5
        assert score_confidence_calibration(0.87, 0.85, 3) == 0.5

    def test_low_confidence_high_ep(self) -> None:
        # conf < 0.75, EP > 0.80 → 0.4
        assert score_confidence_calibration(0.82, 0.70, 5) == 0.4

    def test_slightly_overconfident(self) -> None:
        # EP above max_reasonable but not egregiously
        # max_reasonable = min(0.70 + 3*0.05, 0.95) = 0.85
        # EP=0.86, sa=3, conf=0.80 → 0.7 (slightly over)
        # But sa < 4 and EP > 0.85 hits the second branch → 0.5
        assert score_confidence_calibration(0.86, 0.80, 3) == 0.5

    def test_ep_at_max_reasonable(self) -> None:
        # Due to floating point: 0.70 + 4*0.05 = 0.8999... < 0.90
        # So EP=0.90 is slightly over → returns 0.7 (slightly over-confident)
        assert score_confidence_calibration(0.90, 0.85, 4) == 0.7
        # With 5 sources: max = min(0.95, 0.95) = 0.95, EP=0.90 ≤ 0.95 → 1.0
        assert score_confidence_calibration(0.90, 0.85, 5) == 1.0


# ── score_alert ─────────────────────────────────────────────────


class TestScoreAlert:
    """Tests for the composite alert scoring function."""

    def test_returns_all_sub_scores(self) -> None:
        alert = _make_alert()
        scores = score_alert(alert)
        expected_keys = {
            "thesis_quality",
            "rr_ratio",
            "signal_coverage",
            "confidence_calibration",
            "composite_quality",
        }
        assert set(scores.keys()) == expected_keys

    def test_composite_is_weighted_average(self) -> None:
        alert = _make_alert()
        scores = score_alert(alert)
        weights = {
            "thesis_quality": 0.25,
            "rr_ratio": 0.30,
            "signal_coverage": 0.20,
            "confidence_calibration": 0.25,
        }
        expected = sum(scores[k] * weights[k] for k in weights)
        assert scores["composite_quality"] == pytest.approx(expected)

    def test_high_quality_alert(self) -> None:
        alert = _make_alert(
            thesis=(
                "RSI divergence at 28 on 15m because bollinger band squeeze "
                "confirmed by 3.5x volume spike, leading to breakout above "
                "185.50 resistance with macd crossover support."
            ),
            ep=0.80,
            sa=5,
            entry_level=185.0,
            stop=180.0,
            target=200.0,  # 4:1 R:R
        )
        scores = score_alert(alert)
        assert scores["composite_quality"] >= 0.70

    def test_low_quality_alert(self) -> None:
        alert = _make_alert(
            thesis="Buy AAPL now.",
            ep=0.95,
            sa=2,
            entry_level=185.0,
            stop=184.0,
            target=186.0,  # 1:1 R:R
        )
        scores = score_alert(alert)
        assert scores["composite_quality"] < 0.50


# ── score_batch ─────────────────────────────────────────────────


class TestScoreBatch:
    """Tests for batch-level quality scoring."""

    def test_empty_batch(self) -> None:
        result = score_batch([])
        assert result["batch_diversity"] == 1.0
        assert result["batch_avg_quality"] == 0.0
        assert result["overlapping_entries"] == 0

    def test_single_alert(self) -> None:
        result = score_batch([_make_alert()])
        assert result["batch_concentration"] == 0.0
        assert result["overlapping_entries"] == 0

    def test_diverse_batch(self) -> None:
        alerts = [
            _make_alert(symbol="AAPL", direction="LONG", entry_level=185.0),
            _make_alert(symbol="TSLA", direction="SHORT", entry_level=250.0),
        ]
        result = score_batch(alerts)
        assert result["batch_diversity"] >= 0.8  # Two directions, two symbols
        assert result["batch_concentration"] == 0.0

    def test_concentrated_batch(self) -> None:
        alerts = [
            _make_alert(symbol="AAPL", direction="LONG"),
            _make_alert(symbol="AAPL", direction="LONG"),
            _make_alert(symbol="AAPL", direction="LONG"),
        ]
        result = score_batch(alerts)
        assert result["batch_concentration"] > 0.5

    def test_single_direction_many_alerts_penalized(self) -> None:
        # 3 alerts all LONG → direction_score = 0.3
        alerts = [
            _make_alert(symbol="AAPL", direction="LONG", entry_level=185.0),
            _make_alert(symbol="TSLA", direction="LONG", entry_level=250.0),
            _make_alert(symbol="NVDA", direction="LONG", entry_level=900.0),
        ]
        result = score_batch(alerts)
        # Single direction with >2 alerts → lower diversity score
        assert result["batch_diversity"] < 0.8

    def test_overlapping_entries_detected(self) -> None:
        # Two entries within 2% of each other: 100.0 and 101.0
        alerts = [
            _make_alert(symbol="AAPL", direction="LONG", entry_level=100.0),
            _make_alert(symbol="MSFT", direction="LONG", entry_level=101.0),
        ]
        result = score_batch(alerts)
        assert result["overlapping_entries"] == 1

    def test_non_overlapping_entries(self) -> None:
        alerts = [
            _make_alert(symbol="AAPL", direction="LONG", entry_level=100.0),
            _make_alert(symbol="TSLA", direction="LONG", entry_level=250.0),
        ]
        result = score_batch(alerts)
        assert result["overlapping_entries"] == 0

    def test_watch_entries_excluded_from_overlap(self) -> None:
        alerts = [
            _make_alert(symbol="AAPL", direction="WATCH", entry_level=100.0),
            _make_alert(symbol="MSFT", direction="WATCH", entry_level=101.0),
        ]
        result = score_batch(alerts)
        assert result["overlapping_entries"] == 0

    def test_batch_avg_quality_calculated(self) -> None:
        alerts = [_make_alert(), _make_alert()]
        result = score_batch(alerts)
        assert 0.0 < result["batch_avg_quality"] <= 1.0


# ── post_quality_scores ─────────────────────────────────────────


class TestPostQualityScores:
    """Tests for Langfuse score posting."""

    def test_empty_alerts(self) -> None:
        result = post_quality_scores("trace-123", [])
        assert result["per_alert"] == []
        assert result["batch"]["batch_avg_quality"] == 0.0

    @patch("alert_quality.add_score", create=True)
    def test_posts_scores_with_trace_id(self, mock_add_score: MagicMock) -> None:
        # Import pipeline_tracing mock into alert_quality
        with patch.dict("sys.modules", {"pipeline_tracing": MagicMock(add_score=mock_add_score)}):
            alerts = [_make_alert()]
            result = post_quality_scores("trace-123", alerts)

        assert len(result["per_alert"]) == 1
        assert "composite_quality" in result["per_alert"][0]["scores"]
        assert result["batch"]["batch_avg_quality"] > 0.0

    def test_no_trace_id_still_returns_scores(self) -> None:
        alerts = [_make_alert()]
        result = post_quality_scores(None, alerts)
        assert len(result["per_alert"]) == 1
        assert "batch" in result


# ── Module-level constants ──────────────────────────────────────


class TestConstants:
    """Tests for module constants."""

    def test_technical_terms_is_frozenset(self) -> None:
        assert isinstance(_TECHNICAL_TERMS, frozenset)

    def test_technical_terms_has_equity_terms(self) -> None:
        for term in ("rsi", "macd", "vwap", "bollinger", "earnings"):
            assert term in _TECHNICAL_TERMS

    def test_vague_phrases_is_frozenset(self) -> None:
        assert isinstance(_VAGUE_PHRASES, frozenset)
