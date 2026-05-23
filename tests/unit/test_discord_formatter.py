"""Unit tests for discord_formatter.py embed structure and limits."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from discord_formatter import (
    MAX_EMBED_DESCRIPTION_CHARS,
    MAX_EMBED_FIELDS,
    _enforce_embed_limits,
    _truncate_field,
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
            patch("discord_formatter._truncate_field", return_value="x" * 5000),
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
