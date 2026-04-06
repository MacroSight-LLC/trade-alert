"""Unit tests for the forecast normalizer."""

from __future__ import annotations

import pytest

from normalizers.forecast_normalizer import normalize


@pytest.fixture()
def _base_forecast() -> dict:
    """Minimal valid forecast result for one symbol."""
    return {
        "AAPL": {
            "median_forecast": [150.5, 151.0, 151.8, 152.5],
            "quantiles": {
                "p10": [149.0, 148.5, 148.0, 147.5],
                "p50": [150.5, 151.0, 151.8, 152.5],
                "p90": [152.0, 153.5, 155.0, 157.0],
            },
            "current_price": 150.0,
            "horizon_bars": 4,
            "direction_pct": 1.67,
        }
    }


class TestNormalize:
    """Tests for forecast_normalizer.normalize()."""

    def test_valid_forecast_produces_snapshot(self, _base_forecast: dict) -> None:
        """Valid forecast data should produce exactly one Snapshot."""
        snaps = normalize(_base_forecast, timeframe="15m")
        assert len(snaps) == 1
        snap = snaps[0]
        assert snap.symbol == "AAPL"
        assert snap.timeframe == "15m"
        assert len(snap.signals) == 1

        sig = snap.signals[0]
        assert sig.source == "timesfm"
        assert sig.type == "price_forecast"
        assert -3.0 <= sig.score <= 3.0
        assert 0.0 <= sig.confidence <= 1.0
        assert "TimesFM" in sig.reason

    def test_missing_median_forecast_skips_symbol(self) -> None:
        """Symbol with no median_forecast should be skipped."""
        raw = {
            "AAPL": {
                "quantiles": {"p10": [1], "p90": [2]},
                "current_price": 150.0,
                "direction_pct": 1.0,
            }
        }
        snaps = normalize(raw, timeframe="15m")
        assert len(snaps) == 0

    def test_empty_median_forecast_skips(self) -> None:
        """Empty median_forecast list should be skipped."""
        raw = {
            "AAPL": {
                "median_forecast": [],
                "current_price": 150.0,
                "direction_pct": 1.0,
            }
        }
        snaps = normalize(raw, timeframe="15m")
        assert len(snaps) == 0

    def test_zero_current_price_skips(self) -> None:
        """Zero current_price should be skipped to avoid division by zero."""
        raw = {
            "AAPL": {
                "median_forecast": [1.0, 2.0],
                "current_price": 0.0,
                "direction_pct": 1.0,
            }
        }
        snaps = normalize(raw, timeframe="15m")
        assert len(snaps) == 1
        assert snaps[0].signals[0].confidence <= 0.20

    def test_zero_direction_pct_gives_zero_score(self) -> None:
        """Zero direction_pct should produce score=0.0 (neutral)."""
        raw = {
            "AAPL": {
                "median_forecast": [150.0, 150.0],
                "quantiles": {"p10": [149.5], "p50": [150.0], "p90": [150.5]},
                "current_price": 150.0,
                "direction_pct": 0.0,
            }
        }
        snaps = normalize(raw, timeframe="15m")
        assert len(snaps) == 1
        assert snaps[0].signals[0].score == 0.0

    def test_score_clamped_at_positive_3(self) -> None:
        """direction_pct > 30% should clamp score to +3.0."""
        raw = {
            "AAPL": {
                "median_forecast": [200.0],
                "current_price": 150.0,
                "direction_pct": 50.0,  # 50% => score would be 500 unclamped
            }
        }
        snaps = normalize(raw, timeframe="15m")
        assert snaps[0].signals[0].score == 3.0

    def test_score_clamped_at_negative_3(self) -> None:
        """direction_pct < -30% should clamp score to -3.0."""
        raw = {
            "TSLA": {
                "median_forecast": [50.0],
                "current_price": 100.0,
                "direction_pct": -50.0,
            }
        }
        snaps = normalize(raw, timeframe="1h")
        assert snaps[0].signals[0].score == -3.0

    def test_wide_quantile_spread_low_confidence(self) -> None:
        """Wide quantile spread relative to price should produce low confidence."""
        raw = {
            "AAPL": {
                "median_forecast": [150.0, 155.0],
                "quantiles": {
                    "p10": [100.0, 90.0],  # very wide: ~$60 spread
                    "p90": [200.0, 210.0],
                },
                "current_price": 150.0,
                "direction_pct": 3.0,
            }
        }
        snaps = normalize(raw, timeframe="15m")
        assert snaps[0].signals[0].confidence < 0.5

    def test_narrow_quantile_spread_high_confidence(self) -> None:
        """Narrow quantile spread should produce high confidence."""
        raw = {
            "AAPL": {
                "median_forecast": [150.5, 151.0],
                "quantiles": {
                    "p10": [150.3, 150.8],  # ~$0.2 spread
                    "p90": [150.7, 151.2],
                },
                "current_price": 150.0,
                "direction_pct": 0.67,
            }
        }
        snaps = normalize(raw, timeframe="15m")
        assert snaps[0].signals[0].confidence > 0.9

    def test_no_quantiles_default_confidence(self) -> None:
        """Missing quantiles should use conservative default confidence."""
        raw = {
            "AAPL": {
                "median_forecast": [152.0],
                "current_price": 150.0,
                "direction_pct": 1.33,
            }
        }
        snaps = normalize(raw, timeframe="15m")
        assert snaps[0].signals[0].confidence == 0.4

    def test_multiple_symbols(self) -> None:
        """Multiple symbols should each produce one Snapshot."""
        raw = {
            "AAPL": {
                "median_forecast": [152.0],
                "current_price": 150.0,
                "direction_pct": 1.33,
            },
            "MSFT": {
                "median_forecast": [310.0],
                "current_price": 300.0,
                "direction_pct": 3.33,
            },
        }
        snaps = normalize(raw, timeframe="1h")
        assert len(snaps) == 2
        symbols = {s.symbol for s in snaps}
        assert symbols == {"AAPL", "MSFT"}

    def test_reason_contains_endpoint_pct(self, _base_forecast: dict) -> None:
        """Reason string should contain the endpoint percentage change."""
        snaps = normalize(_base_forecast, timeframe="15m")
        reason = snaps[0].signals[0].reason
        assert "%" in reason
        assert "TimesFM" in reason
        assert "bars" in reason
