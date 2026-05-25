"""Compatibility shim — use ``telemetry.client`` for new code."""

from telemetry.client import (  # noqa: F401
    get_client,
    get_langfuse_client,
    is_langfuse_auth_error,
    register_langfuse_failure,
    reset_client,
)

__all__ = [
    "get_client",
    "get_langfuse_client",
    "is_langfuse_auth_error",
    "register_langfuse_failure",
    "reset_client",
]
