"""Shared Langfuse SDK client singleton for trade-alert.

Every module that needs the Langfuse API imports
``get_langfuse_client()`` from here instead of creating its own
connection.  Returns ``None`` when credentials are absent so callers
can degrade gracefully.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from typing import TYPE_CHECKING

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ

if TYPE_CHECKING:
    from langfuse import Langfuse

logger = logging.getLogger(__name__)

_client: Langfuse | None = None
_initialised: bool = False
_lock = threading.Lock()


def get_langfuse_client() -> Langfuse | None:
    """Return a shared :class:`Langfuse` instance, or ``None``.

    The client is created lazily on first call and reused thereafter.
    If ``LANGFUSE_PUBLIC_KEY`` or ``LANGFUSE_SECRET_KEY`` are missing
    the function returns ``None`` (no error raised).

    Returns:
        A configured ``Langfuse`` client, or ``None`` when credentials
        are not available.
    """
    global _client, _initialised  # noqa: PLW0603

    with _lock:
        if _initialised:
            return _client

        _initialised = True

        public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
        host = os.getenv("LANGFUSE_HOST", "http://localhost:3000")

        if not public_key or not secret_key:
            logger.info("Langfuse credentials not set — client disabled")
            return None

        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                from langfuse import Langfuse

                _client = Langfuse(
                    public_key=public_key,
                    secret_key=secret_key,
                    host=host,
                )
                atexit.register(_shutdown_client)
                logger.info("Langfuse client initialised (host=%s)", host)
                return _client
            except ImportError:
                logger.warning("langfuse package not installed — client disabled")
                return None
            except (ConnectionError, OSError, ValueError, RuntimeError) as exc:
                last_exc = exc
                if attempt < 3:
                    import time

                    backoff = 2**attempt
                    logger.warning(
                        "Langfuse init attempt %d/3 failed (%s), retrying in %ds",
                        attempt,
                        exc,
                        backoff,
                    )
                    time.sleep(backoff)

        logger.warning("Failed to initialise Langfuse client after 3 attempts: %s", last_exc)
        _client = None
        return _client


def _shutdown_client() -> None:
    """Gracefully shut down the Langfuse client at process exit."""
    if _client is not None:
        try:
            _client.shutdown()
        except (OSError, RuntimeError):
            pass


def reset_client() -> None:
    """Reset the cached client (useful for testing)."""
    global _client, _initialised  # noqa: PLW0603
    if _client is not None:
        try:
            _client.shutdown()
        except (OSError, RuntimeError):
            pass
    _client = None
    _initialised = False
