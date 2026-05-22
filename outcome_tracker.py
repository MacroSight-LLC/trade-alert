"""Outcome tracker — resolves open alerts as WIN / LOSS / EXPIRED.

SSOT Reference: §12 — Postgres Schema & Analytics.
Polls Polygon.io for current prices, evaluates each open alert against
its target / stop levels, and writes the result back to Postgres via
``db.update_outcome()``.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta

import httpx

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from constants import is_market_open
from db import get_open_alerts, update_outcome
from models import PlaybookAlert  # noqa: F401 — required project import

logger = logging.getLogger(__name__)

PRICE_POLL_INTERVAL_SECONDS: int = int(os.getenv("PRICE_POLL_INTERVAL", "60"))
OUTCOME_WINDOW_HOURS: int = int(os.getenv("OUTCOME_WINDOW_HOURS", "4"))  # default fallback
OUTCOME_OPEN_ALERT_LIMIT: int = int(os.getenv("OUTCOME_OPEN_ALERT_LIMIT", "200"))
STALE_ALERT_DAYS: int = int(os.getenv("STALE_ALERT_DAYS", "7"))
PRICE_FETCH_MAX_RETRIES: int = int(os.getenv("PRICE_FETCH_MAX_RETRIES", "3"))
PRICE_FETCH_TIMEOUT: float = float(os.getenv("PRICE_FETCH_TIMEOUT", "10.0"))

# Forecast-based stop tightening (feature-flagged)
FORECAST_STOP_TIGHTEN_ENABLED: bool = os.getenv("FORECAST_STOP_TIGHTEN_ENABLED", "false").lower() == "true"
FORECAST_STOP_TIGHTEN_RATIO: float = float(os.getenv("FORECAST_STOP_TIGHTEN_RATIO", "0.50"))
FORECAST_STOP_MIN_CUSHION_PCT: float = float(os.getenv("FORECAST_STOP_MIN_CUSHION_PCT", "1.0"))
_TIMESFM_MCP_URL: str = os.getenv("TIMESFM_MCP_URL", "http://timesfm-mcp:8012")

# Per-timeframe outcome expiry windows (hours)
# 15m alerts should resolve faster than 1h alerts.
_TIMEFRAME_EXPIRY_HOURS: dict[str, int] = {
    "5m": 1,
    "15m": 2,
    "1h": 6,
    "4h": 16,
    "1D": 48,
}

_http_client: httpx.Client | None = None


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
    """Post outcome score to the originating Langfuse trace.

    Links the resolved outcome back to the pipeline trace that produced
    the alert, enabling filtering traces by actual performance.

    Args:
        row: Alert row dict from the DB (must have ``langfuse_trace_id``).
        outcome: Resolved outcome (WIN, LOSS, EXPIRED, etc).
        pnl_pct: PnL percentage (optional).
    """
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


# ── Price source chain ───────────────────────────────────────────


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
        price = resp.json().get("c")  # current price
        if price and float(price) > 0:
            return float(price)
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        logger.debug("Finnhub quote failed for %s: %s", symbol, exc)
    return None


def get_current_price(symbol: str) -> float | None:
    """Fetch latest price using a multi-source fallback chain.

    Equities: Finnhub /quote (real-time) → Polygon prev-day close → None

    Finnhub is primary because it returns the *current* market price,
    whereas Polygon free-tier only provides the previous-day close —
    which can misclassify WIN/LOSS on intraday 15m alerts.

    Args:
        symbol: Ticker symbol (e.g. ``"AAPL"``).

    Returns:
        Latest price, or ``None`` if all sources fail.
    """
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
    """Check if TimesFM forecast still agrees with alert direction.

    Calls the TimesFM MCP ``/tool/validate`` endpoint.  Results are
    cached per-symbol within the current tracker cycle to avoid
    redundant inference calls.

    Args:
        symbol: Ticker symbol.
        direction: Alert direction (``"LONG"`` or ``"SHORT"``).
        timeframe: Alert timeframe.
        cycle_cache: Mutable dict for per-cycle result caching.

    Returns:
        ``True`` if forecast agrees (or MCP unavailable), ``False`` if it contradicts.
    """
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
        return agrees
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        logger.debug("Forecast check unavailable for %s: %s", symbol, exc)
        cycle_cache[symbol] = True  # default: agree (non-blocking)
        return True


def evaluate_outcome(
    alert_row: dict,
    current_price: float,
    *,
    timeframe: str | None = None,
) -> str | None:
    """Determine outcome for an alert given the current market price.

    Args:
        alert_row: Dict with keys ``direction``, ``entry_level``,
            ``stop_level``, ``target_level``, ``fired_at`` (datetime).
        current_price: Latest market price for the symbol.
        timeframe: Alert timeframe for per-timeframe expiry windows.
            Falls back to ``OUTCOME_WINDOW_HOURS`` env var if not given.

    Returns:
        ``"WIN"``, ``"LOSS"``, ``"EXPIRED"``, or ``None`` (still open).
    """
    try:
        direction: str = alert_row["direction"]
        stop_level: float = float(alert_row["stop_level"])
        target_level: float = float(alert_row["target_level"])
        fired_at: datetime = alert_row["fired_at"]

        if direction == "LONG":
            if current_price >= target_level:
                return "WIN"
            if current_price <= stop_level:
                return "LOSS"
        elif direction == "SHORT":
            if current_price <= target_level:
                return "WIN"
            if current_price >= stop_level:
                return "LOSS"
        else:
            logger.warning("Unknown direction '%s' — cannot evaluate outcome", direction)
            return None

        # Check expiry window (per-timeframe if available)
        expiry_hours = _TIMEFRAME_EXPIRY_HOURS.get(timeframe or "", OUTCOME_WINDOW_HOURS)
        now = datetime.now(UTC)
        if isinstance(fired_at, datetime):
            deadline = fired_at + timedelta(hours=expiry_hours)
            if now >= deadline:
                return "EXPIRED"

        return None
    except (KeyError, TypeError, ValueError) as exc:
        logger.error("evaluate_outcome error: %s", exc)
        return None


def _map_db_row(row: dict) -> dict | None:
    """Transform a raw Postgres alert row into the flat format expected
    by ``evaluate_outcome``.

    Args:
        row: Dict from ``get_open_alerts()`` (JSONB ``entry`` column).

    Returns:
        Flat dict with ``entry_level``, ``stop_level``, ``target_level``,
        ``fired_at``, plus passthrough of other keys.  Returns ``None``
        if required price data is missing or non-positive.
    """
    entry = row.get("entry", {})
    if isinstance(entry, str):
        try:
            entry = json.loads(entry)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.error("Malformed entry JSON for alert %s: %s", row.get("id"), exc)
            return None

    # Validate required entry keys — reject rows with missing keys
    for key in ("level", "stop", "target"):
        if key not in entry:
            logger.error(
                "Missing '%s' key in entry for alert %s — skipping",
                key,
                row.get("id"),
            )
            return None

    entry_level = float(entry.get("level", 0))
    stop_level = float(entry.get("stop", 0))
    target_level = float(entry.get("target", 0))

    # Reject non-positive prices — these indicate corrupted data
    if entry_level <= 0 or stop_level <= 0 or target_level <= 0:
        logger.error(
            "Non-positive price for alert %s: level=%.4f stop=%.4f target=%.4f — skipping",
            row.get("id"),
            entry_level,
            stop_level,
            target_level,
        )
        return None

    return {
        **row,
        "entry_level": entry_level,
        "stop_level": stop_level,
        "target_level": target_level,
        "fired_at": row.get("created_at"),
    }


def run_tracker_cycle() -> int:
    """Execute a single tracker cycle.

    Fetches open alerts, polls current prices, evaluates outcomes, and
    writes resolved results back to Postgres.  Skips evaluation when
    US equity markets are closed to avoid stale-price false resolutions.

    Returns:
        Number of outcomes resolved this cycle.
    """
    if not is_market_open():
        logger.info("Market closed — skipping outcome tracker cycle")
        return 0

    resolved = 0
    forecast_cache: dict[str, bool] = {}  # per-cycle forecast result cache
    try:
        rows = get_open_alerts(limit=OUTCOME_OPEN_ALERT_LIMIT)
    except Exception as exc:
        logger.error("Failed to fetch open alerts: %s", exc)
        return 0

    for row in rows:
        try:
            # WATCH alerts have no directional play — skip tracking
            if row.get("direction") == "WATCH":
                continue

            mapped = _map_db_row(row)
            if mapped is None:
                continue
            price = get_current_price(row["symbol"])
            if price is None:
                continue

            outcome = evaluate_outcome(
                mapped,
                price,
                timeframe=row.get("timeframe"),
            )
            if outcome is None:
                # ── Forecast-based stop tightening ─────────────────
                # If alert is still open AND in profit AND forecast
                # now disagrees, tighten the stop to protect gains.
                if FORECAST_STOP_TIGHTEN_ENABLED and mapped["direction"] in ("LONG", "SHORT"):
                    entry_level = mapped["entry_level"]
                    if mapped["direction"] == "LONG" and price > entry_level:
                        unrealized = price - entry_level
                        agrees = _check_forecast_agrees(
                            row["symbol"],
                            mapped["direction"],
                            row.get("timeframe"),
                            cycle_cache=forecast_cache,
                        )
                        if not agrees:
                            new_stop = entry_level + (unrealized * FORECAST_STOP_TIGHTEN_RATIO)
                            # Enforce minimum cushion so stop isn't set
                            # dangerously close to entry level
                            min_cushion = entry_level * (FORECAST_STOP_MIN_CUSHION_PCT / 100.0)
                            new_stop = max(new_stop, entry_level + min_cushion)
                            if new_stop > mapped["stop_level"]:
                                logger.info(
                                    "Forecast stop tighten: %s LONG stop %.2f → %.2f "
                                    "(unrealized=%.2f, ratio=%.2f)",
                                    row["symbol"],
                                    mapped["stop_level"],
                                    new_stop,
                                    unrealized,
                                    FORECAST_STOP_TIGHTEN_RATIO,
                                )
                                mapped["stop_level"] = new_stop
                                outcome = evaluate_outcome(mapped, price, timeframe=row.get("timeframe"))
                    elif mapped["direction"] == "SHORT" and price < entry_level:
                        unrealized = entry_level - price
                        agrees = _check_forecast_agrees(
                            row["symbol"],
                            mapped["direction"],
                            row.get("timeframe"),
                            cycle_cache=forecast_cache,
                        )
                        if not agrees:
                            new_stop = entry_level - (unrealized * FORECAST_STOP_TIGHTEN_RATIO)
                            # Enforce minimum cushion so stop isn't set
                            # dangerously close to entry level
                            min_cushion = entry_level * (FORECAST_STOP_MIN_CUSHION_PCT / 100.0)
                            new_stop = min(new_stop, entry_level - min_cushion)
                            if new_stop < mapped["stop_level"]:
                                logger.info(
                                    "Forecast stop tighten: %s SHORT stop %.2f → %.2f "
                                    "(unrealized=%.2f, ratio=%.2f)",
                                    row["symbol"],
                                    mapped["stop_level"],
                                    new_stop,
                                    unrealized,
                                    FORECAST_STOP_TIGHTEN_RATIO,
                                )
                                mapped["stop_level"] = new_stop
                                outcome = evaluate_outcome(mapped, price, timeframe=row.get("timeframe"))

                continue

            # Calculate PnL
            entry_level = mapped["entry_level"]
            if outcome in ("WIN", "LOSS"):
                if mapped["direction"] == "LONG":
                    pnl = price - entry_level
                else:
                    pnl = entry_level - price
            else:
                pnl = 0.0

            pnl_pct = (pnl / entry_level * 100) if entry_level else None

            # Slippage tracking: actual resolution price vs target/stop
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
            logger.error(
                "Error processing alert %s: %s",
                row.get("id", "?"),
                exc,
            )
            continue

    return resolved


def _expire_stale_alerts() -> int:
    """Auto-expire open alerts older than STALE_ALERT_DAYS.

    Returns:
        Number of alerts expired this cycle.
    """
    expired = 0
    try:
        rows = get_open_alerts(limit=OUTCOME_OPEN_ALERT_LIMIT)
        now = datetime.now(UTC)
        for row in rows:
            created = row.get("created_at")
            if isinstance(created, datetime) and (now - created).days >= STALE_ALERT_DAYS:
                update_outcome(row["id"], "EXPIRED", 0.0, pnl_pct=0.0)
                logger.info(
                    "Expired stale alert %s (id=%s, age=%dd)",
                    row.get("symbol"),
                    row["id"],
                    (now - created).days,
                )
                expired += 1
    except Exception as exc:
        logger.error("Stale alert cleanup failed: %s", exc)
    return expired


_EXPIRY_RATE_THRESHOLD: float = float(os.getenv("EXPIRY_RATE_THRESHOLD", "0.15"))


def _check_expiry_rate() -> None:
    """Send ops alert if daily expiry rate exceeds threshold.

    Queries resolved alerts from the last 24h and computes the
    EXPIRED/(WIN+LOSS+EXPIRED) ratio. If > 15%, sends a warning to ops.
    """
    try:
        from psycopg2.extras import RealDictCursor

        from db import _put_conn, get_conn

        sql = """
            SELECT
                COALESCE(SUM(CASE WHEN outcome = 'EXPIRED' THEN 1 ELSE 0 END), 0) AS expired,
                COALESCE(SUM(CASE WHEN outcome IN ('WIN','LOSS','EXPIRED') THEN 1 ELSE 0 END), 0) AS total
            FROM alerts
            WHERE outcome IS NOT NULL
              AND updated_at >= NOW() - INTERVAL '24 hours'
        """
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql)
                row = cur.fetchone()
        finally:
            _put_conn(conn)

        if not row or row["total"] < 5:
            return  # not enough data to judge

        rate = row["expired"] / row["total"]
        if rate > _EXPIRY_RATE_THRESHOLD:
            try:
                from notifier_and_logger import send_ops_message

                send_ops_message(
                    f"⚠️ High expiry rate: {row['expired']}/{row['total']} "
                    f"({rate:.0%}) alerts EXPIRED in last 24h "
                    f"(threshold: {_EXPIRY_RATE_THRESHOLD:.0%}). "
                    f"Check entry levels and timeframe alignment."
                )
            except Exception as exc:
                logger.warning("Failed to send expiry rate ops alert: %s", exc)
            logger.warning(
                "High expiry rate: %d/%d (%.0f%%) in last 24h",
                row["expired"],
                row["total"],
                rate * 100,
            )
    except Exception as exc:
        logger.debug("Expiry rate check failed: %s", exc)


def run_tracker_loop() -> None:
    """Continuous polling loop for standalone deployment.

    Calls ``run_tracker_cycle()`` every ``PRICE_POLL_INTERVAL_SECONDS``
    until interrupted.
    """
    logger.info(
        "Outcome tracker started — polling every %ds",
        PRICE_POLL_INTERVAL_SECONDS,
    )
    try:
        while True:
            resolved = run_tracker_cycle()
            stale = _expire_stale_alerts()
            _check_expiry_rate()
            logger.info("Tracker cycle complete: %d outcomes resolved, %d stale expired", resolved, stale)
            time.sleep(PRICE_POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("Outcome tracker stopped.")


if __name__ == "__main__":
    from datetime import datetime, timedelta

    sample_alert: dict = {
        "id": 1,
        "symbol": "AAPL",
        "direction": "LONG",
        "entry_level": 185.0,
        "stop_level": 182.0,
        "target_level": 192.0,
        "fired_at": datetime.now(UTC) - timedelta(hours=1),
        "outcome": None,
    }

    # Test WIN
    result = evaluate_outcome(sample_alert, 193.0)
    assert result == "WIN", f"Expected WIN, got {result}"

    # Test LOSS
    result = evaluate_outcome(sample_alert, 181.0)
    assert result == "LOSS", f"Expected LOSS, got {result}"

    # Test OPEN (within window, price between stop and target)
    result = evaluate_outcome(sample_alert, 186.0)
    assert result is None, f"Expected None (open), got {result}"

    # Test EXPIRED (past window)
    expired_alert: dict = {
        **sample_alert,
        "fired_at": datetime.now(UTC) - timedelta(hours=5),
    }
    result = evaluate_outcome(expired_alert, 186.0)
    assert result == "EXPIRED", f"Expected EXPIRED, got {result}"

    # Test SHORT WIN
    short_alert: dict = {
        **sample_alert,
        "direction": "SHORT",
        "entry_level": 185.0,
        "stop_level": 188.0,
        "target_level": 178.0,
    }
    result = evaluate_outcome(short_alert, 177.0)
    assert result == "WIN", f"Expected SHORT WIN, got {result}"

    print("All evaluate_outcome tests passed ✅")
    print("Outcome tracker dry-run complete ✅")
