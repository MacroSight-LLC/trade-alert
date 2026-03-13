"""Discord notifier and Postgres logger for trade-alert.

Receives validated PlaybookAlert JSON from the decision engine,
formats rich Discord embeds, sends via webhook or bot API,
and logs each alert to Postgres.
Implements SSOT §11.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

import httpx
import redis as _redis

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from chart_gen import generate_chart
from db import insert_alert
from models import PlaybookAlert

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DISCORD_HTTP_TIMEOUT: float = float(os.getenv("DISCORD_HTTP_TIMEOUT", "10.0"))
DEDUP_WINDOW_SECONDS: int = int(os.getenv("DEDUP_WINDOW_SECONDS", "900"))  # 15 min default
MAX_ALERTS_PER_CYCLE: int = int(os.getenv("MAX_ALERTS_PER_CYCLE", "5"))

_discord_client: httpx.Client | None = None


def _get_discord_client() -> httpx.Client:
    """Return a module-level HTTP client for Discord API calls."""
    global _discord_client  # noqa: PLW0603
    if _discord_client is None or _discord_client.is_closed:
        _discord_client = httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=DISCORD_HTTP_TIMEOUT, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
        )
    return _discord_client


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


def format_embed(alert: PlaybookAlert) -> dict:
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
        "\n".join(f"  \u2022 {a}" for a in alert.unusual_activity)
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

    return {
        "embeds": [
            {
                "title": (f"{direction_emoji} {alert.symbol} {alert.direction} | {edge_label}"),
                "description": f"**{alert.thesis}**",
                "color": _COLOR_MAP.get(alert.direction, 3447003),
                "fields": [
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
                        "value": alert.sentiment_context,
                        "inline": True,
                    },
                    {
                        "name": "\U0001f30d Macro Regime",
                        "value": alert.macro_regime,
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
                        f"**{alert.sources_agree}/10** independent sources aligned",
                        "inline": False,
                    },
                ],
                "footer": {"text": "trade-alert \u2022 MacroSight LLC"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }


def send_discord_embed(
    embed_payload: dict,
    chart_png: bytes | None = None,
) -> bool:
    """Send embed to Discord alert channel.

    Tries webhook first; falls back to bot API if webhook is not set.
    When *chart_png* is provided the request is sent as multipart/form-data
    so the PNG is uploaded as a Discord file attachment.

    Args:
        embed_payload: Dict with ``embeds`` key matching Discord format.
        chart_png: Optional PNG bytes for the candlestick chart image.

    Returns:
        ``True`` on success (2xx), ``False`` on failure.
    """
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
            return True

        bot_token = _discord_bot_token()
        alert_channel = _discord_alert_channel_id()
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
            return True

        logger.warning("No Discord credentials configured — skipping send")
        return False
    except httpx.HTTPStatusError as exc:
        logger.error("Discord API error %s: %s", exc.response.status_code, exc)
        return False
    except httpx.RequestError as exc:
        logger.error("Discord request failed: %s", exc)
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
        r = _redis.from_url(
            os.getenv("REDIS_URL", "redis://redis:6379"),
            decode_responses=True,
            socket_timeout=5.0,
        )
        # Atomic SET NX+EX eliminates the TOCTOU race between EXISTS and
        # SETEX — a concurrent pipeline tick can no longer slip between
        # the read and the write, which previously allowed duplicate
        # Discord sends for the same signal.
        was_set = r.set(dedup_key, "1", nx=True, ex=DEDUP_WINDOW_SECONDS)
        if was_set:
            # First time seeing this key — store thesis for similarity checks
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
                r.set(dedup_key, "1", ex=DEDUP_WINDOW_SECONDS)
                r.set(thesis_key, thesis, ex=DEDUP_WINDOW_SECONDS)
                return False
        logger.info("Dedup: suppressing duplicate alert %s %s %s", symbol, direction, timeframe)
        return True
    except _redis.RedisError as exc:
        logger.warning("Dedup check failed (allowing alert through): %s", exc)
        return False


def notify(alerts_json: str, raw_snapshots: list[dict] | None = None) -> int:
    """Main entry point called by decision workflows.

    Args:
        alerts_json: JSON string of PlaybookAlert dicts from the decision engine.
        raw_snapshots: Optional raw snapshot dicts for audit logging.

    Returns:
        Count of alerts successfully sent to Discord.
    """
    snapshots = raw_snapshots or []
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
        except Exception as exc:
            logger.error("Notifier alert processing failed: %s", exc)

    # Cap alerts per cycle to prevent Discord spam (sort by quality)
    if len(valid_alerts) > MAX_ALERTS_PER_CYCLE:
        valid_alerts.sort(
            key=lambda a: a.edge_probability * a.confidence,
            reverse=True,
        )
        dropped = len(valid_alerts) - MAX_ALERTS_PER_CYCLE
        valid_alerts = valid_alerts[:MAX_ALERTS_PER_CYCLE]
        logger.warning(
            "Capped alerts: dropped %d of %d (kept top %d by EP*conf)",
            dropped,
            dropped + MAX_ALERTS_PER_CYCLE,
            MAX_ALERTS_PER_CYCLE,
        )

    for alert in valid_alerts:
        try:
            embed = format_embed(alert)
            chart_png = generate_chart(alert.symbol, alert.timeframe, alert.entry)
            if chart_png:
                embed["embeds"][0]["image"] = {"url": "attachment://chart.png"}

            # Persist-first ordering: INSERT into Postgres BEFORE sending
            # to Discord.  This prevents "phantom alerts" where a Discord
            # message is delivered but the alert is never recorded (e.g. a
            # crash between Discord send and Postgres write).  If Postgres
            # fails we skip Discord entirely — no user-visible alert without
            # a persisted record.  If Discord fails after a successful
            # insert the alert is safely persisted; Discord failure is
            # non-fatal and will be retried or noticed via ops monitoring.
            try:
                insert_alert(alert, snapshots)
            except Exception as exc:
                logger.error(
                    "Postgres insert failed for %s — skipping Discord send: %s",
                    alert.symbol,
                    exc,
                )
                continue

            sent = send_discord_embed(embed, chart_png=chart_png)
            if sent:
                n_sent += 1
            else:
                logger.warning(
                    "Discord send failed for %s after successful Postgres insert",
                    alert.symbol,
                )
        except Exception as exc:
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
