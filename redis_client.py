"""Shared Redis client singleton for trade-alert.

Every module that needs Redis imports ``get_redis()`` from here
instead of creating its own connection.  Follows the same pattern as
``langfuse_client.py``.
"""

from __future__ import annotations

import logging
import os
import threading

import redis

logger = logging.getLogger(__name__)

REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379")
REDIS_SOCKET_TIMEOUT: float = float(os.getenv("REDIS_SOCKET_TIMEOUT", "5.0"))

_pool: redis.ConnectionPool | None = None
_lock = threading.Lock()


def get_redis(*, decode_responses: bool = True) -> redis.Redis:
    """Return a :class:`redis.Redis` backed by a module-level pool.

    The pool is created lazily on first call and reused thereafter.

    Args:
        decode_responses: Decode bytes to str (default ``True``).

    Returns:
        A Redis client instance.
    """
    global _pool  # noqa: PLW0603
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = redis.ConnectionPool.from_url(
                    REDIS_URL,
                    decode_responses=decode_responses,
                    socket_timeout=REDIS_SOCKET_TIMEOUT,
                )
    return redis.Redis(connection_pool=_pool)
