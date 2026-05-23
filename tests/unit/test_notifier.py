"""Unit tests for notifier.py HTTP delivery and ops messaging."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

import notifier as _notifier_mod
from notifier import send_discord_embed, send_ops_embed, send_ops_message


@pytest.fixture(autouse=True)
def _reset_circuit_breaker():
    """Reset Discord circuit breaker between tests."""
    _notifier_mod._discord_consecutive_failures = 0
    _notifier_mod._discord_cb_open_since = 0.0
    yield
    _notifier_mod._discord_consecutive_failures = 0
    _notifier_mod._discord_cb_open_since = 0.0


# ── send_discord_embed ──────────────────────────────────────────


class TestSendDiscordEmbed:
    """Tests for Discord embed delivery (webhook + bot fallback)."""

    @patch("notifier._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier._get_discord_client")
    def test_webhook_success(self, mock_client_fn: MagicMock, _wh: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(status_code=204)
        mock_client.post.return_value.raise_for_status = MagicMock()
        mock_client_fn.return_value = mock_client
        assert send_discord_embed({"embeds": []}) is True
        mock_client.post.assert_called_once()

    @patch("notifier._discord_webhook", return_value=None)
    @patch("notifier._discord_bot_token", return_value="tok123")
    @patch("notifier._discord_alert_channel_id", return_value="chan456")
    @patch("notifier._get_discord_client")
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

    @patch("notifier._discord_webhook", return_value=None)
    @patch("notifier._discord_bot_token", return_value=None)
    def test_no_credentials(self, _bt: MagicMock, _wh: MagicMock) -> None:
        assert send_discord_embed({"embeds": []}) is False

    @patch("notifier._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier._get_discord_client")
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

    @patch("notifier._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier._get_discord_client")
    def test_request_error(self, mock_client_fn: MagicMock, _wh: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.post.side_effect = httpx.RequestError("timeout")
        mock_client_fn.return_value = mock_client
        assert send_discord_embed({"embeds": []}) is False

    @patch("notifier._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier._get_discord_client")
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

    @patch("notifier._discord_webhook", return_value=None)
    @patch("notifier._discord_bot_token", return_value="tok123")
    @patch("notifier._discord_alert_channel_id", return_value="chan456")
    @patch("notifier._get_discord_client")
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

    @patch("notifier._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier._get_discord_client")
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

    @patch("notifier.random.uniform", return_value=0.0)
    @patch("notifier.time.sleep")
    @patch("notifier._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier._get_discord_client")
    def test_retry_on_429_then_success(
        self,
        mock_client_fn: MagicMock,
        _wh: MagicMock,
        mock_sleep: MagicMock,
        _jitter: MagicMock,
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

    @patch("notifier.time.sleep")
    @patch("notifier._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier._get_discord_client")
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

    @patch("notifier.time.sleep")
    @patch("notifier._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier._get_discord_client")
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

    @patch("notifier.time.sleep")
    @patch("notifier._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier._get_discord_client")
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

    @patch("notifier.time.sleep")
    @patch("notifier._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier._get_discord_client")
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

    @patch("notifier.time.sleep")
    @patch("notifier._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier._get_discord_client")
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

    @patch("notifier._discord_bot_token", return_value=None)
    def test_no_config_skips(self, _bt: MagicMock) -> None:
        # Should not raise
        send_ops_message("test")

    @patch("notifier._discord_bot_token", return_value="tok")
    @patch("notifier._discord_ops_channel_id", return_value="ops123")
    @patch("notifier._get_discord_client")
    def test_sends_plain_text(self, mock_client_fn: MagicMock, _ch: MagicMock, _bt: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(status_code=200)
        mock_client.post.return_value.raise_for_status = MagicMock()
        mock_client_fn.return_value = mock_client
        send_ops_message("health OK")
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.kwargs.get("json", call_kwargs[1].get("json", {}))["content"] == "health OK"


# ── send_ops_embed ──────────────────────────────────────────────


class TestSendOpsEmbed:
    """Tests for rich embed delivery to ops channel."""

    @patch("notifier._discord_bot_token", return_value=None)
    def test_no_config_returns_false(self, _bt: MagicMock) -> None:
        assert send_ops_embed({"embeds": []}) is False

    @patch("notifier._discord_bot_token", return_value="tok")
    @patch("notifier._discord_ops_channel_id", return_value="ops123")
    @patch("notifier._get_discord_client")
    def test_sends_embed_payload(self, mock_client_fn: MagicMock, _ch: MagicMock, _bt: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(status_code=200)
        mock_client.post.return_value.raise_for_status = MagicMock()
        mock_client_fn.return_value = mock_client
        payload = {"embeds": [{"title": "test"}]}
        assert send_ops_embed(payload) is True
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.kwargs.get("json", call_kwargs[1].get("json", {})) == payload

    @patch("notifier._discord_bot_token", return_value="tok")
    @patch("notifier._discord_ops_channel_id", return_value="ops123")
    @patch("notifier._get_discord_client")
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

    @patch("notifier._discord_bot_token", return_value="tok")
    @patch("notifier._discord_ops_channel_id", return_value="ops123")
    @patch("notifier._get_discord_client")
    def test_request_error_returns_false(
        self, mock_client_fn: MagicMock, _ch: MagicMock, _bt: MagicMock
    ) -> None:
        mock_client = MagicMock()
        mock_client.post.side_effect = httpx.RequestError("timeout")
        mock_client_fn.return_value = mock_client
        assert send_ops_embed({"embeds": []}) is False


# ── Circuit breaker ─────────────────────────────────────────────


class TestDiscordCircuitBreaker:
    """Discord circuit breaker fast-fails when consecutive failures exceed threshold."""

    @patch("notifier._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier._get_discord_client")
    def test_circuit_breaker_opens_after_threshold(self, mock_client_fn: MagicMock, _wh: MagicMock) -> None:
        _notifier_mod._discord_consecutive_failures = _notifier_mod._DISCORD_CB_THRESHOLD
        _notifier_mod._discord_cb_open_since = time.monotonic()

        result = send_discord_embed({"embeds": []})
        assert result is False
        mock_client_fn.return_value.post.assert_not_called()

        _notifier_mod._discord_consecutive_failures = 0
        _notifier_mod._discord_cb_open_since = 0.0

    @patch("notifier._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier._get_discord_client")
    def test_circuit_breaker_resets_after_cooldown(self, mock_client_fn: MagicMock, _wh: MagicMock) -> None:
        _notifier_mod._discord_consecutive_failures = _notifier_mod._DISCORD_CB_THRESHOLD
        _notifier_mod._discord_cb_open_since = time.monotonic() - _notifier_mod._DISCORD_CB_RESET_SECS - 1.0

        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(status_code=204)
        mock_client.post.return_value.raise_for_status = MagicMock()
        mock_client_fn.return_value = mock_client

        result = send_discord_embed({"embeds": []})
        assert result is True
        assert _notifier_mod._discord_consecutive_failures == 0

        _notifier_mod._discord_cb_open_since = 0.0

    @patch("notifier._discord_webhook", return_value="https://hooks.example.com/wh")
    @patch("notifier._get_discord_client")
    def test_success_resets_circuit_breaker(self, mock_client_fn: MagicMock, _wh: MagicMock) -> None:
        _notifier_mod._discord_consecutive_failures = 1
        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(status_code=204)
        mock_client.post.return_value.raise_for_status = MagicMock()
        mock_client_fn.return_value = mock_client

        result = send_discord_embed({"embeds": []})
        assert result is True
        assert _notifier_mod._discord_consecutive_failures == 0
