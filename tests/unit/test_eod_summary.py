"""Unit tests for eod_summary.py."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from eod_summary import _ET, _build_eod_embed

_TODAY = "2026-05-22"


def _session_hash(**kwargs: str) -> dict[str, str]:
    base: dict[str, str] = {
        "decision_runs": "3",
        "llm_candidates": "10",
        "alerts_passed_total": "2",
        "alerts_rejected": "5",
        "gate_dir_ep_threshold": "3",
        "gate_dir_conf_threshold": "2",
    }
    base.update(kwargs)
    return base


class TestBuildEodEmbed:
    @patch("eod_summary.datetime")
    @patch("db.get_recent_alerts")
    @patch("redis_client.get_redis")
    def test_happy_path(
        self,
        mock_get_redis: MagicMock,
        mock_get_recent: MagicMock,
        mock_datetime: MagicMock,
    ) -> None:
        mock_datetime.now.return_value = datetime(2026, 5, 22, 16, 0, tzinfo=_ET)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
        mock_redis = MagicMock()
        mock_redis.hgetall.return_value = _session_hash()
        mock_get_redis.return_value = mock_redis
        mock_get_recent.return_value = [
            {
                "symbol": "AAPL",
                "direction": "LONG",
                "edge_probability": 0.8,
                "confidence": 0.85,
                "outcome": "open",
                "created_at": f"{_TODAY}T14:00:00+00:00",
            }
        ]

        embed = _build_eod_embed()
        fields = {f["name"]: f["value"] for f in embed["embeds"][0]["fields"]}

        assert _TODAY in embed["embeds"][0]["title"]
        assert fields["🔄 Decision Runs"] == "6"  # 3 per timeframe × 2
        assert fields["🎯 Candidates Seen"] == "20"
        assert fields["✅ Alerts Fired"] == "4"
        assert "ep_threshold" in fields["🚧 Top Gate Rejections"]
        assert "AAPL" in fields["📣 Alerts"]
        assert fields["📈 Pass Rate"] == "20%"

    @patch("eod_summary.datetime")
    @patch("db.get_recent_alerts")
    @patch("redis_client.get_redis")
    def test_empty_day(
        self,
        mock_get_redis: MagicMock,
        mock_get_recent: MagicMock,
        mock_datetime: MagicMock,
    ) -> None:
        mock_datetime.now.return_value = datetime(2026, 5, 22, 16, 0, tzinfo=_ET)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
        mock_redis = MagicMock()
        mock_redis.hgetall.return_value = {
            "decision_runs": "0",
            "llm_candidates": "0",
            "alerts_passed_total": "0",
            "alerts_rejected": "0",
        }
        mock_get_redis.return_value = mock_redis
        mock_get_recent.return_value = []

        embed = _build_eod_embed()
        fields = {f["name"]: f["value"] for f in embed["embeds"][0]["fields"]}

        assert fields["📈 Pass Rate"] == "N/A"
        assert fields["📣 Alerts"] == "No alerts fired today"
        assert embed["embeds"][0]["color"] == 0xE74C3C

    @patch("eod_summary.datetime")
    @patch("db.get_recent_alerts")
    @patch("redis_client.get_redis")
    def test_date_boundary_filters_alerts(
        self,
        mock_get_redis: MagicMock,
        mock_get_recent: MagicMock,
        mock_datetime: MagicMock,
    ) -> None:
        mock_datetime.now.return_value = datetime(2026, 5, 22, 20, 15, tzinfo=_ET)
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
        mock_redis = MagicMock()
        mock_redis.hgetall.return_value = _session_hash()
        mock_get_redis.return_value = mock_redis
        mock_get_recent.return_value = [
            {
                "symbol": "AAPL",
                "direction": "LONG",
                "edge_probability": 0.8,
                "confidence": 0.85,
                "outcome": "open",
                "created_at": f"{_TODAY}T14:00:00+00:00",
            },
            {
                "symbol": "MSFT",
                "direction": "SHORT",
                "edge_probability": 0.7,
                "confidence": 0.75,
                "outcome": "open",
                "created_at": "2026-05-21T14:00:00+00:00",
            },
        ]

        embed = _build_eod_embed()
        alerts_field = next(f for f in embed["embeds"][0]["fields"] if f["name"] == "📣 Alerts")

        assert "AAPL" in alerts_field["value"]
        assert "MSFT" not in alerts_field["value"]
