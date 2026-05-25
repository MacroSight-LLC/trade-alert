"""Unit tests for discord_formatter.py embed structure, limits, and formatting."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from discord_formatter import (
    MAX_EMBED_DESCRIPTION_CHARS,
    MAX_EMBED_FIELDS,
    _enforce_embed_limits,
    _quality_color,
    _score_bar,
    _truncate_field,
    compute_rr,
    format_embed,
)
from models import PlaybookAlert


@pytest.fixture()
def sample_alert() -> PlaybookAlert:
    return PlaybookAlert(
        symbol="AAPL",
        direction="LONG",
        edge_probability=0.75,
        confidence=0.80,
        timeframe="15m",
        thesis="Breakout with volume confirmation.",
        entry={"level": 100.0, "stop": 95.0, "target": 110.0},
        timeframe_rationale="15m structure aligns with 1h trend.",
        sentiment_context="Bullish retail flow.",
        unusual_activity=[],
        macro_regime="Risk-on.",
        sources_agree=4,
    )


class TestFormatEmbedStructure:
    def test_embed_has_required_keys(self, sample_alert: PlaybookAlert) -> None:
        result = format_embed(sample_alert)
        assert "embeds" in result
        embed = result["embeds"][0]
        assert "title" in embed
        assert "description" in embed
        assert "color" in embed
        assert "fields" in embed
        assert "AAPL" in embed["title"]

    def test_description_within_discord_limit(self, sample_alert: PlaybookAlert) -> None:
        with (
            patch("formatter.embed._truncate_field", return_value="x" * 5000),
            patch.object(logging.Logger, "warning") as mock_warn,
        ):
            result = format_embed(sample_alert)
        desc = result["embeds"][0]["description"]
        assert len(desc) <= MAX_EMBED_DESCRIPTION_CHARS
        mock_warn.assert_called()


class TestEnforceEmbedLimits:
    def test_truncates_description(self) -> None:
        long_desc = "a" * (MAX_EMBED_DESCRIPTION_CHARS + 100)
        payload = {"embeds": [{"description": long_desc, "fields": []}]}
        with patch.object(logging.Logger, "warning"):
            out = _enforce_embed_limits(payload)
        assert len(out["embeds"][0]["description"]) == MAX_EMBED_DESCRIPTION_CHARS

    def test_truncates_fields(self) -> None:
        fields = [{"name": f"f{i}", "value": "v", "inline": False} for i in range(30)]
        payload = {"embeds": [{"description": "ok", "fields": fields}]}
        with patch.object(logging.Logger, "warning"):
            out = _enforce_embed_limits(payload)
        assert len(out["embeds"][0]["fields"]) == MAX_EMBED_FIELDS

    def test_truncate_field_helper(self) -> None:
        text = "z" * 2000
        assert len(_truncate_field(text, max_len=1000)) == 1000


# ── compute_rr ──────────────────────────────────────────────────


class TestComputeRR:
    """Tests for reward:risk computation."""

    def test_basic_long(self) -> None:
        entry = {"level": 100.0, "stop": 95.0, "target": 115.0}
        assert compute_rr(entry) == "3.0:1"

    def test_basic_short(self) -> None:
        entry = {"level": 100.0, "stop": 105.0, "target": 85.0}
        assert compute_rr(entry) == "3.0:1"

    def test_1_to_1(self) -> None:
        entry = {"level": 100.0, "stop": 95.0, "target": 105.0}
        assert compute_rr(entry) == "1.0:1"

    def test_zero_risk_returns_na(self) -> None:
        entry = {"level": 100.0, "stop": 100.0, "target": 110.0}
        assert compute_rr(entry) == "N/A"

    def test_missing_key_returns_na(self) -> None:
        assert compute_rr({"level": 100.0}) == "N/A"

    def test_empty_dict_returns_na(self) -> None:
        assert compute_rr({}) == "N/A"


# ── format_embed ────────────────────────────────────────────────


class TestFormatEmbed:
    """Tests for Discord embed formatting."""

    @pytest.fixture()
    def mock_alert(self) -> PlaybookAlert:
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

    def test_embed_has_embeds_key(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        assert "embeds" in result
        assert len(result["embeds"]) == 1

    def test_embed_title_contains_symbol(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        title = result["embeds"][0]["title"]
        assert "NVDA" in title
        assert "LONG" in title

    def test_embed_has_description_thesis(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        assert "description" in result["embeds"][0]
        assert "momentum breakout" in result["embeds"][0]["description"]

    def test_embed_color_long(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        # EP=0.82 * conf=0.85 = 0.697 → amber range (0.65-0.80)
        assert result["embeds"][0]["color"] == 15905331  # amber

    def test_embed_color_short(self, mock_alert: PlaybookAlert) -> None:
        mock_alert.direction = "SHORT"
        mock_alert.edge_probability = 0.50
        mock_alert.confidence = 0.80
        result = format_embed(mock_alert)
        # EP=0.50 * conf=0.80 = 0.40 → red range (< 0.65)
        assert result["embeds"][0]["color"] == 15158332  # red

    def test_embed_has_many_fields(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        # New format has separator + signal strength + playbook + context sections
        assert len(result["embeds"][0]["fields"]) > 5

    def test_embed_entry_values_in_fields(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        all_values = " ".join(f["value"] for f in result["embeds"][0]["fields"])
        assert "875.00" in all_values
        assert "865.00" in all_values
        assert "900.00" in all_values

    def test_embed_rr_in_fields(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        all_values = " ".join(f["value"] for f in result["embeds"][0]["fields"])
        assert "2.5:1" in all_values

    def test_embed_sources_in_fields(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        all_values = " ".join(f["value"] for f in result["embeds"][0]["fields"])
        assert "5/10" in all_values

    def test_embed_unusual_activity_listed(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        all_values = " ".join(f["value"] for f in result["embeds"][0]["fields"])
        assert "IV spike" in all_values

    def test_embed_empty_unusual_activity(self, mock_alert: PlaybookAlert) -> None:
        mock_alert.unusual_activity = []
        result = format_embed(mock_alert)
        all_values = " ".join(f["value"] for f in result["embeds"][0]["fields"])
        assert "None detected" in all_values

    def test_embed_has_timestamp(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        assert "timestamp" in result["embeds"][0]

    def test_embed_has_footer(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        assert "footer" in result["embeds"][0]

    def test_embed_has_edge_bar(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        field_names = [f["name"] for f in result["embeds"][0]["fields"]]
        assert any("Edge" in n for n in field_names)

    def test_embed_has_confidence_bar(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        field_names = [f["name"] for f in result["embeds"][0]["fields"]]
        assert any("Confidence" in n for n in field_names)

    def test_embed_has_sentiment_field(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        field_names = [f["name"] for f in result["embeds"][0]["fields"]]
        assert any("Sentiment" in n for n in field_names)

    def test_embed_has_macro_field(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        field_names = [f["name"] for f in result["embeds"][0]["fields"]]
        assert any("Macro" in n for n in field_names)

    def test_embed_high_edge_label(self, mock_alert: PlaybookAlert) -> None:
        mock_alert.edge_probability = 0.90
        result = format_embed(mock_alert)
        assert "HIGH EDGE" in result["embeds"][0]["title"]

    def test_embed_no_image_field_by_default(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        assert "image" not in result["embeds"][0]

    def test_embed_moderate_edge_label(self, mock_alert: PlaybookAlert) -> None:
        mock_alert.edge_probability = 0.50
        result = format_embed(mock_alert)
        assert "MODERATE" in result["embeds"][0]["title"]

    def test_current_price_marked_unavailable_when_stale(self, mock_alert: PlaybookAlert) -> None:
        stale_ts = (datetime.now(UTC) - timedelta(minutes=80)).isoformat()
        result = format_embed(mock_alert, current_price=874.0, current_price_ts=stale_ts)
        fields = result["embeds"][0]["fields"]
        cp_field = next(f for f in fields if f["name"] == "📍 Current Price")
        assert "stale market data" in cp_field["value"]

    def test_current_price_shown_when_fresh(self, mock_alert: PlaybookAlert) -> None:
        fresh_ts = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        result = format_embed(mock_alert, current_price=878.5, current_price_ts=fresh_ts)
        fields = result["embeds"][0]["fields"]
        cp_field = next(f for f in fields if f["name"] == "📍 Current Price")
        assert "$878.50" in cp_field["value"]
        assert "As of:" in cp_field["value"]


# ── _score_bar ──────────────────────────────────────────────────


class TestScoreBar:
    """Tests for the visual Unicode progress bar."""

    def test_full_bar(self) -> None:
        assert _score_bar(1.0) == "▓▓▓▓▓▓▓▓▓▓ 100%"

    def test_empty_bar(self) -> None:
        assert _score_bar(0.0) == "░░░░░░░░░░ 0%"

    def test_partial_bar(self) -> None:
        bar = _score_bar(0.7)
        assert bar.startswith("▓▓▓▓▓▓▓░░░")
        assert "70%" in bar

    def test_custom_segments(self) -> None:
        bar = _score_bar(0.5, segments=4)
        assert bar == "▓▓░░ 50%"


# ── _truncate_field ─────────────────────────────────────────────


class TestTruncateField:
    """Tests for _truncate_field utility."""

    def test_short_text_unchanged(self) -> None:
        assert _truncate_field("hello") == "hello"

    def test_long_text_truncated(self) -> None:
        text = "x" * 1100
        result = _truncate_field(text, max_len=1000)
        assert len(result) == 1000
        assert result.endswith("...")

    def test_exact_length_unchanged(self) -> None:
        text = "x" * 1000
        assert _truncate_field(text, max_len=1000) == text

    def test_empty_string(self) -> None:
        assert _truncate_field("") == ""


# ── _quality_color ──────────────────────────────────────────────


class TestQualityColor:
    """Tests for confidence-tier color coding."""

    def test_high_quality_green(self) -> None:
        alert = PlaybookAlert(
            symbol="X",
            direction="LONG",
            edge_probability=0.90,
            confidence=0.90,
            timeframe="15m",
            thesis="t",
            entry={"level": 1, "stop": 0.9, "target": 1.3},
            timeframe_rationale="t",
            sentiment_context="t",
            unusual_activity=[],
            macro_regime="t",
            sources_agree=5,
        )
        assert _quality_color(alert) == 3066993  # green

    def test_medium_quality_amber(self) -> None:
        alert = PlaybookAlert(
            symbol="X",
            direction="LONG",
            edge_probability=0.80,
            confidence=0.85,
            timeframe="15m",
            thesis="t",
            entry={"level": 1, "stop": 0.9, "target": 1.3},
            timeframe_rationale="t",
            sentiment_context="t",
            unusual_activity=[],
            macro_regime="t",
            sources_agree=5,
        )
        # 0.80 * 0.85 = 0.68 → amber range (0.65-0.80)
        assert _quality_color(alert) == 15905331  # amber

    def test_low_quality_red(self) -> None:
        alert = PlaybookAlert(
            symbol="X",
            direction="LONG",
            edge_probability=0.70,
            confidence=0.80,
            timeframe="15m",
            thesis="t",
            entry={"level": 1, "stop": 0.9, "target": 1.3},
            timeframe_rationale="t",
            sentiment_context="t",
            unusual_activity=[],
            macro_regime="t",
            sources_agree=5,
        )
        # 0.70 * 0.80 = 0.56 → red range (< 0.65)
        assert _quality_color(alert) == 15158332  # red


# ── Embed historical stats field ────────────────────────────────


class TestEmbedHistoricalStats:
    """format_embed includes a Track Record field."""

    @patch(
        "formatter.embed.get_similar_alert_stats",
        return_value="\U0001f4ca Similar past alerts: 70% win rate (N=10)",
    )
    def test_track_record_field_added(self, _mock_stats: MagicMock) -> None:
        alert = PlaybookAlert(
            symbol="NVDA",
            direction="LONG",
            edge_probability=0.82,
            confidence=0.85,
            timeframe="15m",
            thesis="Test.",
            entry={"level": 100, "stop": 95, "target": 110},
            timeframe_rationale="t",
            sentiment_context="t",
            unusual_activity=[],
            macro_regime="t",
            sources_agree=5,
        )
        result = format_embed(alert)
        field_names = [f["name"] for f in result["embeds"][0]["fields"]]
        assert any("Track Record" in n for n in field_names)

    @patch("formatter.embed.get_similar_alert_stats", return_value="")
    def test_no_track_record_when_empty(self, _mock_stats: MagicMock) -> None:
        alert = PlaybookAlert(
            symbol="NVDA",
            direction="LONG",
            edge_probability=0.82,
            confidence=0.85,
            timeframe="15m",
            thesis="Test.",
            entry={"level": 100, "stop": 95, "target": 110},
            timeframe_rationale="t",
            sentiment_context="t",
            unusual_activity=[],
            macro_regime="t",
            sources_agree=5,
        )
        result = format_embed(alert)
        field_names = [f["name"] for f in result["embeds"][0]["fields"]]
        assert not any("Track Record" in n for n in field_names)
