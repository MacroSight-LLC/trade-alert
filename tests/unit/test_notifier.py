"""Unit tests for notifier_and_logger.py (SSOT §11)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from models import PlaybookAlert
from notifier_and_logger import compute_rr, format_embed, notify, send_discord_embed, send_ops_message

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
        assert "82.0%" in title

    def test_embed_color_long(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        assert result["embeds"][0]["color"] == 3066993  # green

    def test_embed_color_short(self, mock_alert: PlaybookAlert) -> None:
        mock_alert.direction = "SHORT"
        result = format_embed(mock_alert)
        assert result["embeds"][0]["color"] == 15158332  # red

    def test_embed_fields_count(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        assert len(result["embeds"][0]["fields"]) == 2

    def test_embed_entry_values_in_playbook(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        playbook_value = result["embeds"][0]["fields"][0]["value"]
        assert "$875.0" in playbook_value
        assert "$865.0" in playbook_value
        assert "$900.0" in playbook_value

    def test_embed_rr_in_playbook(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        playbook_value = result["embeds"][0]["fields"][0]["value"]
        assert "2.5:1" in playbook_value

    def test_embed_sources_in_context(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        context_value = result["embeds"][0]["fields"][1]["value"]
        assert "5/10" in context_value

    def test_embed_empty_unusual_activity(self, mock_alert: PlaybookAlert) -> None:
        mock_alert.unusual_activity = []
        result = format_embed(mock_alert)
        context_value = result["embeds"][0]["fields"][1]["value"]
        assert "None" in context_value

    def test_embed_has_timestamp(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        assert "timestamp" in result["embeds"][0]

    def test_embed_has_footer(self, mock_alert: PlaybookAlert) -> None:
        result = format_embed(mock_alert)
        assert "footer" in result["embeds"][0]


# ── send_discord_embed ──────────────────────────────────────────


class TestSendDiscordEmbed:
    """Tests for Discord embed delivery (webhook + bot fallback)."""

    @patch("notifier_and_logger._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier_and_logger.httpx.post")
    def test_webhook_success(self, mock_post: MagicMock, _wh: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=204)
        mock_post.return_value.raise_for_status = MagicMock()
        assert send_discord_embed({"embeds": []}) is True
        mock_post.assert_called_once()

    @patch("notifier_and_logger._discord_webhook", return_value=None)
    @patch("notifier_and_logger._discord_bot_token", return_value="tok123")
    @patch("notifier_and_logger._discord_alert_channel_id", return_value="chan456")
    @patch("notifier_and_logger.httpx.post")
    def test_bot_fallback(self, mock_post: MagicMock, _ch: MagicMock, _bt: MagicMock, _wh: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()
        assert send_discord_embed({"embeds": []}) is True
        call_kwargs = mock_post.call_args
        assert "Bot tok123" in call_kwargs.kwargs.get("headers", call_kwargs[1].get("headers", {})).get(
            "Authorization", ""
        )

    @patch("notifier_and_logger._discord_webhook", return_value=None)
    @patch("notifier_and_logger._discord_bot_token", return_value=None)
    def test_no_credentials(self, _bt: MagicMock, _wh: MagicMock) -> None:
        assert send_discord_embed({"embeds": []}) is False

    @patch("notifier_and_logger._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier_and_logger.httpx.post")
    def test_http_status_error(self, mock_post: MagicMock, _wh: MagicMock) -> None:
        resp = MagicMock()
        resp.status_code = 429
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "rate limited",
            request=MagicMock(),
            response=resp,
        )
        mock_post.return_value = resp
        assert send_discord_embed({"embeds": []}) is False

    @patch("notifier_and_logger._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier_and_logger.httpx.post")
    def test_request_error(self, mock_post: MagicMock, _wh: MagicMock) -> None:
        mock_post.side_effect = httpx.RequestError("timeout")
        assert send_discord_embed({"embeds": []}) is False


# ── send_ops_message ────────────────────────────────────────────


class TestSendOpsMessage:
    """Tests for ops channel messaging."""

    @patch("notifier_and_logger._discord_bot_token", return_value=None)
    def test_no_config_skips(self, _bt: MagicMock) -> None:
        # Should not raise
        send_ops_message("test")

    @patch("notifier_and_logger._discord_bot_token", return_value="tok")
    @patch("notifier_and_logger._discord_ops_channel_id", return_value="ops123")
    @patch("notifier_and_logger.httpx.post")
    def test_sends_plain_text(self, mock_post: MagicMock, _ch: MagicMock, _bt: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()
        send_ops_message("health OK")
        call_kwargs = mock_post.call_args
        assert call_kwargs.kwargs.get("json", call_kwargs[1].get("json", {}))["content"] == "health OK"


# ── notify (end-to-end with mocks) ─────────────────────────────


class TestNotify:
    """Tests for the main notify() entry point."""

    @patch("notifier_and_logger.insert_alert")
    @patch("notifier_and_logger.send_discord_embed", return_value=True)
    def test_valid_json_sends_and_logs(
        self, _send: MagicMock, _insert: MagicMock, sample_alert: PlaybookAlert
    ) -> None:
        alerts_json = json.dumps([sample_alert.model_dump()])
        count = notify(alerts_json, [{"raw": "snap"}])
        assert count == 1
        _send.assert_called_once()
        _insert.assert_called_once()

    @patch("notifier_and_logger.insert_alert")
    @patch("notifier_and_logger.send_discord_embed", return_value=False)
    def test_discord_failure_still_logs(
        self, _send: MagicMock, _insert: MagicMock, sample_alert: PlaybookAlert
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

    @patch("notifier_and_logger.insert_alert", side_effect=Exception("DB down"))
    @patch("notifier_and_logger.send_discord_embed", return_value=True)
    def test_db_error_still_counts_send(
        self, _send: MagicMock, _insert: MagicMock, sample_alert: PlaybookAlert
    ) -> None:
        alerts_json = json.dumps([sample_alert.model_dump()])
        count = notify(alerts_json)
        assert count == 1

    @patch("notifier_and_logger.insert_alert")
    @patch("notifier_and_logger.send_discord_embed", return_value=True)
    def test_multiple_alerts(self, _send: MagicMock, _insert: MagicMock, sample_alert: PlaybookAlert) -> None:
        alerts_json = json.dumps([sample_alert.model_dump(), sample_alert.model_dump()])
        count = notify(alerts_json)
        assert count == 2
        assert _send.call_count == 2
