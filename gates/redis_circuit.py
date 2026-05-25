"""Redis circuit breaker for WATCH decay and dedup paths."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class RedisCircuitBreaker:
    failure_count: int = 0
    failure_threshold: int = field(
        default_factory=lambda: int(os.environ.get("REDIS_FAILURE_THRESHOLD", "3"))
    )
    failure_window_seconds: int = field(
        default_factory=lambda: int(os.environ.get("REDIS_FAILURE_WINDOW_SECONDS", "60"))
    )
    last_failure_ts: float = 0.0
    circuit_open: bool = False
    warned_this_cycle: bool = False

    def check(self) -> bool:
        """Return True when the circuit is open (skip Redis calls).

        Lazy-resets after ``failure_window_seconds`` with no new failures.
        """
        now = time.monotonic()
        if (
            self.circuit_open
            and self.last_failure_ts > 0.0
            and (now - self.last_failure_ts) >= self.failure_window_seconds
        ):
            self.circuit_open = False
            self.failure_count = 0
            _, redis_circuit_open = _metrics()
            redis_circuit_open.set(0)
            logger.info("Redis circuit breaker reset — WATCH decay re-enabled")
        return self.circuit_open

    def record_failure(self) -> None:
        """Increment failure counter and open circuit if threshold exceeded."""
        now = time.monotonic()
        if self.last_failure_ts and (now - self.last_failure_ts) >= self.failure_window_seconds:
            self.failure_count = 0

        self.failure_count += 1
        self.last_failure_ts = now

        if self.failure_count >= self.failure_threshold and not self.circuit_open:
            self.circuit_open = True
            gate_rejections, redis_circuit_open = _metrics()
            redis_circuit_open.set(1)
            gate_rejections.labels(gate="redis_circuit_open").inc()
            logger.warning(
                "Redis circuit breaker OPEN after %d failures in %ds window",
                self.failure_count,
                self.failure_window_seconds,
            )

    def reset_for_tests(self) -> None:
        """Full state reset — call from conftest autouse fixture."""
        self.failure_count = 0
        self.last_failure_ts = 0.0
        self.circuit_open = False
        self.warned_this_cycle = False


def _metrics():
    """Resolve Prometheus metrics via validate_and_filter for test patch compatibility."""
    import validate_and_filter as vf

    return vf.GATE_REJECTIONS, vf.REDIS_CIRCUIT_OPEN


_breaker = RedisCircuitBreaker()


def get_breaker() -> RedisCircuitBreaker:
    """Return the module-level singleton — use in tests and health checks."""
    return _breaker


def _check_redis_circuit() -> bool:
    return _breaker.check()


def _record_redis_failure() -> None:
    _breaker.record_failure()


def is_redis_circuit_open() -> bool:
    return _breaker.check()


def reset_circuit_warned_flag() -> None:
    _breaker.warned_this_cycle = False


def mark_circuit_warned() -> None:
    _breaker.warned_this_cycle = True


def circuit_warned_this_cycle() -> bool:
    return _breaker.warned_this_cycle
