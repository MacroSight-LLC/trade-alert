"""Backward-compatible shim — canonical code in formatter/chart.py."""

from formatter.chart import (  # noqa: F401
    _fetch_candles,
    generate_chart,
)

__all__ = ["_fetch_candles", "generate_chart"]
