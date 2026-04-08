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
from datetime import date, datetime, timedelta, timezone
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
    log_params = {k: v for k, v in params.items() if k != "apiKey"}

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
        logger.warning("Chart candle fetch failed for %s (params=%s): %s", symbol, log_params, exc)
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


def _fetch_last_trade(symbol: str) -> tuple[float | None, str | None]:
    """Fetch the most recent trade price/timestamp from Polygon.

    Returns:
        Tuple of (price, ISO timestamp), or (None, None) on failure.
    """
    api_key = os.getenv("POLYGON_API_KEY", "")
    if not api_key:
        return None, None

    url = f"{POLYGON_BASE_URL}/v2/last/trade/{symbol}"
    params: dict[str, Any] = {"apiKey": api_key}

    try:
        client = _get_chart_client()
        data: dict = {}
        for attempt in range(_MAX_RETRIES + 1):
            resp = client.get(url, params=params)
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                logger.info("Polygon 429 for last trade %s, backing off %.0fs", symbol, _RETRY_BACKOFF)
                time.sleep(_RETRY_BACKOFF)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("Last trade fetch failed for %s: %s", symbol, exc)
        return None, None

    # Polygon response can include either "results" (v2) or "last" (older variants)
    payload = data.get("results") or data.get("last") or {}
    try:
        price = float(payload.get("p"))
    except (TypeError, ValueError):
        return None, None

    ts_raw = payload.get("t")
    if ts_raw is None:
        return price, None


def _parse_iso_utc(ts: str | None) -> datetime | None:
    """Parse an ISO timestamp into a timezone-aware UTC datetime."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None

    # Polygon trade timestamp may be in ns/us/ms depending on endpoint variant.
    # Infer scale by magnitude and convert to UTC ISO8601.
    try:
        ts_int = int(ts_raw)
        if ts_int > 10_000_000_000_000_000:  # ns
            seconds = ts_int / 1_000_000_000
        elif ts_int > 10_000_000_000_000:  # us
            seconds = ts_int / 1_000_000
        elif ts_int > 10_000_000_000:  # ms
            seconds = ts_int / 1_000
        else:
            seconds = float(ts_int)

        ts_iso = pd.to_datetime(seconds, unit="s", utc=True).isoformat()
        return price, ts_iso
    except (TypeError, ValueError, OverflowError):
        return price, None


def generate_chart(
    symbol: str,
    timeframe: str,
    entry: dict[str, float],
) -> tuple[bytes | None, float | None, float | None, str | None]:
    """Generate a candlestick chart PNG with entry/stop/target overlays and EMAs.

    Args:
        symbol: Ticker symbol (e.g. ``"NVDA"``).
        timeframe: Alert timeframe (e.g. ``"15m"``, ``"1h"``).
        entry: Dict with keys ``level``, ``stop``, ``target`` (all floats).

        Returns:
        Tuple of:
          - PNG image bytes or None
          - 14-period ATR value or None
                    - Most recent market price (live trade preferred, candle close fallback) or None
                    - ISO timestamp for the selected market price or None
    """
    df = _fetch_candles(symbol, timeframe)
    live_price, live_ts = _fetch_last_trade(symbol)

    if df.empty:
        return None, None, live_price, live_ts

    latest_price = float(df["Close"].iloc[-1])
    latest_ts = pd.Timestamp(df.index[-1]).isoformat()

    # Use the freshest timestamped market source:
    # live trade quote (when recent) vs latest candle close.
    candle_dt = _parse_iso_utc(latest_ts)
    live_dt = _parse_iso_utc(live_ts)
    if live_price is not None:
        if live_dt and candle_dt:
            if live_dt >= candle_dt:
                latest_price = live_price
                latest_ts = live_ts or latest_ts
        elif live_dt and not candle_dt:
            latest_price = live_price
            latest_ts = live_ts or latest_ts
        elif live_ts is None:
            # Last-trade endpoint returned price without timestamp; avoid
            # overriding a candle-derived timestamped quote with unknown recency.
            pass

    try:
        import mplfinance as mpf
    except ImportError:
        logger.warning("mplfinance not installed — skipping chart generation")
        return None, None, latest_price, latest_ts

    entry_price = entry.get("level", 0)
    stop_price = entry.get("stop", 0)
    target_price = entry.get("target", 0)

    # Compute 14-period ATR
    atr_value: float | None = None
    if len(df) >= 15:
        high_low = df["High"] - df["Low"]
        high_close = (df["High"] - df["Close"].shift()).abs()
        low_close = (df["Low"] - df["Close"].shift()).abs()
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr_value = float(true_range.rolling(14).mean().iloc[-1])

    # Compute EMA overlays based on timeframe
    ema_plots: list = []
    if timeframe in ("5m", "15m"):
        ema_periods = [9, 21]
        ema_colors = ["#F39C12", "#9B59B6"]  # amber, purple
    elif timeframe in ("1h", "4h"):
        ema_periods = [20, 50]
        ema_colors = ["#3498DB", "#E67E22"]  # blue, orange
    else:  # 1D
        ema_periods = [20, 50]
        ema_colors = ["#3498DB", "#E67E22"]

    for period, color in zip(ema_periods, ema_colors):
        if len(df) >= period:
            ema = df["Close"].ewm(span=period, adjust=False).mean()
            ema_plots.append(mpf.make_addplot(ema, color=color, width=1.0, label=f"EMA{period}"))

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
    fig = None
    try:
        plot_kwargs: dict[str, Any] = {
            "type": "candle",
            "style": style,
            "volume": True,
            "title": f"\n{symbol} — {tf_label} Chart",
            "ylabel": "Price ($)",
            "ylabel_lower": "Volume",
            "figsize": (10, 6),
            "tight_layout": True,
            "returnfig": True,
            **hline_kwargs,
        }
        if ema_plots:
            plot_kwargs["addplot"] = ema_plots

        fig, axes = mpf.plot(df, **plot_kwargs)

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
        chart_bytes = buf.getvalue()
    except Exception as exc:  # noqa: BLE001 — chart is cosmetic; rendering failure must not block alert delivery
        logger.warning("Chart rendering failed for %s: %s", symbol, exc)
        return None, atr_value, latest_price, latest_ts
    finally:
        if fig is not None:
            import matplotlib.pyplot as plt

            plt.close(fig)
        buf.close()

    logger.info("Generated %s chart for %s (%d bytes, ATR=%.4f)", tf_label, symbol, len(chart_bytes), atr_value or 0)
    return chart_bytes, atr_value, latest_price, latest_ts
