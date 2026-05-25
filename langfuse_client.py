"""Compatibility shim — deprecated; import from ``telemetry.client`` instead.

.. deprecated::
    Use ``from telemetry import get_client`` (or ``telemetry.client``) in new code.
    This module remains for existing callers until migration completes.
"""

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
