"""Unit tests for notifier_and_logger orchestration (notify, dedup)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("redis", reason="redis not installed")

from models import PlaybookAlert
from notifier_and_logger import _thesis_similarity, notify




# ── notify (end-to-end with mocks) ─────────────────────────────


class TestNotify:
    """Tests for the main notify() entry point."""

    @patch("notifier_and_logger.generate_chart", return_value=(None, None))
    @patch("notifier_and_logger._is_duplicate_alert", return_value=False)
    @patch("alert_logger.insert_alert")
    @patch("notifier_and_logger.send_discord_embed", return_value=True)
    def test_valid_json_sends_and_logs(
        self,
        _send: MagicMock,
        _insert: MagicMock,
        _dedup: MagicMock,
        _chart: MagicMock,
        sample_alert: PlaybookAlert,
    ) -> None:
        alerts_json = json.dumps([sample_alert.model_dump()])
        count = notify(alerts_json, [{"raw": "snap"}])
        assert count == 1
        _send.assert_called_once()
        _insert.assert_called_once()

    @patch("notifier_and_logger.generate_chart", return_value=(None, None))
    @patch("notifier_and_logger._is_duplicate_alert", return_value=False)
    @patch("alert_logger.insert_alert")
    @patch("notifier_and_logger.send_discord_embed", return_value=False)
    def test_discord_failure_still_logs(
        self,
        _send: MagicMock,
        _insert: MagicMock,
        _dedup: MagicMock,
        _chart: MagicMock,
        sample_alert: PlaybookAlert,
    ) -> None:
        alerts_json = json.dumps([sample_alert.model_dump()])
        count = notify(alerts_json)
        assert count == 0
        _insert.assert_called_once()

    def test_invalid_json_returns_zero(self) -> None:
        assert notify("not-json") == 0

    def test_non_list_json_returns_zero(self) -> None:
        assert notify('{"single": "object"}') == 0

    def test_empty_list(self) -> None:
        assert notify("[]") == 0

    @patch("notifier_and_logger.generate_chart", return_value=(None, None))
    @patch("notifier_and_logger._is_duplicate_alert", return_value=False)
    @patch("alert_logger.insert_alert", side_effect=__import__("psycopg2").Error("DB down"))
    @patch("notifier_and_logger.send_discord_embed", return_value=True)
    def test_db_error_skips_discord_send(
        self,
        _send: MagicMock,
        _insert: MagicMock,
        _dedup: MagicMock,
        _chart: MagicMock,
        sample_alert: PlaybookAlert,
    ) -> None:
        alerts_json = json.dumps([sample_alert.model_dump()])
        count = notify(alerts_json)
        # Persist-first ordering: DB failure → skip Discord entirely
        assert count == 0
        _send.assert_not_called()

    @patch("notifier_and_logger.generate_chart", return_value=(None, None))
    @patch("notifier_and_logger._is_duplicate_alert", return_value=False)
    @patch("alert_logger.insert_alert")
    @patch("notifier_and_logger.send_discord_embed", return_value=True)
    def test_multiple_alerts(
        self,
        _send: MagicMock,
        _insert: MagicMock,
        _dedup: MagicMock,
        _chart: MagicMock,
        sample_alert: PlaybookAlert,
    ) -> None:
        alerts_json = json.dumps([sample_alert.model_dump(), sample_alert.model_dump()])
        count = notify(alerts_json)
        assert count == 2
        assert _send.call_count == 2

    @patch("notifier_and_logger.generate_chart", return_value=(b"\x89PNG chart", 2.35, None, None))
    @patch("notifier_and_logger._is_duplicate_alert", return_value=False)
    @patch("alert_logger.insert_alert")
    @patch("notifier_and_logger.send_discord_embed", return_value=True)
    def test_chart_bytes_passed_to_send(
        self,
        mock_send: MagicMock,
        _insert: MagicMock,
        _dedup: MagicMock,
        _chart: MagicMock,
        sample_alert: PlaybookAlert,
    ) -> None:
        alerts_json = json.dumps([sample_alert.model_dump()])
        notify(alerts_json)
        call_kwargs = mock_send.call_args
        assert call_kwargs.kwargs["chart_png"] == b"\x89PNG chart"

    @patch("notifier_and_logger.generate_chart", return_value=(None, None))
    @patch("notifier_and_logger._is_duplicate_alert", return_value=False)
    @patch("alert_logger.insert_alert")
    @patch("notifier_and_logger.send_discord_embed", return_value=True)
    def test_no_image_field_when_chart_fails(
        self,
        mock_send: MagicMock,
        _insert: MagicMock,
        _dedup: MagicMock,
        _chart: MagicMock,
        sample_alert: PlaybookAlert,
    ) -> None:
        alerts_json = json.dumps([sample_alert.model_dump()])
        notify(alerts_json)
        call_kwargs = mock_send.call_args
        assert call_kwargs.kwargs["chart_png"] is None
        embed_arg = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("embed_payload")
        assert "image" not in embed_arg["embeds"][0]

    @patch("alert_logger.insert_alert")
    @patch("notifier_and_logger.send_discord_embed", return_value=True)
    def test_non_dict_item_skipped(self, _send: MagicMock, _insert: MagicMock) -> None:
        alerts_json = json.dumps(["not-a-dict", 42])
        count = notify(alerts_json)
        assert count == 0
        _send.assert_not_called()




# ── _thesis_similarity ──────────────────────────────────────────


class TestThesisSimilarity:
    """Tests for Jaccard similarity of thesis word sets."""

    def test_identical_theses(self) -> None:
        assert _thesis_similarity("RSI breakout above support", "RSI breakout above support") == 1.0

    def test_completely_different(self) -> None:
        sim = _thesis_similarity("RSI breakout momentum", "earnings catalyst rotation")
        assert sim == 0.0

    def test_partial_overlap(self) -> None:
        sim = _thesis_similarity(
            "RSI breakout above support with volume",
            "RSI breakout below resistance with volume",
        )
        assert 0.0 < sim < 1.0

    def test_empty_first(self) -> None:
        assert _thesis_similarity("", "some thesis content") == 0.0

    def test_empty_second(self) -> None:
        assert _thesis_similarity("some thesis content", "") == 0.0

    def test_both_empty(self) -> None:
        assert _thesis_similarity("", "") == 0.0

    def test_case_insensitive(self) -> None:
        assert _thesis_similarity("RSI Breakout", "rsi breakout") == 1.0

    def test_high_similarity_above_threshold(self) -> None:
        # Same thesis with minor word swap → should be > 0.5
        a = "RSI divergence at support with bollinger squeeze and volume spike"
        b = "RSI divergence at support with bollinger squeeze and momentum spike"
        sim = _thesis_similarity(a, b)
        assert sim >= 0.5




# ── Content-aware dedup ─────────────────────────────────────────


class TestContentAwareDedup:
    """Tests for _is_duplicate_alert with thesis similarity."""

    @patch("notifier_and_logger.get_redis")
    def test_first_alert_not_duplicate(self, mock_get_redis: MagicMock) -> None:
        from notifier_and_logger import _is_duplicate_alert

        mock_conn = MagicMock()
        mock_conn.exists.return_value = False
        mock_get_redis.return_value = mock_conn

        result = _is_duplicate_alert("AAPL", "LONG", "15m", "RSI breakout")
        assert result is False
        mock_conn.set.assert_called()

    @patch("notifier_and_logger.get_redis")
    def test_exact_duplicate_suppressed(self, mock_get_redis: MagicMock) -> None:
        from notifier_and_logger import _is_duplicate_alert

        mock_conn = MagicMock()
        # NX=True SET returns None when key already exists
        mock_conn.set.return_value = None
        mock_conn.get.return_value = "RSI breakout above support with volume"
        mock_get_redis.return_value = mock_conn

        result = _is_duplicate_alert(
            "AAPL",
            "LONG",
            "15m",
            "RSI breakout above support with volume",
        )
        assert result is True

    @patch("notifier_and_logger.get_redis")
    def test_different_thesis_allowed_through(self, mock_get_redis: MagicMock) -> None:
        from notifier_and_logger import _is_duplicate_alert

        mock_conn = MagicMock()
        mock_conn.set.return_value = None  # key already exists
        mock_conn.get.return_value = "RSI breakout above support with volume"
        mock_get_redis.return_value = mock_conn

        # Completely different thesis → similarity < 0.5 → allow through
        result = _is_duplicate_alert(
            "AAPL",
            "LONG",
            "15m",
            "Earnings catalyst with sector rotation and dark pool activity",
        )
        assert result is False

    @patch("notifier_and_logger.get_redis")
    def test_no_thesis_defaults_to_suppress(self, mock_get_redis: MagicMock) -> None:
        from notifier_and_logger import _is_duplicate_alert

        mock_conn = MagicMock()
        mock_conn.set.return_value = None  # key already exists
        mock_get_redis.return_value = mock_conn

        # No thesis → suppress as normal dedup
        result = _is_duplicate_alert("AAPL", "LONG", "15m")
        assert result is True

    @patch("notifier_and_logger.get_redis")
    def test_redis_error_allows_through(self, mock_get_redis: MagicMock) -> None:
        import redis as _test_redis

        from notifier_and_logger import _is_duplicate_alert

        mock_get_redis.side_effect = _test_redis.RedisError("gone")

        result = _is_duplicate_alert("AAPL", "LONG", "15m", "thesis")
        assert result is False
