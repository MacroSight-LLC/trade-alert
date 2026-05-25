"""Unit tests for formatter.playbook_formatter."""

from __future__ import annotations

from unittest.mock import patch

from formatter.playbook_formatter import PlaybookFormatter
from discord_formatter import format_embed as shim_format_embed


@patch("formatter.embed.get_similar_alert_stats", return_value="")
def test_playbook_formatter_delegates_format_embed(_mock_stats, sample_alert) -> None:
    expected = shim_format_embed(sample_alert)
    actual = PlaybookFormatter.format_embed(sample_alert)
    expected["embeds"][0].pop("timestamp", None)
    actual["embeds"][0].pop("timestamp", None)
    assert actual == expected


def test_compute_rr_matches_shim() -> None:
    entry = {"level": 100.0, "stop": 95.0, "target": 110.0}
    from discord_formatter import compute_rr as shim_rr

    assert PlaybookFormatter.compute_rr(entry) == shim_rr(entry)
