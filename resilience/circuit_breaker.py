"""Thread-safe in-memory circuit breaker for MCP endpoints."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class CircuitState:
    """Tracks consecutive failures for one endpoint."""

    failures: int = 0
    open_until: float = 0.0


class CircuitBreakerRegistry:
    """Per-endpoint circuit breaker with mutex-protected state updates."""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        open_duration: float = 300.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.open_duration = open_duration
        self._circuits: dict[str, CircuitState] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> CircuitState:
        with self._lock:
            if key not in self._circuits:
                self._circuits[key] = CircuitState()
            return self._circuits[key]

    def is_open(self, key: str) -> bool:
        circuit = self.get(key)
        with self._lock:
            return circuit.failures >= self.failure_threshold and time.monotonic() < circuit.open_until

    def record_success(self, key: str) -> None:
        with self._lock:
            circuit = self._circuits.setdefault(key, CircuitState())
            circuit.failures = 0
            circuit.open_until = 0.0

    def record_failure(self, key: str) -> bool:
        """Record failure; return True if circuit just opened."""
        with self._lock:
            circuit = self._circuits.setdefault(key, CircuitState())
            circuit.failures += 1
            if circuit.failures >= self.failure_threshold:
                circuit.open_until = time.monotonic() + self.open_duration
                return True
            return False

    def seconds_until_close(self, key: str) -> float:
        circuit = self.get(key)
        with self._lock:
            return max(0.0, circuit.open_until - time.monotonic())
