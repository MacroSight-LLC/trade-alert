"""Unit-test environment setup.

Sets environment variables **before** any production module is imported so
that module-level constants pick up the test-safe values.

- MARKET_HOURS_GATES_ENABLED=0  — disables the market-session-closed gate
  so that tests are not time-of-day dependent.
- REDIS_RETRY_ATTEMPTS=1        — fail fast when Redis is unavailable
  instead of burning the 0.5 s + 1.0 s backoff delays inside the unit
  test suite (no Redis in CI).
- REDIS_RETRY_BACKOFF=0.0       — zero wait between the single attempt.
- REDIS_SOCKET_TIMEOUT=0.5      — short connection timeout for fast failure.
"""

from __future__ import annotations

import os

import pytest

# Must be set before any validate_and_filter / redis_client import occurs.
os.environ.setdefault("MARKET_HOURS_GATES_ENABLED", "0")
os.environ.setdefault("REDIS_RETRY_ATTEMPTS", "1")
os.environ.setdefault("REDIS_RETRY_BACKOFF", "0.0")
os.environ.setdefault("REDIS_SOCKET_TIMEOUT", "0.5")


def _reset_validate_and_filter_redis_state() -> None:
    """Reset module-level Redis circuit breaker between tests."""
    try:
        import validate_and_filter as vf

        vf._REDIS_FAILURE_COUNT = 0
        vf._redis_circuit_open = False
        vf._redis_last_failure_ts = 0.0
        vf._redis_circuit_warned_this_cycle = False
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _reset_redis_circuit_state() -> None:
    _reset_validate_and_filter_redis_state()
    yield
    _reset_validate_and_filter_redis_state()
