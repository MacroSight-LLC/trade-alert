"""Discord notifier and Postgres logger for trade-alert.

Receives validated PlaybookAlert JSON from the decision engine,
formats rich Discord embeds, sends via webhook or bot API,
and logs each alert to Postgres.
Implements SSOT §11.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import time
from atexit import register as _atexit_register
from datetime import datetime, timezone

import httpx
import redis
from pydantic import ValidationError

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from chart_gen import generate_chart
from db import insert_alert
from log_config import configure_logging
from metrics import DB_INSERTS, DISCORD_SENDS
from models import PlaybookAlert
from redis_client import get_redis

configure_logging()
logger = logging.getLogger(__name__)

DISCORD_HTTP_TIMEOUT: float = float(os.getenv("DISCORD_HTTP_TIMEOUT", "10.0"))
DISCORD_SEND_MAX_RETRIES: int = int(os.getenv("DISCORD_SEND_MAX_RETRIES", "3"))
DISCORD_SEND_BACKOFF_BASE: float = float(os.getenv("DISCORD_SEND_BACKOFF_BASE", "1.0"))
from constants import DEDUP_WINDOW_SECONDS, TRADE_EXECUTE_ENABLED  # centralized
from execution_mapper import map_to_execution_trigger
from execution_webhook import deliver_execution_trigger

MAX_ALERTS_PER_CYCLE: int = int(os.getenv("MAX_ALERTS_PER_CYCLE", "5"))

_discord_client: httpx.Client | None = None

# Discord circuit breaker: fast-fail remaining batch when Discord is down
_DISCORD_CB_THRESHOLD: int = int(os.getenv("DISCORD_CB_THRESHOLD", "2"))
_DISCORD_CB_RESET_SECS: float = float(os.getenv("DISCORD_CB_RESET_SECS", "120.0"))
_discord_consecutive_failures: int = 0
_discord_cb_open_since: float = 0.0  # monotonic timestamp when CB opened

# Maximum field length for Discord embed fields (Discord limit is 1024)
_MAX_FIELD_LEN: int = 1000

# Tiered channel routing by alert quality
_DISCORD_CHANNEL_HIGH: str | None = os.getenv("DISCORD_CHANNEL_HIGH")
_DISCORD_CHANNEL_STANDARD: str | None = os.getenv("DISCORD_CHANNEL_STANDARD")
_DISCORD_CHANNEL_WATCH: str | None = os.getenv("DISCORD_CHANNEL_WATCH")


def _get_discord_client() -> httpx.Client:
    """Return a module-level HTTP client for Discord API calls."""
    global _discord_client  # noqa: PLW0603
    if _discord_client is None or _discord_client.is_closed:
        _discord_client = httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=DISCORD_HTTP_TIMEOUT, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
        )
        _atexit_register(_close_http_client)
    return _discord_client


def _close_http_client() -> None:
    """Close the module-level HTTP client on process exit."""
    global _discord_client  # noqa: PLW0603
    if _discord_client is not None and not _discord_client.is_closed:
        try:
            _discord_client.close()
        except Exception:  # noqa: BLE001
            pass
        _discord_client = None


def _discord_webhook() -> str | None:
    return os.getenv("DISCORD_WEBHOOK")


def _discord_bot_token() -> str | None:
    return os.getenv("DISCORD_BOT_TOKEN")


def _discord_alert_channel_id() -> str | None:
    return os.getenv("DISCORD_ALERT_CHANNEL_ID")


def _discord_ops_channel_id() -> str | None:
    return os.getenv("DISCORD_OPS_CHANNEL_ID")


_COLOR_MAP: dict[str, int] = {
    "LONG": 3066993,  # #2ECC71 green
    "SHORT": 15158332,  # #E74C3C red
    "WATCH": 3447003,  # #3498DB blue
}

# Confidence-tier color overrides (composite quality → embed color)
_QUALITY_COLORS: list[tuple[float, int]] = [
    (0.80, 3066993),  # > 0.80 → green  #2ECC71
    (0.65, 15905331),  # 0.65–0.80 → amber #F2C43D
    (0.00, 15158332),  # < 0.65 → red    #E74C3C
]

_DIRECTION_EMOJI: dict[str, str] = {
    "LONG": "\U0001f7e2",  # 🟢
    "SHORT": "\U0001f534",  # 🔴
    "WATCH": "\U0001f535",  # 🔵
}


def _score_bar(value: float, segments: int = 10) -> str:
    """Build a visual Unicode progress bar for a 0-1 value.

    Args:
        value: Float between 0.0 and 1.0.
        segments: Number of bar segments.

    Returns:
        Unicode bar string like ``▓▓▓▓▓▓▓░░░ 70%``.
    """
    filled = round(value * segments)
    return "▓" * filled + "░" * (segments - filled) + f" {value * 100:.0f}%"


def compute_rr(entry: dict[str, float]) -> str:
    """Compute reward:risk ratio from entry dict.

    Args:
        entry: Dict with keys ``level``, ``stop``, ``target``.

    Returns:
        Formatted string e.g. ``"2.3:1"``, or ``"N/A"`` on division by zero.
    """
    try:
        risk = abs(entry["level"] - entry["stop"])
        if risk == 0:
            return "N/A"
        reward = abs(entry["target"] - entry["level"])
        return f"{reward / risk:.1f}:1"
    except (KeyError, TypeError, ZeroDivisionError):
        return "N/A"


def _edge_label(ep: float) -> str:
    """Map edge probability to a human urgency label."""
    if ep >= 0.80:
        return "\u26a1 HIGH EDGE"
    if ep >= 0.65:
        return "\u2705 SOLID EDGE"
    return "\u26a0\ufe0f MODERATE"


def _truncate_field(text: str, max_len: int = _MAX_FIELD_LEN) -> str:
    """Truncate text to fit Discord's field length limit.

    Args:
        text: Raw text.
        max_len: Maximum allowed characters.

    Returns:
        Truncated string with ellipsis if needed.
    """
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _quality_color(alert: PlaybookAlert) -> int:
    """Return embed color based on alert composite quality.

    Blends EP × confidence as a simple quality proxy.
    """
    quality = alert.edge_probability * alert.confidence
    for threshold, color in _QUALITY_COLORS:
        if quality >= threshold:
            return color
    return _COLOR_MAP.get(alert.direction, 3447003)


def _get_similar_alert_stats(symbol: str, direction: str, ep: float) -> str:
    """Query Postgres for historical win-rate of similar alerts.

    Args:
        symbol: Ticker symbol.
        direction: LONG/SHORT/WATCH.
        ep: Edge probability (for \u00b10.05 bucket matching).

    Returns:
        Human-readable string for embed, or empty string.
    """
    try:
        from db import _put_conn, get_conn

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT outcome, COUNT(*) as cnt
                    FROM alerts
                    WHERE symbol = %s
                      AND direction = %s
                      AND edge_probability BETWEEN %s AND %s
                      AND outcome IN ('WIN', 'LOSS')
                      AND created_at > NOW() - INTERVAL '30 days'
                    GROUP BY outcome
                    """,
                    (symbol, direction, ep - 0.05, ep + 0.05),
                )
                rows = cur.fetchall()
        finally:
            _put_conn(conn)

        if not rows:
            return "\U0001f4ca First alert for this setup"

        wins = sum(r[1] for r in rows if r[0] == "WIN")
        total = sum(r[1] for r in rows)
        if total < 2:
            return "\U0001f4ca First alert for this setup"
        pct = int(wins / total * 100)
        return f"\U0001f4ca Similar past alerts: {pct}% win rate (N={total})"
    except Exception:
        return ""


def _batch_similar_alert_stats(
    alerts: list[PlaybookAlert],
) -> dict[str, str]:
    """Batch-fetch historical win-rate stats for multiple alerts in one query.

    Avoids N+1 DB round-trips by fetching all matching rows in a single
    query and grouping results client-side.

    Args:
        alerts: List of PlaybookAlert instances to look up.

    Returns:
        Mapping of ``"{symbol}:{direction}"`` → human-readable stats string.
    """
    if not alerts:
        return {}
    try:
        from db import _put_conn, get_conn

        # Build (symbol, direction, ep_low, ep_high) tuples
        lookups: list[tuple[str, str, float, float]] = []
        for a in alerts:
            lookups.append((a.symbol, a.direction, a.edge_probability - 0.05, a.edge_probability + 0.05))

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                # Use ANY(ARRAY[...]) to batch the query
                cur.execute(
                    """
                    SELECT a.symbol, a.direction, a.outcome, COUNT(*) as cnt
                    FROM alerts a
                    INNER JOIN (
                        SELECT unnest(%s::text[]) AS symbol,
                               unnest(%s::text[]) AS direction,
                               unnest(%s::float8[]) AS ep_low,
                               unnest(%s::float8[]) AS ep_high
                    ) lookup ON a.symbol = lookup.symbol
                            AND a.direction = lookup.direction
                            AND a.edge_probability BETWEEN lookup.ep_low AND lookup.ep_high
                    WHERE a.outcome IN ('WIN', 'LOSS')
                      AND a.created_at > NOW() - INTERVAL '30 days'
                    GROUP BY a.symbol, a.direction, a.outcome
                    """,
                    (
                        [t[0] for t in lookups],
                        [t[1] for t in lookups],
                        [t[2] for t in lookups],
                        [t[3] for t in lookups],
                    ),
                )
                rows = cur.fetchall()
        finally:
            _put_conn(conn)

        # Aggregate: {(symbol, direction): {outcome: count}}
        agg: dict[tuple[str, str], dict[str, int]] = {}
        for sym, direction, outcome, cnt in rows:
            key = (sym, direction)
            agg.setdefault(key, {})
            agg[key][outcome] = agg[key].get(outcome, 0) + cnt

        result: dict[str, str] = {}
        for a in alerts:
            k = (a.symbol, a.direction)
            stats = agg.get(k, {})
            wins = stats.get("WIN", 0)
            total = sum(stats.values())
            lookup_key = f"{a.symbol}:{a.direction}"
            if total < 2:
                result[lookup_key] = "\U0001f4ca First alert for this setup"
            else:
                pct = int(wins / total * 100)
                result[lookup_key] = f"\U0001f4ca Similar past alerts: {pct}% win rate (N={total})"
        return result
    except Exception:
        return {}


def _route_channel_for_alert(alert: PlaybookAlert) -> str | None:
    """Select Discord channel ID based on alert quality tier.

    Uses EP * confidence as a quality score to route to high/standard/watch
    channels when the corresponding env vars are set.  Returns the channel
    ID instead of mutating ``os.environ`` (thread-safe).

    Args:
        alert: The alert being sent.

    Returns:
        Channel ID string, or ``None`` to use the default channel.
    """
    # Always route WATCH alerts to the watch channel when configured so
    # low-priority context never competes with actionable channels.
    if alert.direction == "WATCH" and _DISCORD_CHANNEL_WATCH:
        return _DISCORD_CHANNEL_WATCH

    quality = alert.edge_probability * alert.confidence
    if quality >= 0.65 and _DISCORD_CHANNEL_HIGH:
        return _DISCORD_CHANNEL_HIGH
    if quality >= 0.45 and _DISCORD_CHANNEL_STANDARD:
        return _DISCORD_CHANNEL_STANDARD
    if _DISCORD_CHANNEL_WATCH:
        return _DISCORD_CHANNEL_WATCH
    return None


def _format_watch_embed(alert: PlaybookAlert) -> dict:
    """Format a compact WATCH embed to minimize distraction.

    WATCH posts are intentionally lightweight context and should not look
    equivalent to actionable LONG/SHORT alerts.
    """
    direction_emoji = _DIRECTION_EMOJI.get("WATCH", "⚪")
    return {
        "embeds": [
            {
                "title": f"{direction_emoji} {alert.symbol} WATCH | Context Only",
                "description": _truncate_field(alert.thesis, max_len=280),
                "color": _COLOR_MAP.get("WATCH", 3447003),
                "fields": [
                    {
                        "name": "Setup",
                        "value": (
                            f"TF: **{alert.timeframe}**\n"
                            f"EP: **{alert.edge_probability:.2f}** | "
                            f"CONF: **{alert.confidence:.2f}** | "
                            f"SA: **{alert.sources_agree}/10**"
                        ),
                        "inline": False,
                    },
                    {
                        "name": "Note",
                        "value": "Non-actionable context. Directional alerts (LONG/SHORT) take precedence.",
                        "inline": False,
                    },
                ],
                "footer": {"text": "trade-alert • WATCH"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }


def format_embed(
    alert: PlaybookAlert,
    *,
    hist_stats: str = "",
    current_price: float | None = None,
    current_price_ts: str | None = None,
) -> dict:
    """Format a PlaybookAlert as a rich Discord embed payload.

    Produces a visually dense, multi-section embed with:
    - Color-coded direction header with emoji and urgency label
    - Visual score bars for edge probability and confidence
    - Full trade playbook with R:R, entry/stop/target
    - Market context section with sentiment, macro, unusual activity
    - Source alignment gauge
    - Footer with timestamp and branding

    Args:
        alert: Validated PlaybookAlert instance.

    Returns:
        Dict matching Discord webhook/bot embed structure per SSOT §11.
    """
    rr = compute_rr(alert.entry)
    unusual = (
        "\n".join(f"  \u2022 {a}" for a in alert.unusual_activity if a)
        if alert.unusual_activity
        else "_None detected_"
    )
    direction_emoji = _DIRECTION_EMOJI.get(alert.direction, "\u26aa")
    edge_label = _edge_label(alert.edge_probability)

    # Risk dollar amounts
    entry_price = alert.entry.get("level", 0)
    stop_price = alert.entry.get("stop", 0)
    target_price = alert.entry.get("target", 0)
    risk_per_share = abs(entry_price - stop_price)
    reward_per_share = abs(target_price - entry_price)

    # Current price context (from latest fetched candle, when available)
    delta_vs_entry: float | None = None
    delta_vs_entry_pct: float | None = None
    if current_price is not None and entry_price > 0:
        delta_vs_entry = current_price - entry_price
        delta_vs_entry_pct = (delta_vs_entry / entry_price) * 100.0

    # Hard freshness gates by alert timeframe: if quote is older than these
    # windows, don't present it as "Current Price" because it can mislead entry context.
    max_price_age_seconds = {
        "5m": int(os.environ.get("ALERT_MAX_PRICE_AGE_5M", str(60 * 60))),
        "15m": int(os.environ.get("ALERT_MAX_PRICE_AGE_15M", str(45 * 60))),
        "1h": int(os.environ.get("ALERT_MAX_PRICE_AGE_1H", str(6 * 60 * 60))),
        "4h": int(os.environ.get("ALERT_MAX_PRICE_AGE_4H", str(24 * 60 * 60))),
        "1D": int(os.environ.get("ALERT_MAX_PRICE_AGE_1D", str(72 * 60 * 60))),
    }
    hard_stale = False
    hard_age_mins: int | None = None

    if current_price_ts:
        try:
            parsed_ts = datetime.fromisoformat(current_price_ts)
            if parsed_ts.tzinfo is None:
                parsed_ts = parsed_ts.replace(tzinfo=timezone.utc)
            ts_utc = parsed_ts.astimezone(timezone.utc)
            age_seconds = max(0.0, (datetime.now(timezone.utc) - ts_utc).total_seconds())
            hard_threshold = max_price_age_seconds.get(alert.timeframe, 2 * 60 * 60)
            if age_seconds > hard_threshold:
                hard_stale = True
                hard_age_mins = int(round(age_seconds / 60.0))
        except ValueError:
            pass

    if hard_stale:
        current_price_field = (
            "_Unavailable (stale market data)_"
            + (f"\nLast quote age: {hard_age_mins}m" if hard_age_mins is not None else "")
        )
    elif current_price is None or delta_vs_entry is None or delta_vs_entry_pct is None:
        current_price_field = "_Unavailable_"
    else:
        if alert.direction == "LONG":
            favorable = delta_vs_entry >= 0
        elif alert.direction == "SHORT":
            favorable = delta_vs_entry <= 0
        else:
            favorable = None

        if favorable is None:
            delta_emoji = "⚪"
        else:
            delta_emoji = "🟢" if favorable else "🔴"

        delta_sign = "+" if delta_vs_entry >= 0 else ""
        pct_sign = "+" if delta_vs_entry_pct >= 0 else ""
        current_price_field = (
            f"**${current_price:,.2f}**\n"
            f"{delta_emoji} {delta_sign}${delta_vs_entry:,.2f} ({pct_sign}{delta_vs_entry_pct:.2f}%) vs entry"
        )
        if current_price_ts:
            try:
                parsed_ts = datetime.fromisoformat(current_price_ts)
                if parsed_ts.tzinfo is None:
                    parsed_ts = parsed_ts.replace(tzinfo=timezone.utc)
                ts_utc = parsed_ts.astimezone(timezone.utc)
                ts_fmt = ts_utc.strftime("%H:%M UTC")

                age_seconds = max(0.0, (datetime.now(timezone.utc) - ts_utc).total_seconds())
                stale_thresholds = {
                    "5m": 15 * 60,
                    "15m": 30 * 60,
                    "1h": 2 * 60 * 60,
                    "4h": 6 * 60 * 60,
                    "1D": 48 * 60 * 60,
                }
                threshold = stale_thresholds.get(alert.timeframe, 30 * 60)
                age_mins = int(round(age_seconds / 60.0))
                stale_suffix = f" (stale {age_mins}m)" if age_seconds > threshold else ""
            except ValueError:
                ts_fmt = current_price_ts
                stale_suffix = ""
            current_price_field += f"\nAs of: {ts_fmt}{stale_suffix}"

    # Historical win-rate context (pre-fetched by caller or per-alert fallback)
    if not hist_stats:
        hist_stats = _get_similar_alert_stats(alert.symbol, alert.direction, alert.edge_probability)

    # Confidence-tier color
    embed_color = _quality_color(alert)

    # Truncate long fields
    thesis = _truncate_field(alert.thesis)
    sentiment_ctx = _truncate_field(alert.sentiment_context)
    macro_ctx = _truncate_field(alert.macro_regime)
    unusual = _truncate_field(unusual)

    fields: list[dict] = [
        {
            "name": "\u2500" * 25,
            "value": "**SIGNAL STRENGTH**",
            "inline": False,
        },
        {
            "name": "\U0001f3af Edge Probability",
            "value": f"```{_score_bar(alert.edge_probability)}```",
            "inline": True,
        },
        {
            "name": "\U0001f4aa Confidence",
            "value": f"```{_score_bar(alert.confidence)}```",
            "inline": True,
        },
        {
            "name": "\u2500" * 25,
            "value": "**TRADE PLAYBOOK**",
            "inline": False,
        },
        {
            "name": "\U0001f4b0 Entry",
            "value": f"```${entry_price:,.2f}```",
            "inline": True,
        },
        {
            "name": "\U0001f6d1 Stop Loss",
            "value": f"```${stop_price:,.2f}```",
            "inline": True,
        },
        {
            "name": "\U0001f3c6 Target",
            "value": f"```${target_price:,.2f}```",
            "inline": True,
        },
        {
            "name": "📍 Current Price",
            "value": current_price_field,
            "inline": True,
        },
        {
            "name": "\u2696\ufe0f Risk / Reward",
            "value": (
                f"**R:R {rr}**\n"
                f"Risk: ${risk_per_share:,.2f}/share  \u2192  "
                f"Reward: ${reward_per_share:,.2f}/share"
            ),
            "inline": False,
        },
        {
            "name": "\u2500" * 25,
            "value": "**MARKET CONTEXT**",
            "inline": False,
        },
        {
            "name": "\u23f0 Timeframe",
            "value": f"**{alert.timeframe}** \u2014 {alert.timeframe_rationale}",
            "inline": False,
        },
        {
            "name": "\U0001f4e3 Sentiment",
            "value": sentiment_ctx,
            "inline": True,
        },
        {
            "name": "\U0001f30d Macro Regime",
            "value": macro_ctx,
            "inline": True,
        },
        {
            "name": "\U0001f50d Unusual Activity",
            "value": unusual,
            "inline": False,
        },
        {
            "name": "\U0001f4ca Source Alignment",
            "value": f"```{_score_bar(alert.sources_agree / 10, segments=10)}```"
            f"**{alert.sources_agree}/10** independent signal families aligned",
            "inline": False,
        },
    ]

    # Append historical stats if available
    if hist_stats:
        fields.append(
            {
                "name": "\U0001f4c8 Track Record",
                "value": hist_stats,
                "inline": False,
            }
        )

    return {
        "embeds": [
            {
                "title": (f"{direction_emoji} {alert.symbol} {alert.direction} | {edge_label}"),
                "description": f"**{thesis}**",
                "color": embed_color,
                "fields": fields,
                "footer": {"text": "trade-alert \u2022 MacroSight LLC"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }


def _is_retryable(exc: httpx.HTTPStatusError) -> bool:
    """Return True for transient HTTP status codes worth retrying."""
    return exc.response.status_code in {429, 500, 502, 503, 504}


def send_discord_embed(
    embed_payload: dict,
    chart_png: bytes | None = None,
    *,
    channel_override: str | None = None,
) -> bool:
    """Send embed to Discord alert channel with retry on transient errors.

    Tries webhook first; falls back to bot API if webhook is not set.
    When *chart_png* is provided the request is sent as multipart/form-data
    so the PNG is uploaded as a Discord file attachment.

    Retries up to ``DISCORD_SEND_MAX_RETRIES`` times with exponential
    backoff (1s, 2s, 4s by default) on 429/5xx responses or network errors.

    Args:
        embed_payload: Dict with ``embeds`` key matching Discord format.
        chart_png: Optional PNG bytes for the candlestick chart image.
        channel_override: Optional Discord channel ID for tiered routing.
            Overrides ``DISCORD_ALERT_CHANNEL_ID`` for this send only.

    Returns:
        ``True`` on success (2xx), ``False`` on failure.
    """
    last_exc: httpx.HTTPStatusError | httpx.RequestError | None = None
    global _discord_consecutive_failures, _discord_cb_open_since  # noqa: PLW0603

    # Circuit breaker: fast-fail if Discord has been consecutively failing.
    # Auto-resets after _DISCORD_CB_RESET_SECS so alerts resume after outages.
    if _discord_consecutive_failures >= _DISCORD_CB_THRESHOLD:
        elapsed = time.monotonic() - _discord_cb_open_since
        if elapsed < _DISCORD_CB_RESET_SECS:
            logger.error(
                "Discord circuit breaker OPEN (%d consecutive failures, %.0fs remaining) — skipping send",
                _discord_consecutive_failures,
                _DISCORD_CB_RESET_SECS - elapsed,
            )
            DISCORD_SENDS.labels(status="circuit_open").inc()
            return False
        # Reset after cooldown period
        logger.info("Discord circuit breaker RESET after %.0fs cooldown", elapsed)
        _discord_consecutive_failures = 0

    for attempt in range(1, DISCORD_SEND_MAX_RETRIES + 1):
        try:
            webhook = _discord_webhook()
            if webhook:
                if chart_png:
                    resp = _get_discord_client().post(
                        webhook,
                        data={"payload_json": json.dumps(embed_payload)},
                        files={"files[0]": ("chart.png", chart_png, "image/png")},
                    )
                else:
                    resp = _get_discord_client().post(webhook, json=embed_payload)
                resp.raise_for_status()
                _discord_consecutive_failures = 0
                DISCORD_SENDS.labels(status="success").inc()
                return True

            bot_token = _discord_bot_token()
            alert_channel = channel_override or _discord_alert_channel_id()
            if bot_token and alert_channel:
                url = f"https://discord.com/api/v10/channels/{alert_channel}/messages"
                headers = {"Authorization": f"Bot {bot_token}"}
                if chart_png:
                    resp = _get_discord_client().post(
                        url,
                        headers=headers,
                        data={"payload_json": json.dumps(embed_payload)},
                        files={"files[0]": ("chart.png", chart_png, "image/png")},
                    )
                else:
                    resp = _get_discord_client().post(
                        url,
                        json=embed_payload,
                        headers=headers,
                    )
                resp.raise_for_status()
                _discord_consecutive_failures = 0
                DISCORD_SENDS.labels(status="success").inc()
                return True

            logger.warning("No Discord credentials configured — skipping send")
            DISCORD_SENDS.labels(status="unconfigured").inc()
            return False

        except httpx.HTTPStatusError as exc:
            last_exc = exc
            if _is_retryable(exc) and attempt < DISCORD_SEND_MAX_RETRIES:
                delay = DISCORD_SEND_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "Discord API %s (attempt %d/%d), retrying in %.1fs",
                    exc.response.status_code,
                    attempt,
                    DISCORD_SEND_MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)
                continue
            _discord_consecutive_failures += 1
            if _discord_consecutive_failures >= _DISCORD_CB_THRESHOLD:
                _discord_cb_open_since = time.monotonic()
            logger.error("Discord API error %s: %s", exc.response.status_code, exc)
            DISCORD_SENDS.labels(status="failure").inc()
            return False

        except httpx.RequestError as exc:
            last_exc = exc
            if attempt < DISCORD_SEND_MAX_RETRIES:
                delay = DISCORD_SEND_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "Discord request failed (attempt %d/%d): %s, retrying in %.1fs",
                    attempt,
                    DISCORD_SEND_MAX_RETRIES,
                    exc,
                    delay,
                )
                time.sleep(delay)
                continue
            _discord_consecutive_failures += 1
            if _discord_consecutive_failures >= _DISCORD_CB_THRESHOLD:
                _discord_cb_open_since = time.monotonic()
            logger.error("Discord request failed after %d attempts: %s", attempt, exc)
            DISCORD_SENDS.labels(status="failure").inc()
            return False

    _discord_consecutive_failures += 1
    if _discord_consecutive_failures >= _DISCORD_CB_THRESHOLD:
        _discord_cb_open_since = time.monotonic()
    logger.error("Discord send exhausted %d retries, last error: %s", DISCORD_SEND_MAX_RETRIES, last_exc)
    DISCORD_SENDS.labels(status="failure").inc()
    return False


def send_ops_message(message: str) -> None:
    """Send a plain text message to the ops/health Discord channel.

    Args:
        message: Plain text body for the ops channel.
    """
    if not _discord_bot_token() or not _discord_ops_channel_id():
        logger.warning("Ops channel not configured — skipping ops message")
        return
    try:
        url = f"https://discord.com/api/v10/channels/{_discord_ops_channel_id()}/messages"
        headers = {"Authorization": f"Bot {_discord_bot_token()}"}
        resp = _get_discord_client().post(
            url,
            json={"content": message},
            headers=headers,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error("Ops message API error %s: %s", exc.response.status_code, exc)
    except httpx.RequestError as exc:
        logger.error("Ops message request failed: %s", exc)


def send_ops_embed(embed_payload: dict) -> bool:
    """Send a rich embed to the ops/health Discord channel.

    Args:
        embed_payload: Dict with ``embeds`` key matching Discord format.

    Returns:
        ``True`` on success (2xx), ``False`` on failure.
    """
    bot_token = _discord_bot_token()
    ops_channel = _discord_ops_channel_id()
    if not bot_token or not ops_channel:
        logger.warning("Ops channel not configured — skipping ops embed")
        return False
    try:
        url = f"https://discord.com/api/v10/channels/{ops_channel}/messages"
        headers = {"Authorization": f"Bot {bot_token}"}
        resp = _get_discord_client().post(url, json=embed_payload, headers=headers)
        resp.raise_for_status()
        return True
    except httpx.HTTPStatusError as exc:
        logger.error("Ops embed API error %s: %s", exc.response.status_code, exc)
        return False
    except httpx.RequestError as exc:
        logger.error("Ops embed request failed: %s", exc)
        return False


def _thesis_similarity(a: str, b: str) -> float:
    """Jaccard similarity of thesis word sets.

    Args:
        a: First thesis string.
        b: Second thesis string.

    Returns:
        Similarity score from 0.0 (completely different) to 1.0 (identical).
    """
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def _is_duplicate_alert(
    symbol: str,
    direction: str,
    timeframe: str,
    thesis: str = "",
) -> bool:
    """Check Redis for a recent alert with the same symbol/direction/timeframe.

    Sets a key with TTL on first fire, returns True if key already exists.
    Prevents duplicate alerts within the dedup window.

    When a dedup hit is found, compares the new thesis against the stored
    thesis using Jaccard similarity. If the theses are materially different
    (similarity < 0.5), the alert is allowed through and the stored thesis
    is updated.

    Args:
        symbol: Ticker symbol.
        direction: LONG/SHORT/WATCH.
        timeframe: Pipeline timeframe.
        thesis: Alert thesis for content-aware dedup.

    Returns:
        True if a duplicate was found (alert should be suppressed).
    """
    # Include a short thesis hash in the dedup key so that the same symbol
    # can re-alert within the window IF the thesis is materially different.
    thesis_hash = hashlib.md5(thesis[:120].encode()).hexdigest()[:8] if thesis else "no_thesis"
    dedup_key = f"alert:dedup:{symbol}:{direction}:{timeframe}:{thesis_hash}"
    thesis_key = f"alert:thesis:{symbol}:{direction}:{timeframe}"
    try:
        r = get_redis()
        # Atomic SET NX+EX eliminates the TOCTOU race between EXISTS and
        # SETEX — a concurrent pipeline tick can no longer slip between
        # the read and the write, which previously allowed duplicate
        # Discord sends for the same signal.
        was_set = r.set(dedup_key, "1", nx=True, ex=DEDUP_WINDOW_SECONDS)
        if was_set:
            # First time seeing this key — store thesis atomically
            if thesis:
                r.set(thesis_key, thesis, ex=DEDUP_WINDOW_SECONDS)
            return False

        # Key already existed — check for content-aware override.
        # Jaccard threshold 0.5: allows re-alerting when less than half the
        # thesis words overlap, indicating a materially different thesis.
        if thesis:
            stored_thesis = r.get(thesis_key) or ""
            if stored_thesis and _thesis_similarity(thesis, stored_thesis) < 0.5:
                logger.info(
                    "Dedup: allowing new thesis for %s %s %s (different content)",
                    symbol,
                    direction,
                    timeframe,
                )
                # Update both keys atomically with a pipeline
                pipe = r.pipeline()
                pipe.set(dedup_key, "1", ex=DEDUP_WINDOW_SECONDS)
                pipe.set(thesis_key, thesis, ex=DEDUP_WINDOW_SECONDS)
                pipe.execute()
                return False
        logger.info("Dedup: suppressing duplicate alert %s %s %s", symbol, direction, timeframe)
        return True
    except redis.RedisError as exc:
        logger.warning("Dedup check failed (allowing alert through): %s", exc)
        return False


def notify(
    alerts_json: str,
    raw_snapshots: list[dict] | None = None,
    trace_id: str | None = None,
) -> int:
    """Main entry point called by decision workflows.

    Args:
        alerts_json: JSON string of PlaybookAlert dicts from the decision engine.
        raw_snapshots: Optional raw snapshot dicts for audit logging.
        trace_id: Langfuse trace ID for this pipeline run (for DB linkage).

    Returns:
        Count of alerts successfully sent to Discord.
    """
    snapshots = raw_snapshots or []

    # Extract per-symbol forecast scores from raw snapshots for DB logging
    _forecast_scores: dict[str, float] = {}
    for snap in snapshots:
        sym = snap.get("symbol", "")
        for sig in snap.get("signals", []):
            if sig.get("type") == "price_forecast":
                try:
                    val = float(sig.get("score", 0))
                    if sym not in _forecast_scores or abs(val) > abs(_forecast_scores[sym]):
                        _forecast_scores[sym] = val
                except (TypeError, ValueError):
                    pass

    n_sent = 0

    try:
        items = json.loads(alerts_json)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.error("Notifier JSON parse error: %s", exc)
        return 0

    if not isinstance(items, list):
        logger.error("Notifier expected list, got %s", type(items).__name__)
        return 0

    valid_alerts: list[PlaybookAlert] = []
    for item in items:
        try:
            if not isinstance(item, dict):
                logger.warning("Notifier: skipping non-dict item %s", type(item).__name__)
                continue
            alert = PlaybookAlert(**item)
            # Dedup: suppress duplicate alerts within the window
            if _is_duplicate_alert(alert.symbol, alert.direction, alert.timeframe, alert.thesis):
                continue
            valid_alerts.append(alert)
        except (ValidationError, redis.RedisError, KeyError, TypeError) as exc:
            logger.error("Notifier alert processing failed: %s", exc)

    # Protect directional delivery: cap LONG/SHORT independently so WATCH
    # can never consume actionable alert capacity.
    directional_alerts = [a for a in valid_alerts if a.direction in ("LONG", "SHORT")]
    watch_alerts = [a for a in valid_alerts if a.direction == "WATCH"]

    if len(directional_alerts) > MAX_ALERTS_PER_CYCLE:
        directional_alerts.sort(
            key=lambda a: a.edge_probability * a.confidence,
            reverse=True,
        )
        dropped = len(directional_alerts) - MAX_ALERTS_PER_CYCLE
        directional_alerts = directional_alerts[:MAX_ALERTS_PER_CYCLE]
        logger.warning(
            "Capped directional alerts: dropped %d of %d (kept top %d by EP*conf)",
            dropped,
            dropped + MAX_ALERTS_PER_CYCLE,
            MAX_ALERTS_PER_CYCLE,
        )

    # Keep WATCH strictly minimal in output volume too.
    if len(watch_alerts) > 1:
        watch_alerts.sort(
            key=lambda a: a.edge_probability * a.confidence,
            reverse=True,
        )
        dropped_watch = len(watch_alerts) - 1
        watch_alerts = watch_alerts[:1]
        logger.info("Capped WATCH alerts: dropped %d (kept top 1)", dropped_watch)

    valid_alerts = directional_alerts + watch_alerts

    # Pre-generate candlestick charts in parallel (I/O-bound: Polygon API + mplfinance render)
    # Also batch-fetch historical win-rate stats (single DB query instead of N+1)
    batch_stats = _batch_similar_alert_stats(valid_alerts)
    chart_map: dict[str, bytes | None] = {}
    atr_map: dict[str, float | None] = {}
    current_price_map: dict[str, float | None] = {}
    current_price_ts_map: dict[str, str | None] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        future_to_sym = {
            pool.submit(generate_chart, alert.symbol, alert.timeframe, alert.entry): alert.symbol
            for alert in valid_alerts
            if alert.direction != "WATCH"
        }
        for future in concurrent.futures.as_completed(future_to_sym):
            sym = future_to_sym[future]
            try:
                chart_bytes, atr_val, current_price, current_price_ts = future.result()
                chart_map[sym] = chart_bytes
                atr_map[sym] = atr_val
                current_price_map[sym] = current_price
                current_price_ts_map[sym] = current_price_ts
            except Exception as exc:
                logger.warning("Chart generation failed for %s: %s", sym, exc)
                chart_map[sym] = None
                atr_map[sym] = None
                current_price_map[sym] = None
                current_price_ts_map[sym] = None

    for alert in valid_alerts:
        try:
            if alert.direction == "WATCH":
                embed = _format_watch_embed(alert)
            else:
                embed = format_embed(
                    alert,
                    hist_stats=batch_stats.get(f"{alert.symbol}:{alert.direction}", ""),
                    current_price=current_price_map.get(alert.symbol),
                    current_price_ts=current_price_ts_map.get(alert.symbol),
                )
            chart_png = chart_map.get(alert.symbol)
            atr_val = atr_map.get(alert.symbol)
            if chart_png and alert.direction != "WATCH":
                embed["embeds"][0]["image"] = {"url": "attachment://chart.png"}
            # Add ATR-based position sizing hint
            if atr_val and atr_val > 0 and alert.direction != "WATCH":
                embed["embeds"][0]["fields"].append(
                    {
                        "name": "\U0001f4b0 ATR Risk Guide",
                        "value": (
                            f"14-period ATR: **${atr_val:,.2f}**\n"
                            f"1 ATR stop: ${atr_val * 1.5:,.2f} risk/share"
                        ),
                        "inline": True,
                    }
                )

            # Persist-first ordering: INSERT into Postgres BEFORE sending
            # to Discord.  This prevents "phantom alerts" where a Discord
            # message is delivered but the alert is never recorded (e.g. a
            # crash between Discord send and Postgres write).  If Postgres
            # fails we skip Discord entirely — no user-visible alert without
            # a persisted record.  If Discord fails after a successful
            # insert the alert is safely persisted; Discord failure is
            # non-fatal and will be retried or noticed via ops monitoring.
            try:
                _fc = _forecast_scores.get(alert.symbol)
                insert_alert(
                    alert,
                    snapshots,
                    forecast_score=_fc,
                    forecast_contradicted=False,
                    trace_id=trace_id or None,
                )
                DB_INSERTS.labels(status="success").inc()
            except Exception as exc:
                logger.error(
                    "Postgres insert failed for %s — skipping Discord send: %s",
                    alert.symbol,
                    exc,
                )
                DB_INSERTS.labels(status="failure").inc()
                continue

            # Outbound execution webhook (config-gated; non-fatal to Discord delivery)
            if TRADE_EXECUTE_ENABLED:
                try:
                    _trigger = map_to_execution_trigger(alert)
                    deliver_execution_trigger(_trigger)
                except Exception as _exc:  # noqa: BLE001
                    logger.error(
                        "Execution webhook failed for %s — continuing to Discord: %s",
                        alert.symbol,
                        _exc,
                    )

            # Tiered channel routing: select channel based on quality (thread-safe)
            routed_channel = _route_channel_for_alert(alert)

            sent = send_discord_embed(embed, chart_png=chart_png, channel_override=routed_channel)
            if sent:
                n_sent += 1
            else:
                logger.warning(
                    "Discord send failed for %s after successful Postgres insert",
                    alert.symbol,
                )
        except (httpx.HTTPError, redis.RedisError, KeyError, TypeError, ValueError) as exc:
            logger.error("Notifier alert send failed: %s", exc)

    logger.info("Notifier: sent %d/%d alerts to Discord", n_sent, len(items))
    return n_sent


if __name__ == "__main__":
    # Dry-run test — format only, no real Discord send
    sample_alert = PlaybookAlert(
        symbol="NVDA",
        direction="LONG",
        edge_probability=0.82,
        confidence=0.85,
        timeframe="15m",
        thesis="Bollinger Band squeeze breaking out with 3x volume and "
        "strong retail sentiment. Institutional order flow confirms.",
        entry={"level": 875.0, "stop": 865.0, "target": 900.0},
        timeframe_rationale="15m breakout aligning with 1h uptrend structure.",
        sentiment_context="ROT strong_bullish, Finnhub +0.6 aggregate score.",
        unusual_activity=["IV spike 2.1x avg", "options sweep $900c 0DTE"],
        macro_regime="Risk-on. VIX 13.2, yield curve +18bps.",
        sources_agree=5,
    )

    embed = format_embed(sample_alert)
    rr = compute_rr(sample_alert.entry)

    print("=== DISCORD EMBED (dry-run) ===")
    print(json.dumps(embed, indent=2))
    print(f"\nR:R computed: {rr}")
    print(f"Title: {embed['embeds'][0]['title']}")
    print("\nNotifier dry-run \u2705")
