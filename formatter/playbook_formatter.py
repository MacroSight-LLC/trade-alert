"""Unified formatter facade for Discord embeds and chart attachments (SSOT §11)."""

from __future__ import annotations

from typing import Any

from formatter.chart import generate_chart as _generate_chart
from formatter.embed import compute_rr as _compute_rr
from formatter.embed import format_embed as _format_embed
from models import PlaybookAlert


class PlaybookFormatter:
    """Facade for alert embed formatting and candlestick chart generation."""

    @staticmethod
    def format_embed(
        alert: PlaybookAlert,
        *,
        hist_stats: str = "",
        current_price: float | None = None,
        current_price_ts: str | None = None,
    ) -> dict[str, Any]:
        return _format_embed(
            alert,
            hist_stats=hist_stats,
            current_price=current_price,
            current_price_ts=current_price_ts,
        )

    @staticmethod
    def generate_chart(
        symbol: str,
        timeframe: str,
        entry: dict[str, float],
    ) -> tuple[bytes | None, float | None, float | None, str | None]:
        return _generate_chart(symbol, timeframe, entry)

    @staticmethod
    def compute_rr(entry: dict[str, float]) -> str:
        return _compute_rr(entry)
