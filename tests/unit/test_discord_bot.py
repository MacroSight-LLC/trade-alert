"""Unit tests for discord_bot.py — command handling and helper functions."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# _run_pipeline
# ---------------------------------------------------------------------------


class TestRunPipeline:
    """Tests for _run_pipeline subprocess invocation."""

    @patch("discord_bot.subprocess.run")
    def test_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
        from discord_bot import _run_pipeline

        ok, summary = _run_pipeline("15m")
        assert ok is True
        assert "Pipeline 15m completed" in summary
        mock_run.assert_called_once()
        # Verify correct workflow path is passed
        cmd = mock_run.call_args[0][0]
        assert "orchestrator-15m.yaml" in " ".join(cmd)

    @patch("discord_bot.subprocess.run")
    def test_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="boom")
        from discord_bot import _run_pipeline

        ok, summary = _run_pipeline("1h")
        assert ok is False

    @patch("discord_bot.subprocess.run")
    def test_timeout(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=300)
        from discord_bot import _run_pipeline

        ok, summary = _run_pipeline("15m")
        assert ok is False
        assert "timed out" in summary.lower()


# ---------------------------------------------------------------------------
# _get_status
# ---------------------------------------------------------------------------


class TestGetStatus:
    """Tests for _get_status Redis / MCP health summary."""

    @patch("redis_client.get_redis", side_effect=__import__("redis").RedisError("connection refused"))
    @patch("discord_bot.httpx.Client")
    def test_status_with_redis_down(self, mock_client_cls: MagicMock, _mock_redis: MagicMock) -> None:
        """When Redis is unreachable, status should still return a string."""
        from discord_bot import _get_status

        result = _get_status()
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# _handle_command
# ---------------------------------------------------------------------------


class TestHandleCommand:
    """Tests for command dispatch."""

    @patch("discord_bot._send_message")
    def test_help_command(self, mock_send: MagicMock) -> None:
        from discord_bot import _handle_command

        _handle_command("!help", "123456")
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][1]
        assert "!scan" in msg
        assert "!status" in msg

    @patch("discord_bot._send_message")
    @patch("discord_bot._run_pipeline", return_value=(True, "Pipeline finished"))
    def test_scan_command(self, mock_run: MagicMock, mock_send: MagicMock) -> None:
        from discord_bot import _handle_command

        _handle_command("!scan 1h", "123456")
        mock_run.assert_called_once_with("1h")

    @patch("discord_bot._send_message")
    def test_unknown_command(self, mock_send: MagicMock) -> None:
        from discord_bot import _handle_command

        _handle_command("!unknown", "123456")
        # Unknown commands are silently ignored (no matching branch)
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    """Tests for the shutdown mechanism."""

    def test_shutdown_event_exists(self) -> None:
        from discord_bot import _shutdown

        assert not _shutdown.is_set()

    def test_shutdown_handler_sets_event(self) -> None:
        from discord_bot import _shutdown, _shutdown_handler

        _shutdown.clear()
        _shutdown_handler(15, None)
        assert _shutdown.is_set()
        _shutdown.clear()  # reset for other tests
