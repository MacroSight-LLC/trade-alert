"""Discord notifier and Postgres logger for trade-alert.

Receives validated PlaybookAlert JSON from the decision engine,
formats rich Discord embeds, sends via webhook or bot API,
and logs each alert to Postgres.
Implements SSOT §11.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import httpx

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from db import insert_alert
from models import PlaybookAlert

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DISCORD_HTTP_TIMEOUT: float = float(os.getenv("DISCORD_HTTP_TIMEOUT", "10.0"))


def _discord_webhook() -> str | None:
    return os.getenv("DISCORD_WEBHOOK")


def _discord_bot_token() -> str | None:
    return os.getenv("DISCORD_BOT_TOKEN")


def _discord_alert_channel_id() -> str | None:
    return os.getenv("DISCORD_ALERT_CHANNEL_ID")


def _discord_ops_channel_id() -> str | None:
    return os.getenv("DISCORD_OPS_CHANNEL_ID")


_COLOR_MAP: dict[str, int] = {
    "LONG": 3066993,
    "SHORT": 15158332,
    "WATCH": 3447003,
}


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


def format_embed(alert: PlaybookAlert) -> dict:
    """Format a PlaybookAlert as a Discord embed payload.

    Args:
        alert: Validated PlaybookAlert instance.

    Returns:
        Dict matching Discord webhook/bot embed structure per SSOT §11.
    """
    ep_pct = f"{alert.edge_probability * 100:.1f}%"
    conf_pct = f"{alert.confidence * 100:.1f}%"
    rr = compute_rr(alert.entry)
    unusual = " \u00b7 ".join(alert.unusual_activity) if alert.unusual_activity else "None"

    return {
        "embeds": [
            {
                "title": (f"\U0001f6a8 {alert.symbol} {alert.direction} | Edge: {ep_pct} | Conf: {conf_pct}"),
                "color": _COLOR_MAP.get(alert.direction, 3447003),
                "fields": [
                    {
                        "name": "\U0001f3af Trade Playbook",
                        "value": (
                            f"**Thesis:** {alert.thesis}\n"
                            f"**Entry:** ${alert.entry['level']}"
                            f" | **Stop:** ${alert.entry['stop']}"
                            f" | **Target:** ${alert.entry['target']}"
                            f" (R:R {rr})"
                        ),
                        "inline": False,
                    },
                    {
                        "name": "\U0001f4ca Context",
                        "value": (
                            f"**Timeframe:** {alert.timeframe}"
                            f" \u2013 {alert.timeframe_rationale}\n"
                            f"**Sentiment:** {alert.sentiment_context}\n"
                            f"**Unusual:** {unusual}\n"
                            f"**Macro:** {alert.macro_regime}\n"
                            f"**Sources:** {alert.sources_agree}/10 aligned"
                        ),
                        "inline": False,
                    },
                ],
                "footer": {"text": "trade-alert | MacroSight LLC"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }


def send_discord_embed(embed_payload: dict) -> bool:
    """Send embed to Discord alert channel.

    Tries webhook first; falls back to bot API if webhook is not set.

    Args:
        embed_payload: Dict with ``embeds`` key matching Discord format.

    Returns:
        ``True`` on success (2xx), ``False`` on failure.
    """
    try:
        webhook = _discord_webhook()
        if webhook:
            resp = httpx.post(webhook, json=embed_payload, timeout=DISCORD_HTTP_TIMEOUT)
            resp.raise_for_status()
            return True

        bot_token = _discord_bot_token()
        alert_channel = _discord_alert_channel_id()
        if bot_token and alert_channel:
            url = f"https://discord.com/api/v10/channels/{alert_channel}/messages"
            headers = {"Authorization": f"Bot {bot_token}"}
            resp = httpx.post(
                url,
                json=embed_payload,
                headers=headers,
                timeout=DISCORD_HTTP_TIMEOUT,
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
        resp = httpx.post(
            url,
            json={"content": message},
            headers=headers,
            timeout=DISCORD_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error("Ops message API error %s: %s", exc.response.status_code, exc)
    except httpx.RequestError as exc:
        logger.error("Ops message request failed: %s", exc)


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

    for item in items:
        try:
            if not isinstance(item, dict):
                logger.warning("Notifier: skipping non-dict item %s", type(item).__name__)
                continue
            alert = PlaybookAlert(**item)
            embed = format_embed(alert)
            sent = send_discord_embed(embed)
            if sent:
                n_sent += 1
            try:
                insert_alert(alert, snapshots)
            except Exception as exc:
                logger.error("Postgres insert failed for %s: %s", alert.symbol, exc)
        except Exception as exc:
            logger.error("Notifier alert processing failed: %s", exc)

    logger.info("Notifier: sent %d/%d alerts to Discord", n_sent, len(items))
    return n_sent


if __name__ == "__main__":
    # Dry-run test — format only, no real Discord send
    mock_alert = PlaybookAlert(
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

    embed = format_embed(mock_alert)
    rr = compute_rr(mock_alert.entry)

    print("=== MOCK DISCORD EMBED ===")
    print(json.dumps(embed, indent=2))
    print(f"\nR:R computed: {rr}")
    print(f"Title: {embed['embeds'][0]['title']}")
    print("\nNotifier dry-run \u2705")
