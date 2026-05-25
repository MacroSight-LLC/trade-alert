"""Central MCP endpoint registry (SSOT §3 / §13).

Single source for pipeline_runner tool dispatch and healthcheck MCP probes.
Environment variables override Docker Compose defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _default_endpoints() -> dict[str, str]:
    return {
        "tradingview-mcp": os.getenv("TRADINGVIEW_MCP_URL", "http://tradingview-mcp:8001"),
        "polygon-mcp": os.getenv("POLYGON_MCP_URL", "http://polygon-mcp:8002"),
        "discord-mcp": os.getenv("DISCORD_MCP_URL", "http://discord-mcp:8003"),
        "finnhub-mcp": os.getenv("FINNHUB_MCP_URL", "http://finnhub-mcp:8004"),
        "rot-mcp": os.getenv("ROT_MCP_URL", "http://rot-mcp:8005"),
        "edgar-mcp": os.getenv("EDGAR_MCP_URL", "http://edgar-mcp:8006"),
        "yfinance-mcp": os.getenv("YFINANCE_MCP_URL", "http://yfinance-mcp:8007"),
        "trading-mcp": os.getenv("TRADING_MCP_URL", "http://trading-mcp:8008"),
        "fred-mcp": os.getenv("FRED_MCP_URL", "http://fred-mcp:8009"),
        "spamshield-mcp": os.getenv("SPAMSHIELD_MCP_URL", "http://spamshield-mcp:8010"),
        "alpaca-mcp": os.getenv("ALPACA_MCP_URL", "http://alpaca-mcp:8011"),
        "timesfm-mcp": os.getenv("TIMESFM_MCP_URL", "http://timesfm-mcp:8012"),
    }


@dataclass
class MCPRegistry:
    """Resolved MCP base URLs with optional workflow-level overrides."""

    endpoints: dict[str, str] = field(default_factory=_default_endpoints)
    workflow_overrides: dict[str, str] = field(default_factory=dict)

    def resolve(self, tool: str) -> str | None:
        """Return base URL for an MCP tool name, or None if unknown."""
        return self.workflow_overrides.get(tool) or self.endpoints.get(tool)

    def register_workflow_overrides(self, servers: list[dict[str, str]]) -> None:
        """Register YAML ``mcp_servers`` overrides (env vars take precedence)."""
        for srv in servers:
            srv_name = srv["name"]
            if "endpoint" in srv:
                env_key = srv_name.upper().replace("-", "_") + "_URL"
                self.workflow_overrides[srv_name] = os.getenv(env_key, srv["endpoint"])

    def reset_workflow_overrides(self) -> None:
        self.workflow_overrides.clear()

    def health_services(self) -> list[tuple[str, str]]:
        """Return (name, health_url) pairs for healthcheck probes."""
        return [(name, base + "/health") for name, base in self.endpoints.items()]


_registry = MCPRegistry()


def get_registry() -> MCPRegistry:
    return _registry


def get_endpoint(tool: str) -> str | None:
    return _registry.resolve(tool)


def register_workflow_overrides(servers: list[dict[str, str]]) -> None:
    _registry.register_workflow_overrides(servers)


def reset_workflow_overrides() -> None:
    _registry.reset_workflow_overrides()


def get_health_services() -> list[tuple[str, str]]:
    return _registry.health_services()
