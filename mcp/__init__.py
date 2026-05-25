"""MCP endpoint registry for pipeline runner and health checks."""

from mcp.registry import (
    MCPRegistry,
    get_endpoint,
    get_health_services,
    get_registry,
    register_workflow_overrides,
    reset_workflow_overrides,
)

# Backward-compatible module-level dict (same object as registry.endpoints).
MCP_ENDPOINTS = get_registry().endpoints

__all__ = [
    "MCPRegistry",
    "MCP_ENDPOINTS",
    "get_endpoint",
    "get_health_services",
    "get_registry",
    "register_workflow_overrides",
    "reset_workflow_overrides",
]
