"""Typed entry order model for gate and schema validation (SSOT §4)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class EntryOrder:
    """Finite level/stop/target with direction-aware ordering checks."""

    level: float
    stop: float
    target: float

    @classmethod
    def from_dict(cls, entry: dict[str, float] | Any) -> EntryOrder:
        try:
            return cls(
                level=float(entry["level"]),
                stop=float(entry["stop"]),
                target=float(entry["target"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("entry missing required keys: level, stop, target") from exc

    def all_finite_positive(self) -> bool:
        return all(
            isinstance(v, (int, float)) and math.isfinite(float(v)) and float(v) > 0
            for v in (self.level, self.stop, self.target)
        )

    def has_valid_ordering(self, direction: Literal["LONG", "SHORT", "WATCH"]) -> bool:
        if direction == "WATCH":
            return True
        if direction == "LONG":
            return self.stop < self.level < self.target
        if direction == "SHORT":
            return self.target < self.level < self.stop
        return False

    def is_valid_for_direction(self, direction: Literal["LONG", "SHORT", "WATCH"]) -> bool:
        if direction == "WATCH":
            return True
        if not self.all_finite_positive():
            return False
        return self.has_valid_ordering(direction)
