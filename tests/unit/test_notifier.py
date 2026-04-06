"""Unit tests for notifier_and_logger.py (SSOT §11)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from models import PlaybookAlert
from notifier_and_logger import (
    _score_bar,
    _thesis_similarity,
    compute_rr,
    format_embed,
    notify,
    send_discord_embed,
    send_ops_embed,
    send_ops_message,
)

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
        assert result["embeds"][0]["color"] == 3066993  # green

    def test_embed_color_short(self, mock_alert: PlaybookAlert) -> None:
        mock_alert.direction = "SHORT"
        result = format_embed(mock_alert)
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


# ── send_discord_embed ──────────────────────────────────────────


class TestSendDiscordEmbed:
    """Tests for Discord embed delivery (webhook + bot fallback)."""

    @patch("notifier_and_logger._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier_and_logger._get_discord_client")
    def test_webhook_success(self, mock_client_fn: MagicMock, _wh: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(status_code=204)
        mock_client.post.return_value.raise_for_status = MagicMock()
        mock_client_fn.return_value = mock_client
        assert send_discord_embed({"embeds": []}) is True
        mock_client.post.assert_called_once()

    @patch("notifier_and_logger._discord_webhook", return_value=None)
    @patch("notifier_and_logger._discord_bot_token", return_value="tok123")
    @patch("notifier_and_logger._discord_alert_channel_id", return_value="chan456")
    @patch("notifier_and_logger._get_discord_client")
    def test_bot_fallback(
        self, mock_client_fn: MagicMock, _ch: MagicMock, _bt: MagicMock, _wh: MagicMock
    ) -> None:
        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(status_code=200)
        mock_client.post.return_value.raise_for_status = MagicMock()
        mock_client_fn.return_value = mock_client
        assert send_discord_embed({"embeds": []}) is True
        call_kwargs = mock_client.post.call_args
        assert "Bot tok123" in call_kwargs.kwargs.get("headers", call_kwargs[1].get("headers", {})).get(
            "Authorization", ""
        )

    @patch("notifier_and_logger._discord_webhook", return_value=None)
    @patch("notifier_and_logger._discord_bot_token", return_value=None)
    def test_no_credentials(self, _bt: MagicMock, _wh: MagicMock) -> None:
        assert send_discord_embed({"embeds": []}) is False

    @patch("notifier_and_logger._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier_and_logger._get_discord_client")
    def test_http_status_error(self, mock_client_fn: MagicMock, _wh: MagicMock) -> None:
        mock_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 429
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "rate limited",
            request=MagicMock(),
            response=resp,
        )
        mock_client.post.return_value = resp
        mock_client_fn.return_value = mock_client
        assert send_discord_embed({"embeds": []}) is False

    @patch("notifier_and_logger._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier_and_logger._get_discord_client")
    def test_request_error(self, mock_client_fn: MagicMock, _wh: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.post.side_effect = httpx.RequestError("timeout")
        mock_client_fn.return_value = mock_client
        assert send_discord_embed({"embeds": []}) is False

    @patch("notifier_and_logger._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier_and_logger._get_discord_client")
    def test_webhook_with_chart_uses_multipart(self, mock_client_fn: MagicMock, _wh: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(status_code=204)
        mock_client.post.return_value.raise_for_status = MagicMock()
        mock_client_fn.return_value = mock_client
        chart_bytes = b"\x89PNG fake chart"
        assert send_discord_embed({"embeds": []}, chart_png=chart_bytes) is True
        call_kwargs = mock_client.post.call_args
        assert "data" in call_kwargs.kwargs or "data" in (call_kwargs[1] if len(call_kwargs) > 1 else {})
        assert "files" in call_kwargs.kwargs or "files" in (call_kwargs[1] if len(call_kwargs) > 1 else {})

    @patch("notifier_and_logger._discord_webhook", return_value=None)
    @patch("notifier_and_logger._discord_bot_token", return_value="tok123")
    @patch("notifier_and_logger._discord_alert_channel_id", return_value="chan456")
    @patch("notifier_and_logger._get_discord_client")
    def test_bot_with_chart_uses_multipart(
        self, mock_client_fn: MagicMock, _ch: MagicMock, _bt: MagicMock, _wh: MagicMock
    ) -> None:
        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(status_code=200)
        mock_client.post.return_value.raise_for_status = MagicMock()
        mock_client_fn.return_value = mock_client
        chart_bytes = b"\x89PNG fake chart"
        assert send_discord_embed({"embeds": []}, chart_png=chart_bytes) is True
        call_kwargs = mock_client.post.call_args
        assert "data" in call_kwargs.kwargs
        assert "files" in call_kwargs.kwargs

    @patch("notifier_and_logger._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier_and_logger._get_discord_client")
    def test_webhook_without_chart_uses_json(self, mock_client_fn: MagicMock, _wh: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(status_code=204)
        mock_client.post.return_value.raise_for_status = MagicMock()
        mock_client_fn.return_value = mock_client
        assert send_discord_embed({"embeds": []}) is True
        call_kwargs = mock_client.post.call_args
        assert "json" in call_kwargs.kwargs


# ── Discord retry logic ─────────────────────────────────────────


class TestSendDiscordEmbedRetry:
    """Tests for exponential backoff retry on transient Discord errors."""

    @patch("notifier_and_logger.time.sleep")
    @patch("notifier_and_logger._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier_and_logger._get_discord_client")
    def test_retry_on_429_then_success(
        self, mock_client_fn: MagicMock, _wh: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """First call gets 429, second succeeds — should return True."""
        mock_client = MagicMock()
        resp_429 = MagicMock(status_code=429)
        resp_429.raise_for_status.side_effect = httpx.HTTPStatusError(
            "rate limited",
            request=MagicMock(),
            response=resp_429,
        )
        resp_ok = MagicMock(status_code=204)
        resp_ok.raise_for_status = MagicMock()
        mock_client.post.side_effect = [resp_429, resp_ok]
        mock_client_fn.return_value = mock_client
        assert send_discord_embed({"embeds": []}) is True
        assert mock_client.post.call_count == 2
        mock_sleep.assert_called_once_with(1.0)

    @patch("notifier_and_logger.time.sleep")
    @patch("notifier_and_logger._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier_and_logger._get_discord_client")
    def test_retry_on_502_then_success(
        self, mock_client_fn: MagicMock, _wh: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """502 on first attempt, success on second."""
        mock_client = MagicMock()
        resp_502 = MagicMock(status_code=502)
        resp_502.raise_for_status.side_effect = httpx.HTTPStatusError(
            "bad gateway",
            request=MagicMock(),
            response=resp_502,
        )
        resp_ok = MagicMock(status_code=200)
        resp_ok.raise_for_status = MagicMock()
        mock_client.post.side_effect = [resp_502, resp_ok]
        mock_client_fn.return_value = mock_client
        assert send_discord_embed({"embeds": []}) is True
        mock_sleep.assert_called_once_with(1.0)

    @patch("notifier_and_logger.time.sleep")
    @patch("notifier_and_logger._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier_and_logger._get_discord_client")
    def test_exhausts_retries_on_persistent_429(
        self, mock_client_fn: MagicMock, _wh: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """All 3 attempts get 429 — should return False after exhausting retries."""
        mock_client = MagicMock()
        resp_429 = MagicMock(status_code=429)
        resp_429.raise_for_status.side_effect = httpx.HTTPStatusError(
            "rate limited",
            request=MagicMock(),
            response=resp_429,
        )
        mock_client.post.return_value = resp_429
        mock_client_fn.return_value = mock_client
        assert send_discord_embed({"embeds": []}) is False
        assert mock_client.post.call_count == 3
        # Backoff: 1s then 2s (no sleep on last failure)
        assert mock_sleep.call_count == 2

    @patch("notifier_and_logger.time.sleep")
    @patch("notifier_and_logger._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier_and_logger._get_discord_client")
    def test_no_retry_on_4xx_non_429(
        self, mock_client_fn: MagicMock, _wh: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """A 403 should fail immediately without retry."""
        mock_client = MagicMock()
        resp_403 = MagicMock(status_code=403)
        resp_403.raise_for_status.side_effect = httpx.HTTPStatusError(
            "forbidden",
            request=MagicMock(),
            response=resp_403,
        )
        mock_client.post.return_value = resp_403
        mock_client_fn.return_value = mock_client
        assert send_discord_embed({"embeds": []}) is False
        assert mock_client.post.call_count == 1
        mock_sleep.assert_not_called()

    @patch("notifier_and_logger.time.sleep")
    @patch("notifier_and_logger._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier_and_logger._get_discord_client")
    def test_retry_on_request_error_then_success(
        self, mock_client_fn: MagicMock, _wh: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """Network error on first attempt, success on second."""
        mock_client = MagicMock()
        resp_ok = MagicMock(status_code=200)
        resp_ok.raise_for_status = MagicMock()
        mock_client.post.side_effect = [httpx.RequestError("timeout"), resp_ok]
        mock_client_fn.return_value = mock_client
        assert send_discord_embed({"embeds": []}) is True
        mock_sleep.assert_called_once_with(1.0)

    @patch("notifier_and_logger.time.sleep")
    @patch("notifier_and_logger._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier_and_logger._get_discord_client")
    def test_backoff_doubling(self, mock_client_fn: MagicMock, _wh: MagicMock, mock_sleep: MagicMock) -> None:
        """Verify exponential backoff delays: 1s, 2s."""
        mock_client = MagicMock()
        resp_500 = MagicMock(status_code=500)
        resp_500.raise_for_status.side_effect = httpx.HTTPStatusError(
            "server error",
            request=MagicMock(),
            response=resp_500,
        )
        mock_client.post.return_value = resp_500
        mock_client_fn.return_value = mock_client
        send_discord_embed({"embeds": []})
        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert delays == [1.0, 2.0]


# ── send_ops_message ────────────────────────────────────────────


class TestSendOpsMessage:
    """Tests for ops channel messaging."""

    @patch("notifier_and_logger._discord_bot_token", return_value=None)
    def test_no_config_skips(self, _bt: MagicMock) -> None:
        # Should not raise
        send_ops_message("test")

    @patch("notifier_and_logger._discord_bot_token", return_value="tok")
    @patch("notifier_and_logger._discord_ops_channel_id", return_value="ops123")
    @patch("notifier_and_logger._get_discord_client")
    def test_sends_plain_text(self, mock_client_fn: MagicMock, _ch: MagicMock, _bt: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(status_code=200)
        mock_client.post.return_value.raise_for_status = MagicMock()
        mock_client_fn.return_value = mock_client
        send_ops_message("health OK")
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.kwargs.get("json", call_kwargs[1].get("json", {}))["content"] == "health OK"


# ── notify (end-to-end with mocks) ─────────────────────────────


class TestNotify:
    """Tests for the main notify() entry point."""

    @patch("notifier_and_logger.generate_chart", return_value=None)
    @patch("notifier_and_logger._is_duplicate_alert", return_value=False)
    @patch("notifier_and_logger.insert_alert")
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

    @patch("notifier_and_logger.generate_chart", return_value=None)
    @patch("notifier_and_logger._is_duplicate_alert", return_value=False)
    @patch("notifier_and_logger.insert_alert")
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

    @patch("notifier_and_logger.generate_chart", return_value=None)
    @patch("notifier_and_logger._is_duplicate_alert", return_value=False)
    @patch("notifier_and_logger.insert_alert", side_effect=Exception("DB down"))
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

    @patch("notifier_and_logger.generate_chart", return_value=None)
    @patch("notifier_and_logger._is_duplicate_alert", return_value=False)
    @patch("notifier_and_logger.insert_alert")
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

    @patch("notifier_and_logger.generate_chart", return_value=b"\x89PNG chart")
    @patch("notifier_and_logger._is_duplicate_alert", return_value=False)
    @patch("notifier_and_logger.insert_alert")
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
        # Verify image field was injected into the embed
        embed_arg = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("embed_payload")
        assert embed_arg["embeds"][0]["image"] == {"url": "attachment://chart.png"}

    @patch("notifier_and_logger.generate_chart", return_value=None)
    @patch("notifier_and_logger._is_duplicate_alert", return_value=False)
    @patch("notifier_and_logger.insert_alert")
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

    @patch("notifier_and_logger.insert_alert")
    @patch("notifier_and_logger.send_discord_embed", return_value=True)
    def test_non_dict_item_skipped(self, _send: MagicMock, _insert: MagicMock) -> None:
        alerts_json = json.dumps(["not-a-dict", 42])
        count = notify(alerts_json)
        assert count == 0
        _send.assert_not_called()


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


# ── send_ops_embed ──────────────────────────────────────────────


class TestSendOpsEmbed:
    """Tests for rich embed delivery to ops channel."""

    @patch("notifier_and_logger._discord_bot_token", return_value=None)
    def test_no_config_returns_false(self, _bt: MagicMock) -> None:
        assert send_ops_embed({"embeds": []}) is False

    @patch("notifier_and_logger._discord_bot_token", return_value="tok")
    @patch("notifier_and_logger._discord_ops_channel_id", return_value="ops123")
    @patch("notifier_and_logger._get_discord_client")
    def test_sends_embed_payload(self, mock_client_fn: MagicMock, _ch: MagicMock, _bt: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(status_code=200)
        mock_client.post.return_value.raise_for_status = MagicMock()
        mock_client_fn.return_value = mock_client
        payload = {"embeds": [{"title": "test"}]}
        assert send_ops_embed(payload) is True
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.kwargs.get("json", call_kwargs[1].get("json", {})) == payload

    @patch("notifier_and_logger._discord_bot_token", return_value="tok")
    @patch("notifier_and_logger._discord_ops_channel_id", return_value="ops123")
    @patch("notifier_and_logger._get_discord_client")
    def test_http_error_returns_false(
        self, mock_client_fn: MagicMock, _ch: MagicMock, _bt: MagicMock
    ) -> None:
        mock_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 500
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "server error", request=MagicMock(), response=resp
        )
        mock_client.post.return_value = resp
        mock_client_fn.return_value = mock_client
        assert send_ops_embed({"embeds": []}) is False

    @patch("notifier_and_logger._discord_bot_token", return_value="tok")
    @patch("notifier_and_logger._discord_ops_channel_id", return_value="ops123")
    @patch("notifier_and_logger._get_discord_client")
    def test_request_error_returns_false(
        self, mock_client_fn: MagicMock, _ch: MagicMock, _bt: MagicMock
    ) -> None:
        mock_client = MagicMock()
        mock_client.post.side_effect = httpx.RequestError("timeout")
        mock_client_fn.return_value = mock_client
        assert send_ops_embed({"embeds": []}) is False


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
