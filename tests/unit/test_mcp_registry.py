"""Unit tests for mcp.registry."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from mcp.registry import (
    MCPRegistry,
    get_endpoint,
    get_health_services,
    get_registry,
    reset_workflow_overrides,
)


class TestMCPRegistry:
    def test_default_endpoints_include_timesfm(self) -> None:
        reg = MCPRegistry()
        assert "timesfm-mcp" in reg.endpoints
        assert reg.endpoints["timesfm-mcp"] == os.getenv("TIMESFM_MCP_URL", "http://timesfm-mcp:8012")

    def test_resolve_unknown_returns_none(self) -> None:
        assert get_endpoint("nonexistent-mcp") is None

    def test_workflow_override_precedence(self) -> None:
        reg = MCPRegistry()
        reg.register_workflow_overrides([{"name": "polygon-mcp", "endpoint": "http://yaml-host:8002"}])
        assert reg.resolve("polygon-mcp") == "http://yaml-host:8002"

    def test_env_overrides_yaml_workflow_endpoint(self) -> None:
        reg = MCPRegistry()
        with patch.dict(os.environ, {"POLYGON_MCP_URL": "http://env-host:8002"}):
            reg.register_workflow_overrides([{"name": "polygon-mcp", "endpoint": "http://yaml-host:8002"}])
        assert reg.resolve("polygon-mcp") == "http://env-host:8002"

    def test_reset_workflow_overrides(self) -> None:
        reg = MCPRegistry()
        reg.register_workflow_overrides([{"name": "fred-mcp", "endpoint": "http://custom:8009"}])
        reg.reset_workflow_overrides()
        assert reg.resolve("fred-mcp") == reg.endpoints["fred-mcp"]

    def test_health_services_suffix(self) -> None:
        services = get_health_services()
        assert len(services) == 12
        assert all(url.endswith("/health") for _, url in services)

    def test_singleton_endpoints_shared_with_pipeline_runner(self) -> None:
        from pipeline_runner import MCP_ENDPOINTS

        assert MCP_ENDPOINTS is get_registry().endpoints


@pytest.fixture(autouse=True)
def _reset_overrides() -> None:
    reset_workflow_overrides()
    yield
    reset_workflow_overrides()
