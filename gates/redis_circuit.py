"""Redis circuit breaker for WATCH decay and dedup paths."""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

_REDIS_FAILURE_COUNT = 0
_REDIS_FAILURE_THRESHOLD = int(os.environ.get("REDIS_FAILURE_THRESHOLD", "3"))
_REDIS_FAILURE_WINDOW_SECONDS = int(os.environ.get("REDIS_FAILURE_WINDOW_SECONDS", "60"))
_redis_last_failure_ts: float = 0.0
_redis_circuit_open: bool = False
_redis_circuit_warned_this_cycle: bool = False


def _metrics():
    """Resolve Prometheus metrics via validate_and_filter for test patch compatibility."""
    import validate_and_filter as vf

    return vf.GATE_REJECTIONS, vf.REDIS_CIRCUIT_OPEN


def _check_redis_circuit() -> bool:
    """Return True when the circuit is open (skip Redis calls).

    Lazy-resets the circuit after ``_REDIS_FAILURE_WINDOW_SECONDS`` with no
    new failures.
    """
    global _redis_circuit_open, _REDIS_FAILURE_COUNT  # noqa: PLW0603

    now = time.monotonic()
    if _redis_circuit_open and (now - _redis_last_failure_ts) >= _REDIS_FAILURE_WINDOW_SECONDS:
        _redis_circuit_open = False
        _REDIS_FAILURE_COUNT = 0
        _, redis_circuit_open = _metrics()
        redis_circuit_open.set(0)
        logger.info("Redis circuit breaker reset — WATCH decay re-enabled")

    return _redis_circuit_open


def _record_redis_failure() -> None:
    """Increment failure counter and open circuit if threshold exceeded."""
    global _redis_circuit_open, _REDIS_FAILURE_COUNT, _redis_last_failure_ts  # noqa: PLW0603

    now = time.monotonic()
    if _redis_last_failure_ts and (now - _redis_last_failure_ts) >= _REDIS_FAILURE_WINDOW_SECONDS:
        _REDIS_FAILURE_COUNT = 0

    _REDIS_FAILURE_COUNT += 1
    _redis_last_failure_ts = now

    if _REDIS_FAILURE_COUNT >= _REDIS_FAILURE_THRESHOLD and not _redis_circuit_open:
        _redis_circuit_open = True
        gate_rejections, redis_circuit_open = _metrics()
        redis_circuit_open.set(1)
        gate_rejections.labels(gate="redis_circuit_open").inc()
        logger.warning(
            "Redis circuit breaker OPEN after %d failures in %ds window",
            _REDIS_FAILURE_COUNT,
            _REDIS_FAILURE_WINDOW_SECONDS,
        )


def is_redis_circuit_open() -> bool:
    """Public accessor for healthcheck / dashboard."""
    return _check_redis_circuit()


def reset_circuit_warned_flag() -> None:
    """Reset per-cycle warning flag at the start of validate_and_filter."""
    global _redis_circuit_warned_this_cycle  # noqa: PLW0603
    _redis_circuit_warned_this_cycle = False


def mark_circuit_warned() -> None:
    """Record that the circuit-open warning was emitted this cycle."""
    global _redis_circuit_warned_this_cycle  # noqa: PLW0603
    _redis_circuit_warned_this_cycle = True


def circuit_warned_this_cycle() -> bool:
    """Return whether the circuit-open warning was already logged this cycle."""
    return _redis_circuit_warned_this_cycle
