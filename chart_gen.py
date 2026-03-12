"""Server-side candlestick chart generation for Discord alert embeds.

Fetches intraday/daily OHLCV bars from Polygon.io, renders a
candlestick chart with entry/stop/target overlays using mplfinance,
and returns the PNG bytes for Discord file attachment upload.
"""

from __future__ import annotations

import io
import logging
import os
import time
from datetime import date, timedelta
from typing import Any

import matplotlib

import vault_env_loader  # noqa: F401  — seeds os.environ from Vault

matplotlib.use("Agg")  # headless — must precede mplfinance import

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

POLYGON_BASE_URL = "https://api.polygon.io"
POLYGON_TIMEOUT = 10.0
_MAX_RETRIES = 1
_RETRY_BACKOFF = 13.0  # slightly over Polygon's 12s free-tier rate window

_chart_client: httpx.Client | None = None


def _get_chart_client() -> httpx.Client:
    """Return a module-level HTTP client for Polygon chart requests."""
    global _chart_client  # noqa: PLW0603
    if _chart_client is None or _chart_client.is_closed:
        _chart_client = httpx.Client(timeout=POLYGON_TIMEOUT)
    return _chart_client


# Timeframe → (multiplier, span, num_bars) for Polygon range endpoint
_TIMEFRAME_MAP: dict[str, tuple[int, str, int]] = {
    "5m": (5, "minute", 48),  # ~4 hours of 5m candles
    "15m": (15, "minute", 48),  # ~12 hours of 15m candles
    "1h": (1, "hour", 48),  # ~2 days of hourly candles
    "4h": (4, "hour", 30),  # ~5 days of 4h candles
    "1D": (1, "day", 60),  # ~60 trading days
}


def _fetch_candles(symbol: str, timeframe: str) -> pd.DataFrame:
    """Fetch OHLCV bars from Polygon.io for chart rendering.

    Args:
        symbol: Ticker symbol (e.g. ``"NVDA"``).
        timeframe: Alert timeframe (e.g. ``"15m"``, ``"1h"``).

    Returns:
        DataFrame with DatetimeIndex and columns: Open, High, Low, Close, Volume.
        Empty DataFrame on any failure.
    """
    api_key = os.getenv("POLYGON_API_KEY", "")
    if not api_key:
        logger.debug("POLYGON_API_KEY not set — skipping chart")
        return pd.DataFrame()

    multiplier, span, num_bars = _TIMEFRAME_MAP.get(timeframe, (15, "minute", 48))

    # Lookback: enough calendar days to cover the requested bars
    if span == "minute":
        lookback_days = max(3, (num_bars * multiplier) // (60 * 6) + 2)
    elif span == "hour":
        lookback_days = max(3, (num_bars * multiplier) // 6 + 2)
    else:
        lookback_days = num_bars + 10

    end = date.today()
    start = end - timedelta(days=lookback_days)

    url = (
        f"{POLYGON_BASE_URL}/v2/aggs/ticker/{symbol}/range"
        f"/{multiplier}/{span}/{start.isoformat()}/{end.isoformat()}"
    )
    params: dict[str, Any] = {
        "adjusted": "true",
        "sort": "asc",
        "limit": num_bars,
        "apiKey": api_key,
    }

    try:
        client = _get_chart_client()
        data: dict = {}
        for attempt in range(_MAX_RETRIES + 1):
            resp = client.get(url, params=params)
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                logger.info("Polygon 429 for chart %s, backing off %.0fs", symbol, _RETRY_BACKOFF)
                time.sleep(_RETRY_BACKOFF)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Chart candle fetch failed for %s: %s", symbol, exc)
        return pd.DataFrame()

    bars = data.get("results", [])
    if not bars:
        logger.info("No candle data returned for %s (%s)", symbol, timeframe)
        return pd.DataFrame()

    df = pd.DataFrame(bars)
    df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume", "t": "ts"})
    df["Date"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("Date")
    df = df[["Open", "High", "Low", "Close", "Volume"]].tail(num_bars)

    return df


def generate_chart(
    symbol: str,
    timeframe: str,
    entry: dict[str, float],
) -> bytes | None:
    """Generate a candlestick chart PNG with entry/stop/target overlays.

    Args:
        symbol: Ticker symbol (e.g. ``"NVDA"``).
        timeframe: Alert timeframe (e.g. ``"15m"``, ``"1h"``).
        entry: Dict with keys ``level``, ``stop``, ``target`` (all floats).

    Returns:
        PNG image bytes, or ``None`` if chart generation fails.
    """
    df = _fetch_candles(symbol, timeframe)
    if df.empty:
        return None

    try:
        import mplfinance as mpf
    except ImportError:
        logger.warning("mplfinance not installed — skipping chart generation")
        return None

    entry_price = entry.get("level", 0)
    stop_price = entry.get("stop", 0)
    target_price = entry.get("target", 0)

    # Build horizontal lines — only include valid (non-zero) prices
    hlines_vals: list[float] = []
    hlines_colors: list[str] = []
    hlines_widths: list[float] = []
    hlines_styles: list[str] = []

    for price, color, style in [
        (entry_price, "#FFFFFF", "-"),  # white solid = entry
        (stop_price, "#E74C3C", "--"),  # red dashed = stop
        (target_price, "#2ECC71", "--"),  # green dashed = target
    ]:
        if price > 0:
            hlines_vals.append(price)
            hlines_colors.append(color)
            hlines_widths.append(1.2)
            hlines_styles.append(style)

    hline_kwargs: dict[str, Any] = {}
    if hlines_vals:
        hline_kwargs["hlines"] = dict(
            hlines=hlines_vals,
            colors=hlines_colors,
            linewidths=hlines_widths,
            linestyle=hlines_styles,
        )

    # Dark style matching Discord embeds
    mc = mpf.make_marketcolors(
        up="#2ECC71",
        down="#E74C3C",
        edge="inherit",
        wick="inherit",
        volume={"up": "#2ECC71", "down": "#E74C3C"},
    )
    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        marketcolors=mc,
        facecolor="#2C2F33",
        edgecolor="#2C2F33",
        figcolor="#2C2F33",
        gridcolor="#40444B",
        gridstyle="--",
        gridaxis="both",
    )

    # Price annotation text at right edge
    multiplier, span, _ = _TIMEFRAME_MAP.get(timeframe, (15, "minute", 48))
    tf_label = f"{multiplier}{span[0].upper()}" if span != "day" else "D"

    buf = io.BytesIO()
    try:
        fig, axes = mpf.plot(
            df,
            type="candle",
            style=style,
            volume=True,
            title=f"\n{symbol} — {tf_label} Chart",
            ylabel="Price ($)",
            ylabel_lower="Volume",
            figsize=(10, 6),
            tight_layout=True,
            returnfig=True,
            **hline_kwargs,
        )

        # Reserve right margin for price labels
        fig.subplots_adjust(right=0.82)

        # Add price labels on the right margin
        ax_price = axes[0]
        label_configs = [
            (entry_price, "Entry", "#FFFFFF"),
            (stop_price, "Stop", "#E74C3C"),
            (target_price, "Target", "#2ECC71"),
        ]
        for price, label, color in label_configs:
            if price > 0:
                ax_price.annotate(
                    f"  {label} ${price:,.2f}",
                    xy=(len(df) - 1, price),
                    xytext=(len(df) + 1, price),
                    fontsize=8,
                    color=color,
                    fontweight="bold",
                    va="center",
                )

        fig.savefig(buf, format="png", dpi=120, facecolor="#2C2F33")
        import matplotlib.pyplot as plt

        plt.close(fig)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Chart rendering failed for %s: %s", symbol, exc)
        return None

    buf.seek(0)
    chart_bytes = buf.read()
    logger.info("Generated %s chart for %s (%d bytes)", tf_label, symbol, len(chart_bytes))
    return chart_bytes
