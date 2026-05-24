"""Outcome tracker — resolves open alerts as WIN / LOSS / EXPIRED.

SSOT Reference: §12 — Postgres Schema & Analytics.
Polls Polygon.io / Finnhub for current prices, evaluates each open alert
against its target / stop levels, and writes results via ``outcome_queries``.
"""

from __future__ import annotations

import atexit
import logging
import os
import time

import httpx

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from constants import is_market_open
from models import PlaybookAlert  # noqa: F401 — required project import
from outcome_queries import (
    evaluate_outcome,
    expire_stale_alerts,
    fetch_daily_expiry_stats,
    fetch_open_alerts,
    map_db_row,
    write_outcome,
)

logger = logging.getLogger(__name__)

PRICE_POLL_INTERVAL_SECONDS: int = int(os.getenv("PRICE_POLL_INTERVAL", "60"))
PRICE_FETCH_MAX_RETRIES: int = int(os.getenv("PRICE_FETCH_MAX_RETRIES", "3"))
PRICE_FETCH_TIMEOUT: float = float(os.getenv("PRICE_FETCH_TIMEOUT", "10.0"))

FORECAST_STOP_TIGHTEN_ENABLED: bool = os.getenv("FORECAST_STOP_TIGHTEN_ENABLED", "false").lower() == "true"
FORECAST_STOP_TIGHTEN_RATIO: float = float(os.getenv("FORECAST_STOP_TIGHTEN_RATIO", "0.50"))
FORECAST_STOP_MIN_CUSHION_PCT: float = float(os.getenv("FORECAST_STOP_MIN_CUSHION_PCT", "1.0"))
_TIMESFM_MCP_URL: str = os.getenv("TIMESFM_MCP_URL", "http://timesfm-mcp:8012")

_EXPIRY_RATE_THRESHOLD: float = float(os.getenv("EXPIRY_RATE_THRESHOLD", "0.15"))

_http_client: httpx.Client | None = None

# Backward-compatible re-exports for tests and integration patches.
_map_db_row = map_db_row
get_open_alerts = fetch_open_alerts
update_outcome = write_outcome


def _close_http_client() -> None:
    """Close the module-level HTTP client on process exit."""
    global _http_client  # noqa: PLW0603
    if _http_client is not None and not _http_client.is_closed:
        _http_client.close()
        _http_client = None


atexit.register(_close_http_client)


def _get_http_client() -> httpx.Client:
    """Return a module-level HTTP client with connection pooling."""
    global _http_client  # noqa: PLW0603
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=PRICE_FETCH_TIMEOUT, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _http_client


def _post_outcome_to_langfuse(row: dict, outcome: str, pnl_pct: float | None) -> None:
    """Post outcome score to the originating Langfuse trace."""
    trace_id = row.get("langfuse_trace_id")
    if not trace_id:
        return
    try:
        from pipeline_tracing import add_score

        score_val = 1.0 if outcome == "WIN" else 0.0
        add_score(
            trace_id,
            "outcome_result",
            score_val,
            comment=f"{row.get('symbol')} {outcome} pnl={pnl_pct:.2f}%"
            if pnl_pct
            else f"{row.get('symbol')} {outcome}",
        )
        if pnl_pct is not None:
            add_score(trace_id, "outcome_pnl_pct", pnl_pct, comment=f"{row.get('symbol')}")
    except Exception as exc:
        logger.debug("Langfuse outcome linkage failed for alert %s: %s", row.get("id"), exc)


def _polygon_prev_close(symbol: str) -> float | None:
    """Polygon.io free-tier previous-day close."""
    api_key = os.getenv("POLYGON_API_KEY")
    if not api_key:
        return None
    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev"
    try:
        resp = _get_http_client().get(url, params={"apiKey": api_key})
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            return float(results[0]["c"])
    except (httpx.HTTPError, KeyError, TypeError, ValueError, IndexError) as exc:
        logger.debug("Polygon prev-close failed for %s: %s", symbol, exc)
    return None


def _finnhub_quote(symbol: str) -> float | None:
    """Finnhub /quote endpoint for current price."""
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        return None
    try:
        resp = _get_http_client().get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": symbol, "token": api_key},
        )
        resp.raise_for_status()
        price = resp.json().get("c")
        if price and float(price) > 0:
            return float(price)
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        logger.debug("Finnhub quote failed for %s: %s", symbol, exc)
    return None


def get_current_price(symbol: str) -> float | None:
    """Fetch latest price using Finnhub → Polygon fallback chain."""
    chain = [_finnhub_quote, _polygon_prev_close]

    for fetcher in chain:
        fetcher_name = getattr(fetcher, "__name__", repr(fetcher))
        for attempt in range(PRICE_FETCH_MAX_RETRIES):
            price = fetcher(symbol)
            if price is not None:
                return price
            if attempt < PRICE_FETCH_MAX_RETRIES - 1:
                delay = 2**attempt
                logger.debug(
                    "Price fetch %s attempt %d failed for %s — retrying in %ds",
                    fetcher_name,
                    attempt + 1,
                    symbol,
                    delay,
                )
                time.sleep(delay)
        logger.debug("Source %s exhausted for %s", fetcher_name, symbol)

    logger.warning("All price sources failed for %s", symbol)
    return None


def _check_forecast_agrees(
    symbol: str,
    direction: str,
    timeframe: str | None,
    *,
    cycle_cache: dict[str, bool],
) -> bool:
    """Check if TimesFM forecast still agrees with alert direction."""
    if symbol in cycle_cache:
        return cycle_cache[symbol]

    try:
        resp = _get_http_client().post(
            f"{_TIMESFM_MCP_URL}/tool/validate",
            json={"symbol": symbol, "direction": direction, "timeframe": timeframe or "15m"},
            timeout=15.0,
        )
        resp.raise_for_status()
        result = resp.json()
        agrees = result.get("agrees", True)
        cycle_cache[symbol] = agrees
        if not agrees:
            logger.info(
                "Forecast disagrees for %s %s (forecast_dir=%s, pct=%.2f%%)",
                symbol,
                direction,
                result.get("forecast_direction", "?"),
                result.get("direction_pct", 0.0),
            )
        return bool(agrees)
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        logger.debug("Forecast check unavailable for %s: %s", symbol, exc)
        cycle_cache[symbol] = True
        return True


def _maybe_tighten_stop_on_forecast(
    mapped: dict,
    row: dict,
    price: float,
    *,
    forecast_cache: dict[str, bool],
) -> str | None:
    """Tighten stop when in profit and forecast disagrees; re-evaluate outcome."""
    if not FORECAST_STOP_TIGHTEN_ENABLED or mapped["direction"] not in ("LONG", "SHORT"):
        return None

    entry_level = mapped["entry_level"]
    direction = mapped["direction"]

    if direction == "LONG" and price <= entry_level:
        return None
    if direction == "SHORT" and price >= entry_level:
        return None

    agrees = _check_forecast_agrees(
        row["symbol"],
        direction,
        row.get("timeframe"),
        cycle_cache=forecast_cache,
    )
    if agrees:
        return None

    min_cushion = entry_level * (FORECAST_STOP_MIN_CUSHION_PCT / 100.0)
    if direction == "LONG":
        unrealized = price - entry_level
        new_stop = max(
            entry_level + (unrealized * FORECAST_STOP_TIGHTEN_RATIO),
            entry_level + min_cushion,
        )
        if new_stop <= mapped["stop_level"]:
            return None
    else:
        unrealized = entry_level - price
        new_stop = min(
            entry_level - (unrealized * FORECAST_STOP_TIGHTEN_RATIO),
            entry_level - min_cushion,
        )
        if new_stop >= mapped["stop_level"]:
            return None

    logger.info(
        "Forecast stop tighten: %s %s stop %.2f → %.2f (unrealized=%.2f, ratio=%.2f)",
        row["symbol"],
        direction,
        mapped["stop_level"],
        new_stop,
        unrealized,
        FORECAST_STOP_TIGHTEN_RATIO,
    )
    mapped["stop_level"] = new_stop
    return evaluate_outcome(mapped, price, timeframe=row.get("timeframe"))


def run_tracker_cycle() -> int:
    """Execute a single tracker cycle.

    Returns:
        Number of outcomes resolved this cycle.
    """
    if not is_market_open():
        logger.info("Market closed — skipping outcome tracker cycle")
        return 0

    resolved = 0
    forecast_cache: dict[str, bool] = {}
    try:
        rows = get_open_alerts()
    except Exception as exc:
        logger.error("Failed to fetch open alerts: %s", exc)
        return 0

    for row in rows:
        try:
            if row.get("direction") == "WATCH":
                continue

            mapped = map_db_row(row)
            if mapped is None:
                continue
            price = get_current_price(row["symbol"])
            if price is None:
                continue

            outcome = evaluate_outcome(mapped, price, timeframe=row.get("timeframe"))
            if outcome is None:
                outcome = _maybe_tighten_stop_on_forecast(
                    mapped,
                    row,
                    price,
                    forecast_cache=forecast_cache,
                )
                if outcome is None:
                    continue

            entry_level = mapped["entry_level"]
            if outcome in ("WIN", "LOSS"):
                pnl = price - entry_level if mapped["direction"] == "LONG" else entry_level - price
            else:
                pnl = 0.0

            pnl_pct = (pnl / entry_level * 100) if entry_level else None

            if outcome == "WIN":
                slippage = abs(price - mapped["target_level"])
            elif outcome == "LOSS":
                slippage = abs(price - mapped["stop_level"])
            else:
                slippage = 0.0

            update_outcome(row["id"], outcome, pnl, pnl_pct=pnl_pct)
            _post_outcome_to_langfuse(row, outcome, pnl_pct)
            logger.info(
                "Outcome: %s → %s @ %.2f (pnl=%.4f, pnl_pct=%.2f%%, slippage=%.4f)",
                row["symbol"],
                outcome,
                price,
                pnl,
                pnl_pct or 0.0,
                slippage,
            )
            resolved += 1
        except Exception as exc:
            logger.error("Error processing alert %s: %s", row.get("id", "?"), exc)
            continue

    return resolved


def _check_expiry_rate() -> None:
    """Send ops alert if daily expiry rate exceeds threshold."""
    try:
        stats = fetch_daily_expiry_stats()
        if not stats or stats["total"] < 5:
            return

        rate = stats["expired"] / stats["total"]
        if rate > _EXPIRY_RATE_THRESHOLD:
            try:
                from notifier_and_logger import send_ops_message

                send_ops_message(
                    f"⚠️ High expiry rate: {stats['expired']}/{stats['total']} "
                    f"({rate:.0%}) alerts EXPIRED in last 24h "
                    f"(threshold: {_EXPIRY_RATE_THRESHOLD:.0%}). "
                    f"Check entry levels and timeframe alignment."
                )
            except Exception as exc:
                logger.warning("Failed to send expiry rate ops alert: %s", exc)
            logger.warning(
                "High expiry rate: %d/%d (%.0f%%) in last 24h",
                stats["expired"],
                stats["total"],
                rate * 100,
            )
    except Exception as exc:
        logger.debug("Expiry rate check failed: %s", exc)


def run_tracker_loop() -> None:
    """Continuous polling loop for standalone deployment."""
    logger.info(
        "Outcome tracker started — polling every %ds",
        PRICE_POLL_INTERVAL_SECONDS,
    )
    try:
        while True:
            resolved = run_tracker_cycle()
            stale = expire_stale_alerts()
            _check_expiry_rate()
            logger.info("Tracker cycle complete: %d outcomes resolved, %d stale expired", resolved, stale)
            time.sleep(PRICE_POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("Outcome tracker stopped.")
