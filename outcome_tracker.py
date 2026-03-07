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

from db import get_recent_alerts, update_outcome
from models import PlaybookAlert  # noqa: F401 — required project import

logger = logging.getLogger(__name__)

POLYGON_API_KEY: str | None = os.getenv("POLYGON_API_KEY")
PRICE_POLL_INTERVAL_SECONDS: int = int(os.getenv("PRICE_POLL_INTERVAL", "60"))
OUTCOME_WINDOW_HOURS: int = int(os.getenv("OUTCOME_WINDOW_HOURS", "4"))


def get_current_price(symbol: str) -> float | None:
    """Fetch latest trade price from Polygon.io snapshot endpoint.

    Args:
        symbol: Ticker symbol (e.g. ``"AAPL"``).

    Returns:
        Latest close/last price, or ``None`` on any error.
    """
    url = (
        f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/"
        f"tickers/{symbol}"
    )
    try:
        resp = httpx.get(url, params={"apiKey": POLYGON_API_KEY}, timeout=10.0)
        resp.raise_for_status()
        return float(resp.json()["ticker"]["day"]["c"])
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        logger.error("Failed to fetch price for %s: %s", symbol, exc)
        return None


def evaluate_outcome(alert_row: dict, current_price: float) -> str | None:
    """Determine outcome for an alert given the current market price.

    Args:
        alert_row: Dict with keys ``direction``, ``entry_level``,
            ``stop_level``, ``target_level``, ``fired_at`` (datetime).
        current_price: Latest market price for the symbol.

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

        # Check expiry window
        now = datetime.now(timezone.utc)
        if isinstance(fired_at, datetime):
            deadline = fired_at + timedelta(hours=OUTCOME_WINDOW_HOURS)
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

            mapped = _map_db_row(row)
            price = get_current_price(row["symbol"])
            if price is None:
                continue

            outcome = evaluate_outcome(mapped, price)
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

    mock_alert: dict = {
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
    result = evaluate_outcome(mock_alert, 193.0)
    assert result == "WIN", f"Expected WIN, got {result}"

    # Test LOSS
    result = evaluate_outcome(mock_alert, 181.0)
    assert result == "LOSS", f"Expected LOSS, got {result}"

    # Test OPEN (within window, price between stop and target)
    result = evaluate_outcome(mock_alert, 186.0)
    assert result is None, f"Expected None (open), got {result}"

    # Test EXPIRED (past window)
    expired_alert: dict = {
        **mock_alert,
        "fired_at": datetime.now(timezone.utc) - timedelta(hours=5),
    }
    result = evaluate_outcome(expired_alert, 186.0)
    assert result == "EXPIRED", f"Expected EXPIRED, got {result}"

    # Test SHORT WIN
    short_alert: dict = {
        **mock_alert,
        "direction": "SHORT",
        "entry_level": 185.0,
        "stop_level": 188.0,
        "target_level": 178.0,
    }
    result = evaluate_outcome(short_alert, 177.0)
    assert result == "WIN", f"Expected SHORT WIN, got {result}"

    print("All evaluate_outcome tests passed ✅")
    print("Outcome tracker dry-run complete ✅")
