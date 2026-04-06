"""Shared Redis client singleton for trade-alert.

Every module that needs Redis imports ``get_redis()`` from here
instead of creating its own connection.  Follows the same pattern as
``langfuse_client.py``.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import redis

logger = logging.getLogger(__name__)

REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379")
REDIS_SOCKET_TIMEOUT: float = float(os.getenv("REDIS_SOCKET_TIMEOUT", "5.0"))
REDIS_RETRY_ATTEMPTS: int = int(os.getenv("REDIS_RETRY_ATTEMPTS", "3"))
REDIS_RETRY_BACKOFF: float = float(os.getenv("REDIS_RETRY_BACKOFF", "0.5"))

_pool: redis.ConnectionPool | None = None
_lock = threading.Lock()


def get_redis(*, decode_responses: bool = True) -> redis.Redis:
    """Return a :class:`redis.Redis` backed by a module-level pool.

    The pool is created lazily on first call and reused thereafter.
    Includes retry logic with exponential backoff for transient
    connection failures during pool creation.

    Args:
        decode_responses: Decode bytes to str (default ``True``).

    Returns:
        A Redis client instance.
    """
    global _pool  # noqa: PLW0603
    if _pool is None:
        with _lock:
            if _pool is None:
                last_exc: redis.RedisError | None = None
                for attempt in range(1, REDIS_RETRY_ATTEMPTS + 1):
                    try:
                        pool = redis.ConnectionPool.from_url(
                            REDIS_URL,
                            decode_responses=decode_responses,
                            socket_timeout=REDIS_SOCKET_TIMEOUT,
                            socket_connect_timeout=REDIS_SOCKET_TIMEOUT,
                            retry_on_timeout=True,
                        )
                        # Verify connectivity
                        test_client = redis.Redis(connection_pool=pool)
                        test_client.ping()
                        _pool = pool
                        break
                    except (redis.ConnectionError, redis.TimeoutError) as exc:
                        last_exc = exc
                        if attempt < REDIS_RETRY_ATTEMPTS:
                            delay = REDIS_RETRY_BACKOFF * (2 ** (attempt - 1))
                            logger.warning(
                                "Redis connection attempt %d/%d failed: %s, retrying in %.1fs",
                                attempt,
                                REDIS_RETRY_ATTEMPTS,
                                exc,
                                delay,
                            )
                            time.sleep(delay)
                        else:
                            logger.error(
                                "Redis connection failed after %d attempts: %s",
                                attempt,
                                exc,
                            )
                            raise last_exc  # type: ignore[misc]
    return redis.Redis(connection_pool=_pool)
