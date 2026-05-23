"""Discord embed formatting for trade-alert alerts (SSOT §11)."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from alert_logger import get_similar_alert_stats
from log_config import configure_logging
from models import PlaybookAlert

configure_logging()
logger = logging.getLogger(__name__)

MAX_EMBED_FIELDS: int = 25
MAX_EMBED_DESCRIPTION_CHARS: int = 4096

_MAX_FIELD_LEN: int = 1000

# Tiered channel routing by alert quality
_DISCORD_CHANNEL_HIGH: str | None = os.getenv("DISCORD_CHANNEL_HIGH")
_DISCORD_CHANNEL_STANDARD: str | None = os.getenv("DISCORD_CHANNEL_STANDARD")
_DISCORD_CHANNEL_WATCH: str | None = os.getenv("DISCORD_CHANNEL_WATCH")


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
    payload = {
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
                "timestamp": datetime.now(UTC).isoformat(),
            }
        ]
    }
    return _enforce_embed_limits(payload)


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
                parsed_ts = parsed_ts.replace(tzinfo=UTC)
            ts_utc = parsed_ts.astimezone(UTC)
            age_seconds = max(0.0, (datetime.now(UTC) - ts_utc).total_seconds())
            hard_threshold = max_price_age_seconds.get(alert.timeframe, 2 * 60 * 60)
            if age_seconds > hard_threshold:
                hard_stale = True
                hard_age_mins = int(round(age_seconds / 60.0))
        except ValueError:
            pass

    if hard_stale:
        current_price_field = "_Unavailable (stale market data)_" + (
            f"\nLast quote age: {hard_age_mins}m" if hard_age_mins is not None else ""
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
                    parsed_ts = parsed_ts.replace(tzinfo=UTC)
                ts_utc = parsed_ts.astimezone(UTC)
                ts_fmt = ts_utc.strftime("%H:%M UTC")

                age_seconds = max(0.0, (datetime.now(UTC) - ts_utc).total_seconds())
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
        hist_stats = get_similar_alert_stats(alert.symbol, alert.direction, alert.edge_probability)

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

    payload = {
        "embeds": [
            {
                "title": (f"{direction_emoji} {alert.symbol} {alert.direction} | {edge_label}"),
                "description": f"**{thesis}**",
                "color": embed_color,
                "fields": fields,
                "footer": {"text": "trade-alert \u2022 MacroSight LLC"},
                "timestamp": datetime.now(UTC).isoformat(),
            }
        ]
    }
    return _enforce_embed_limits(payload)


def _enforce_embed_limits(payload: dict) -> dict:
    """Truncate embed description and fields to Discord API limits."""
    for embed in payload.get("embeds", []):
        desc = embed.get("description", "")
        if len(desc) > MAX_EMBED_DESCRIPTION_CHARS:
            logger.warning(
                "Truncating embed description from %d to %d chars",
                len(desc),
                MAX_EMBED_DESCRIPTION_CHARS,
            )
            embed["description"] = desc[: MAX_EMBED_DESCRIPTION_CHARS - 3] + "..."
        fields = embed.get("fields", [])
        if len(fields) > MAX_EMBED_FIELDS:
            logger.warning(
                "Truncating embed fields from %d to %d",
                len(fields),
                MAX_EMBED_FIELDS,
            )
            embed["fields"] = fields[:MAX_EMBED_FIELDS]
    return payload
