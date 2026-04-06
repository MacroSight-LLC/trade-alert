"""Centralized constants for Redis keys, TTLs, and shared thresholds.

Eliminates magic strings scattered across merger, notifier, healthcheck,
discord_bot, and validate_and_filter modules.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from datetime import time as dt_time
from zoneinfo import ZoneInfo

# ── Redis Key Patterns ─────────────────────────────────────────────

SNAPSHOT_KEY_PREFIX: str = "snapshots:"
"""Redis list key prefix. Full key: ``snapshots:{timeframe}``."""

MACRO_REGIME_KEY: str = "macro:regime"
"""Redis string key holding the current macro regime JSON dict."""

DEDUP_KEY_PREFIX: str = "alert:dedup:"
"""Redis string key prefix for alert dedup. Full key: ``alert:dedup:{sym}:{dir}:{tf}:{hash}``."""

THESIS_KEY_PREFIX: str = "alert:thesis:"
"""Redis string key prefix for thesis text. Full key: ``alert:thesis:{sym}:{dir}:{tf}``."""

UNIVERSE_KEY: str = "universe:equities"
"""Redis string key for the screened equity symbol universe."""


# ── TTL Constants (seconds) ────────────────────────────────────────

SNAPSHOT_TTL: int = int(os.environ.get("SNAPSHOT_TTL", "900"))
"""TTL for snapshot queue entries. Default 15 minutes (aligned with cron cadence)."""

DEDUP_WINDOW_SECONDS: int = int(os.environ.get("DEDUP_WINDOW_SECONDS", "900"))
"""Alert deduplication window. Default 15 minutes."""

MACRO_STALE_SECONDS: int = int(os.environ.get("MACRO_STALE_SECONDS", "1800"))
"""Macro data staleness threshold. Default 30 minutes."""


# ── Operational Limits ─────────────────────────────────────────────

LRANGE_CAP: int = int(os.environ.get("LRANGE_CAP", "500"))
"""Max entries read per snapshot queue in merger."""

SNAPSHOT_STALE_TTL_THRESHOLD: int = int(os.environ.get("SNAPSHOT_STALE_THRESHOLD", "100"))
"""TTL (seconds) below which a snapshot key is considered stale in healthcheck."""


# ── Market Hours & Holidays ────────────────────────────────────────

# US market holidays for 2025-2027 (NYSE observed).
# Extend annually or replace with exchange_calendars package.
MARKET_HOLIDAYS: frozenset[tuple[int, int, int]] = frozenset(
    {
        # 2025
        (2025, 1, 1),
        (2025, 1, 20),
        (2025, 2, 17),
        (2025, 4, 18),
        (2025, 5, 26),
        (2025, 6, 19),
        (2025, 7, 4),
        (2025, 9, 1),
        (2025, 11, 27),
        (2025, 12, 25),
        # 2026
        (2026, 1, 1),
        (2026, 1, 19),
        (2026, 2, 16),
        (2026, 4, 3),
        (2026, 5, 25),
        (2026, 6, 19),
        (2026, 7, 3),
        (2026, 9, 7),
        (2026, 11, 26),
        (2026, 12, 25),
        # 2027
        (2027, 1, 1),
        (2027, 1, 18),
        (2027, 2, 15),
        (2027, 3, 26),
        (2027, 5, 31),
        (2027, 6, 18),
        (2027, 7, 5),
        (2027, 9, 6),
        (2027, 11, 25),
        (2027, 12, 24),
    }
)

# NYSE early-close days (1:00 PM ET). Extend annually.
MARKET_EARLY_CLOSES: frozenset[tuple[int, int, int]] = frozenset(
    {
        # 2025
        (2025, 7, 3),
        (2025, 11, 28),
        (2025, 12, 24),
        # 2026
        (2026, 11, 27),
        (2026, 12, 24),
        # 2027
        (2027, 11, 26),
    }
)

_logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")

# Try to load exchange_calendars for dynamic holiday data;
# fall back to the hardcoded frozensets above.
_xcal_nyse = None
try:
    import exchange_calendars  # type: ignore[import-untyped]

    _xcal_nyse = exchange_calendars.get_calendar("XNYS")
except (ImportError, Exception) as _exc:  # noqa: BLE001
    _logger.debug("exchange_calendars unavailable — using hardcoded holidays: %s", _exc)


def is_holiday(d: date | None = None) -> bool:
    """Return True if *d* is an NYSE holiday.

    Args:
        d: Date to check.  Defaults to today in ET.
    """
    if d is None:
        d = datetime.now(tz=_ET).date()
    if _xcal_nyse is not None:
        import pandas as pd

        ts = pd.Timestamp(d)
        return not _xcal_nyse.is_session(ts)  # not a session = holiday
    return (d.year, d.month, d.day) in MARKET_HOLIDAYS


def is_early_close(d: date | None = None) -> bool:
    """Return True if *d* is an NYSE early-close day (1 PM ET close).

    Args:
        d: Date to check.  Defaults to today in ET.
    """
    if d is None:
        d = datetime.now(tz=_ET).date()
    if _xcal_nyse is not None:
        import pandas as pd

        ts = pd.Timestamp(d)
        if _xcal_nyse.is_session(ts):
            close_time = _xcal_nyse.session_close(ts)
            # NYSE early closes end at 18:00 UTC (1 PM ET) vs normal 21:00 UTC
            return close_time.hour < 20
        return False
    return (d.year, d.month, d.day) in MARKET_EARLY_CLOSES


def is_market_open(now: datetime | None = None) -> bool:
    """Return True if US equity markets are in regular or extended hours.

    Extended hours window: Mon-Fri 04:00–20:00 ET, excluding holidays.
    On early-close days the regular session ends at 13:00 ET but
    after-hours still run until 20:00 ET.

    Args:
        now: Datetime to check.  Defaults to now in ET.
    """
    if now is None:
        now = datetime.now(tz=_ET)
    now_et = now.astimezone(_ET)
    if now_et.weekday() >= 5:
        return False
    if is_holiday(now_et.date()):
        return False
    return 4 <= now_et.hour < 20


def get_market_hours_status(now: datetime | None = None) -> str:
    """Return a human-readable US equity market hours status string.

    Args:
        now: Datetime to check.  Defaults to now in ET.

    Returns:
        String like ``"Regular Trading Hours (14:32 ET)"``.
    """
    if now is None:
        now = datetime.now(tz=_ET)
    now_et = now.astimezone(_ET)
    weekday = now_et.weekday()
    t = now_et.time()
    ts = now_et.strftime("%H:%M")

    if weekday >= 5:
        return f"Market Closed (weekend, {now_et.strftime('%A')} {ts} ET)"

    if is_holiday(now_et.date()):
        return f"Market Closed (holiday, {ts} ET)"

    early = is_early_close(now_et.date())
    regular_close = dt_time(13, 0) if early else dt_time(16, 0)
    suffix = " [early close]" if early else ""

    if t < dt_time(4, 0):
        return f"Market Closed (overnight, {ts} ET){suffix}"
    if t < dt_time(9, 30):
        return f"Pre-market ({ts} ET){suffix}"
    if t < regular_close:
        return f"Regular Trading Hours ({ts} ET){suffix}"
    if t < dt_time(20, 0):
        return f"After-hours ({ts} ET){suffix}"
    return f"Market Closed (post-session, {ts} ET){suffix}"
