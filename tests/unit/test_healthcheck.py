"""Unit tests for healthcheck.py (SSOT §13)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from healthcheck import MCP_SERVICES, check_mcps


class TestCheckMcps:
    """Tests for the check_mcps() function."""

    def test_all_healthy(self) -> None:
        """All 10 MCPs return 200."""
        mock_resp = MagicMock(status_code=200)
        with patch.object(httpx, "get", return_value=mock_resp):
            healthy, unhealthy = check_mcps()
        assert len(healthy) == 10
        assert unhealthy == []

    def test_all_unreachable(self) -> None:
        """All MCPs raise connection error."""
        with patch.object(httpx, "get", side_effect=httpx.ConnectError("refused")):
            healthy, unhealthy = check_mcps()
        assert healthy == []
        assert len(unhealthy) == 10

    def test_partial_failure(self) -> None:
        """Some MCPs healthy, some not."""

        def _side_effect(url: str, timeout: float = 5.0) -> MagicMock:
            if "8001" in url or "8002" in url:
                raise httpx.ConnectError("refused")
            return MagicMock(status_code=200)

        with patch.object(httpx, "get", side_effect=_side_effect):
            healthy, unhealthy = check_mcps()
        assert len(unhealthy) == 2
        assert "tradingview-mcp" in unhealthy
        assert "polygon-mcp" in unhealthy
        assert len(healthy) == 8

    def test_non_200_counted_as_unhealthy(self) -> None:
        """Non-200 status codes count as unhealthy."""
        mock_resp = MagicMock(status_code=503)
        with patch.object(httpx, "get", return_value=mock_resp):
            healthy, unhealthy = check_mcps()
        assert healthy == []
        assert len(unhealthy) == 10

    def test_timeout_counted_as_unhealthy(self) -> None:
        """Timeout errors count as unhealthy."""
        with patch.object(httpx, "get", side_effect=httpx.ReadTimeout("timeout")):
            healthy, unhealthy = check_mcps()
        assert healthy == []
        assert len(unhealthy) == 10

    def test_custom_timeout_passed(self) -> None:
        """Custom timeout is forwarded to httpx.get."""
        mock_resp = MagicMock(status_code=200)
        with patch.object(httpx, "get", return_value=mock_resp) as mock_get:
            check_mcps(timeout=2.0)
        for call in mock_get.call_args_list:
            assert call.kwargs.get("timeout") == 2.0 or call[1].get("timeout") == 2.0

    def test_mcp_services_has_10_entries(self) -> None:
        """SSOT §3 defines exactly 10 MCP services."""
        assert len(MCP_SERVICES) == 10

    def test_mcp_ports_match_ssot(self) -> None:
        """Verify port assignments match SSOT §3."""
        expected = {
            "tradingview-mcp": 8001,
            "polygon-mcp": 8002,
            "discord-mcp": 8003,
            "finnhub-mcp": 8004,
            "rot-mcp": 8005,
            "crypto-orderbook-mcp": 8006,
            "coingecko-mcp": 8007,
            "trading-mcp": 8008,
            "fred-mcp": 8009,
            "spamshield-mcp": 8010,
        }
        for name, url in MCP_SERVICES:
            port = int(url.split(":")[-1].split("/")[0])
            assert port == expected[name], f"{name} port mismatch"


class TestRunHealthcheck:
    """Tests for run_healthcheck() integration with MCP checks."""

    @patch("healthcheck.check_recent_alerts", return_value=True)
    @patch("healthcheck.check_mcps", return_value=([], ["fred-mcp", "discord-mcp"]))
    @patch("healthcheck.check_postgres", return_value=True)
    @patch("healthcheck.check_redis", return_value=True)
    @patch("healthcheck.send_ops_message")
    def test_mcp_failures_trigger_ops_message(
        self,
        mock_ops: MagicMock,
        mock_redis: MagicMock,
        mock_pg: MagicMock,
        mock_mcps: MagicMock,
        mock_alerts: MagicMock,
    ) -> None:
        """Unhealthy MCPs should trigger an ops alert."""
        from healthcheck import run_healthcheck

        run_healthcheck("15m")
        mock_ops.assert_called_once()
        msg = mock_ops.call_args[0][0]
        assert "fred-mcp" in msg
        assert "discord-mcp" in msg

    @patch("healthcheck.check_recent_alerts", return_value=True)
    @patch("healthcheck.check_mcps", return_value=(["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"], []))
    @patch("healthcheck.check_postgres", return_value=True)
    @patch("healthcheck.check_redis", return_value=True)
    @patch("healthcheck.send_ops_message")
    def test_all_healthy_no_ops_message(
        self,
        mock_ops: MagicMock,
        mock_redis: MagicMock,
        mock_pg: MagicMock,
        mock_mcps: MagicMock,
        mock_alerts: MagicMock,
    ) -> None:
        """All green should NOT trigger an ops alert."""
        from healthcheck import run_healthcheck

        run_healthcheck("1h")
        mock_ops.assert_not_called()
