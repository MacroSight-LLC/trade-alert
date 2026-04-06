"""TimesFM MCP — time-series forecasting via Google TimesFM 2.5.

Tools: forecast (batch), validate (single-symbol directional check).
Requires: POLYGON_API_KEY in environment (via Vault).
Model: google/timesfm-2.5-200m-pytorch (loaded once at startup).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, timedelta
from typing import Any

import httpx
import numpy as np

logger = logging.getLogger(__name__)

SERVICE_NAME = "TimesFM MCP"

# ── Configuration (env-overridable) ──────────────────────────────
_CONTEXT_LENGTH = int(os.getenv("TIMESFM_CONTEXT_LENGTH", "512"))
_HORIZON = int(os.getenv("TIMESFM_HORIZON", "24"))
_QUANTILES = [0.1, 0.5, 0.9]
_MAX_BATCH = int(os.getenv("TIMESFM_MAX_BATCH", "50"))
_POLYGON_TIMEOUT = float(os.getenv("TIMESFM_POLYGON_TIMEOUT", "10.0"))

# Timeframe → (multiplier, span) for Polygon range endpoint
_TIMEFRAME_MAP: dict[str, tuple[int, str]] = {
    "5m": (5, "minute"),
    "15m": (15, "minute"),
    "1h": (1, "hour"),
    "4h": (4, "hour"),
    "1D": (1, "day"),
}

# ── In-memory result cache (TTL-based) ──────────────────────────
_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_CACHE_TTL = float(os.getenv("TIMESFM_CACHE_TTL", "900"))

# ── HTTP client ──────────────────────────────────────────────────
_http_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    """Return a module-level pooled HTTP client."""
    global _http_client  # noqa: PLW0603
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.Client(timeout=_POLYGON_TIMEOUT)
    return _http_client


# ── Model loading (lazy singleton) ───────────────────────────────
_model: Any = None
_model_load_attempted = False


def _get_model() -> Any:
    """Load TimesFM model once, return cached instance or None on failure."""
    global _model, _model_load_attempted  # noqa: PLW0603
    if _model is not None:
        return _model
    if _model_load_attempted:
        return None
    _model_load_attempted = True
    try:
        import timesfm  # type: ignore[import-untyped]

        tfm = timesfm.TimesFm(
            hparams=timesfm.TimesFmHparams(
                per_core_batch_size=32,
                horizon_len=_HORIZON,
                backend="cpu",
            ),
            checkpoint=timesfm.TimesFmCheckpoint(
                huggingface_repo_id="google/timesfm-2.5-200m-pytorch",
            ),
        )
        _model = tfm
        logger.info("TimesFM model loaded successfully (horizon=%d, ctx=%d)", _HORIZON, _CONTEXT_LENGTH)
        return _model
    except Exception as exc:
        logger.error("Failed to load TimesFM model: %s", exc)
        return None


# ── Polygon OHLCV fetch ──────────────────────────────────────────


def _fetch_close_series(symbol: str, timeframe: str) -> list[float]:
    """Fetch close prices from Polygon for TimesFM input.

    Args:
        symbol: Ticker symbol (e.g. ``"AAPL"``).
        timeframe: Candle timeframe (e.g. ``"15m"``).

    Returns:
        List of close prices (most recent last), or empty list on failure.
    """
    api_key = os.getenv("POLYGON_API_KEY", "")
    if not api_key:
        logger.warning("POLYGON_API_KEY not set — cannot fetch bars for %s", symbol)
        return []

    multiplier, span = _TIMEFRAME_MAP.get(timeframe, (15, "minute"))
    # Request extra bars to ensure we get _CONTEXT_LENGTH after filtering
    request_bars = _CONTEXT_LENGTH + 50

    if span == "minute":
        lookback_days = max(5, (request_bars * multiplier) // (60 * 6) + 3)
    elif span == "hour":
        lookback_days = max(5, (request_bars * multiplier) // 6 + 3)
    else:
        lookback_days = request_bars + 15

    end = date.today()
    start = end - timedelta(days=lookback_days)

    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range"
        f"/{multiplier}/{span}/{start.isoformat()}/{end.isoformat()}"
    )
    params: dict[str, Any] = {
        "adjusted": "true",
        "sort": "asc",
        "limit": request_bars,
        "apiKey": api_key,
    }

    try:
        resp = _get_client().get(url, params=params)
        if resp.status_code == 429:
            logger.warning("Polygon 429 for %s — rate limited", symbol)
            return []
        resp.raise_for_status()
        bars = resp.json().get("results", [])
        closes = [float(b["c"]) for b in bars if "c" in b]
        # Take the most recent _CONTEXT_LENGTH bars
        return closes[-_CONTEXT_LENGTH:]
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        logger.warning("Failed to fetch bars for %s: %s", symbol, exc)
        return []


# ── Core forecast logic ──────────────────────────────────────────


def _run_forecast(symbols: list[str], timeframe: str) -> dict[str, dict[str, Any]]:
    """Run TimesFM forecast for a batch of symbols.

    Args:
        symbols: List of ticker symbols.
        timeframe: Candle timeframe.

    Returns:
        Dict keyed by symbol with forecast results.
    """
    model = _get_model()
    if model is None:
        return {s: {"error": "model_unavailable"} for s in symbols}

    now = time.monotonic()
    results: dict[str, dict[str, Any]] = {}
    to_forecast: list[str] = []
    series_batch: list[list[float]] = []

    for symbol in symbols[:_MAX_BATCH]:
        # Check cache first
        cache_key = (symbol, timeframe)
        cached = _CACHE.get(cache_key)
        if cached and (now - cached[0]) < _CACHE_TTL:
            results[symbol] = cached[1]
            continue

        closes = _fetch_close_series(symbol, timeframe)
        if len(closes) < 32:  # minimum context for meaningful forecast
            results[symbol] = {"error": f"insufficient_data ({len(closes)} bars)"}
            continue

        to_forecast.append(symbol)
        series_batch.append(closes)

    if series_batch:
        try:
            # Batch inference — all series at once for efficiency
            freq_map = {"5m": 0, "15m": 0, "1h": 0, "4h": 0, "1D": 0}
            freq = freq_map.get(timeframe, 0)

            point_forecast, quantile_forecast = model.forecast(
                [np.array(s) for s in series_batch],
                freq=[freq] * len(series_batch),
            )

            for i, symbol in enumerate(to_forecast):
                current_price = series_batch[i][-1]
                median = point_forecast[i].tolist()[:_HORIZON]
                endpoint = median[-1] if median else current_price
                direction_pct = ((endpoint - current_price) / current_price) * 100 if current_price else 0.0

                # Extract quantile forecasts
                quantiles: dict[str, list[float]] = {}
                if quantile_forecast is not None and len(quantile_forecast.shape) == 3:
                    # Shape: (batch, quantile_idx, horizon)
                    for qi, q_label in enumerate(["p10", "p50", "p90"]):
                        if qi < quantile_forecast.shape[1]:
                            quantiles[q_label] = quantile_forecast[i, qi].tolist()[:_HORIZON]

                result = {
                    "symbol": symbol,
                    "median_forecast": median,
                    "quantiles": quantiles,
                    "current_price": current_price,
                    "horizon_bars": len(median),
                    "direction_pct": round(direction_pct, 4),
                }
                results[symbol] = result
                _CACHE[(symbol, timeframe)] = (now, result)

        except Exception as exc:
            logger.error("TimesFM batch forecast failed: %s", exc)
            for symbol in to_forecast:
                results[symbol] = {"error": str(exc)}

    return results


# ── Tool handlers (exported to MCP framework) ────────────────────


async def forecast_handler(params: dict[str, Any]) -> dict[str, Any]:
    """Batch forecast for up to 50 symbols.

    Args:
        params: ``{"symbols": list[str], "timeframe": str}``

    Returns:
        ``{"results": [...]}``, one entry per symbol.
    """
    symbols = params.get("symbols", [])
    timeframe = params.get("timeframe", "15m")

    if not symbols:
        return {"results": [], "error": "no symbols provided"}

    if not isinstance(symbols, list):
        symbols = [symbols]

    forecast_results = _run_forecast(symbols[:_MAX_BATCH], timeframe)
    return {"results": list(forecast_results.values())}


async def validate_handler(params: dict[str, Any]) -> dict[str, Any]:
    """Single-symbol directional validation for gate / outcome use.

    Args:
        params: ``{"symbol": str, "direction": str, "entry_level": float, "timeframe": str}``

    Returns:
        ``{"agrees": bool, "forecast_direction": str, "confidence": float, "median_endpoint": float}``
    """
    symbol = params.get("symbol", "")
    direction = params.get("direction", "").upper()
    timeframe = params.get("timeframe", "15m")

    if not symbol:
        return {"error": "symbol required"}

    forecast_results = _run_forecast([symbol], timeframe)
    result = forecast_results.get(symbol, {})

    if "error" in result:
        return {"agrees": True, "error": result["error"], "confidence": 0.0, "median_endpoint": 0.0}

    direction_pct = result.get("direction_pct", 0.0)
    current_price = result.get("current_price", 0.0)
    median = result.get("median_forecast", [])
    endpoint = median[-1] if median else current_price

    # Determine forecast direction
    if direction_pct > 0.1:
        forecast_dir = "LONG"
    elif direction_pct < -0.1:
        forecast_dir = "SHORT"
    else:
        forecast_dir = "NEUTRAL"

    agrees = forecast_dir == direction or forecast_dir == "NEUTRAL" or direction == "WATCH"

    # Confidence based on quantile spread
    quantiles = result.get("quantiles", {})
    p10 = quantiles.get("p10", [])
    p90 = quantiles.get("p90", [])
    if p10 and p90 and current_price > 0:
        spreads = [abs(hi - lo) for hi, lo in zip(p90, p10)]
        mean_spread = sum(spreads) / len(spreads) if spreads else 0.0
        confidence = max(0.0, min(1.0, 1.0 - (mean_spread / current_price)))
    else:
        confidence = 0.4

    return {
        "agrees": agrees,
        "forecast_direction": forecast_dir,
        "confidence": round(confidence, 4),
        "median_endpoint": round(endpoint, 4),
        "direction_pct": round(direction_pct, 4),
    }


# ── Tool registry (required by mcp_server.py framework) ─────────
TOOLS: dict[str, Any] = {
    "forecast": forecast_handler,
    "validate": validate_handler,
}
