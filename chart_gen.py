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

from metrics import CHART_GEN_DURATION

logger = logging.getLogger(__name__)

POLYGON_BASE_URL = "https://api.polygon.io"
ALPACA_BASE_URL = "https://data.alpaca.markets"
POLYGON_TIMEOUT = float(os.getenv("POLYGON_TIMEOUT", "10.0"))
POLYGON_LAST_TRADE_TIMEOUT = float(os.getenv("POLYGON_LAST_TRADE_TIMEOUT", "3.0"))
ALPACA_TIMEOUT = float(os.getenv("ALPACA_TIMEOUT", "5.0"))
_MAX_RETRIES = 1
_RETRY_BACKOFF = 13.0  # slightly over Polygon's 12s free-tier rate window
_LAST_TRADE_MAX_RETRIES = int(os.getenv("POLYGON_LAST_TRADE_RETRIES", "2"))
_LAST_TRADE_BACKOFF = float(os.getenv("POLYGON_LAST_TRADE_BACKOFF", "0.5"))

_chart_client: httpx.Client | None = None
_alpaca_client: httpx.Client | None = None


def _get_chart_client() -> httpx.Client:
    """Return a module-level HTTP client for Polygon chart requests."""
    global _chart_client  # noqa: PLW0603
    if _chart_client is None or _chart_client.is_closed:
        _chart_client = httpx.Client(timeout=POLYGON_TIMEOUT)
    return _chart_client


def _get_alpaca_client() -> httpx.Client:
    """Return a module-level HTTP client for Alpaca price fallback requests."""
    global _alpaca_client  # noqa: PLW0603
    if _alpaca_client is None or _alpaca_client.is_closed:
        _alpaca_client = httpx.Client(timeout=ALPACA_TIMEOUT)
    return _alpaca_client


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
        for attempt in range(_LAST_TRADE_MAX_RETRIES + 1):
            resp = client.get(url, params=params, timeout=POLYGON_LAST_TRADE_TIMEOUT)
            if resp.status_code == 429 and attempt < _LAST_TRADE_MAX_RETRIES:
                backoff = _LAST_TRADE_BACKOFF * (2**attempt)
                logger.info("Polygon 429 for last trade %s, backing off %.1fs", symbol, backoff)
                time.sleep(backoff)
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


def _alpaca_timeframe(timeframe: str) -> str:
    """Map alert timeframe to Alpaca bars timeframe string."""
    return {
        "5m": "5Min",
        "15m": "15Min",
        "1h": "1Hour",
        "4h": "4Hour",
        "1D": "1Day",
    }.get(timeframe, "15Min")


def _fetch_alpaca_last_close(symbol: str, timeframe: str) -> tuple[float | None, str | None]:
    """Fetch latest bar close from Alpaca as fallback quote source.

    Returns:
        Tuple of (close_price, ISO timestamp), or (None, None) on failure.
    """
    api_key = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        return None, None

    timeframe_param = _alpaca_timeframe(timeframe)
    now_utc = datetime.now(timezone.utc)
    start = (now_utc - timedelta(days=3)).isoformat()

    url = f"{ALPACA_BASE_URL}/v2/stocks/{symbol}/bars"
    params: dict[str, Any] = {
        "timeframe": timeframe_param,
        "start": start,
        "limit": 1,
        "adjustment": "raw",
        "feed": "iex",
        "sort": "desc",
    }
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
    }

    try:
        resp = _get_alpaca_client().get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("Alpaca fallback fetch failed for %s: %s", symbol, exc)
        return None, None

    bars = data.get("bars") or []
    if not bars:
        return None, None

    latest = bars[0]
    try:
        close = float(latest.get("c"))
    except (TypeError, ValueError):
        return None, None

    ts = latest.get("t")
    if isinstance(ts, str):
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return close, parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            return close, None
    return close, None


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
    with CHART_GEN_DURATION.time():
        return _generate_chart_impl(symbol, timeframe, entry)


def _generate_chart_impl(
    symbol: str,
    timeframe: str,
    entry: dict[str, float],
) -> tuple[bytes | None, float | None, float | None, str | None]:
    """Implementation of generate_chart wrapped by Prometheus timing."""
    df = _fetch_candles(symbol, timeframe)
    live_price, live_ts = _fetch_last_trade(symbol)
    alpaca_price, alpaca_ts = _fetch_alpaca_last_close(symbol, timeframe)

    latest_price: float | None = None
    latest_ts: str | None = None
    selected_source = "none"

    candle_price: float | None = None
    candle_ts: str | None = None
    if not df.empty:
        candle_price = float(df["Close"].iloc[-1])
        candle_ts = pd.Timestamp(df.index[-1]).isoformat()

    # Primary quote source: Polygon last trade.
    if live_price is not None:
        latest_price = live_price
        latest_ts = live_ts
        selected_source = "polygon_last_trade"
    # Secondary: Alpaca latest close.
    elif alpaca_price is not None:
        latest_price = alpaca_price
        latest_ts = alpaca_ts
        selected_source = "alpaca_last_close"
    # Last resort: chart candle close.
    elif candle_price is not None:
        latest_price = candle_price
        latest_ts = candle_ts
        selected_source = "polygon_candle_close"

    # If we have multiple timestamped sources, keep the freshest timestamp.
    candidate_quotes: list[tuple[float, str | None, str]] = []
    if live_price is not None:
        candidate_quotes.append((live_price, live_ts, "polygon_last_trade"))
    if alpaca_price is not None:
        candidate_quotes.append((alpaca_price, alpaca_ts, "alpaca_last_close"))
    if candle_price is not None:
        candidate_quotes.append((candle_price, candle_ts, "polygon_candle_close"))

    freshest_dt: datetime | None = None
    for price_val, ts_val, src in candidate_quotes:
        dt_val = _parse_iso_utc(ts_val)
        if dt_val is None:
            continue
        if freshest_dt is None or dt_val > freshest_dt:
            freshest_dt = dt_val
            latest_price = price_val
            latest_ts = ts_val
            selected_source = src

    logger.info(
        "Price source %s for %s tf=%s (live=%s alpaca=%s candle=%s)",
        selected_source,
        symbol,
        timeframe,
        "yes" if live_price is not None else "no",
        "yes" if alpaca_price is not None else "no",
        "yes" if candle_price is not None else "no",
    )

    if df.empty:
        return None, None, latest_price, latest_ts

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
