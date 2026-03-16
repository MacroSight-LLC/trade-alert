"""Outcome tracker — resolves open alerts as WIN / LOSS / EXPIRED.

SSOT Reference: §12 — Postgres Schema & Analytics.
Polls Polygon.io for current prices, evaluates each open alert against
its target / stop levels, and writes the result back to Postgres via
``db.update_outcome()``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from db import get_recent_alerts, update_outcome
from models import PlaybookAlert  # noqa: F401 — required project import

logger = logging.getLogger(__name__)

PRICE_POLL_INTERVAL_SECONDS: int = int(os.getenv("PRICE_POLL_INTERVAL", "60"))
OUTCOME_WINDOW_HOURS: int = int(os.getenv("OUTCOME_WINDOW_HOURS", "4"))  # default fallback
PRICE_FETCH_MAX_RETRIES: int = int(os.getenv("PRICE_FETCH_MAX_RETRIES", "3"))
PRICE_FETCH_TIMEOUT: float = float(os.getenv("PRICE_FETCH_TIMEOUT", "10.0"))

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


def _get_http_client() -> httpx.Client:
    """Return a module-level HTTP client with connection pooling."""
    global _http_client  # noqa: PLW0603
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=PRICE_FETCH_TIMEOUT, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _http_client


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
        now = datetime.now(timezone.utc)
        if isinstance(fired_at, datetime):
            deadline = fired_at + timedelta(hours=expiry_hours)
            if now >= deadline:
                return "EXPIRED"

        return None
    except (KeyError, TypeError, ValueError) as exc:
        logger.error("evaluate_outcome error: %s", exc)
        return None


def _map_db_row(row: dict) -> dict:
    """Transform a raw Postgres alert row into the flat format expected
    by ``evaluate_outcome``.

    Args:
        row: Dict from ``get_recent_alerts()`` (JSONB ``entry`` column).

    Returns:
        Flat dict with ``entry_level``, ``stop_level``, ``target_level``,
        ``fired_at``, plus passthrough of other keys.
    """
    entry = row.get("entry", {})
    if isinstance(entry, str):
        entry = json.loads(entry)

    return {
        **row,
        "entry_level": float(entry.get("level", 0)),
        "stop_level": float(entry.get("stop", 0)),
        "target_level": float(entry.get("target", 0)),
        "fired_at": row.get("created_at"),
    }


def run_tracker_cycle() -> int:
    """Execute a single tracker cycle.

    Fetches open alerts, polls current prices, evaluates outcomes, and
    writes resolved results back to Postgres.

    Returns:
        Number of outcomes resolved this cycle.
    """
    resolved = 0
    try:
        rows = get_recent_alerts(limit=50)
    except Exception as exc:
        logger.error("Failed to fetch recent alerts: %s", exc)
        return 0

    for row in rows:
        try:
            # Skip already-resolved alerts
            if row.get("outcome") is not None:
                continue

            # WATCH alerts have no directional play — skip tracking
            if row.get("direction") == "WATCH":
                continue

            mapped = _map_db_row(row)
            price = get_current_price(row["symbol"])
            if price is None:
                continue

            outcome = evaluate_outcome(
                mapped,
                price,
                timeframe=row.get("timeframe"),
            )
            if outcome is None:
                continue

            # Map EXPIRED → SCRATCH for DB (schema CHECK constraint)
            db_outcome = "SCRATCH" if outcome == "EXPIRED" else outcome

            # Calculate PnL
            entry_level = mapped["entry_level"]
            if outcome in ("WIN", "LOSS"):
                if mapped["direction"] == "LONG":
                    pnl = price - entry_level
                else:
                    pnl = entry_level - price
            else:
                pnl = 0.0

            update_outcome(row["id"], db_outcome, pnl)
            logger.info(
                "Outcome: %s → %s @ %.2f (pnl=%.4f)",
                row["symbol"],
                outcome,
                price,
                pnl,
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
            logger.info("Tracker cycle complete: %d outcomes resolved", resolved)
            time.sleep(PRICE_POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("Outcome tracker stopped.")


if __name__ == "__main__":
    from datetime import datetime, timedelta, timezone

    sample_alert: dict = {
        "id": 1,
        "symbol": "AAPL",
        "direction": "LONG",
        "entry_level": 185.0,
        "stop_level": 182.0,
        "target_level": 192.0,
        "fired_at": datetime.now(timezone.utc) - timedelta(hours=1),
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
        "fired_at": datetime.now(timezone.utc) - timedelta(hours=5),
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
