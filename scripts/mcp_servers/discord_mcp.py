"""Discord MCP — real API integration.

Tools: send_rich_embed
Requires: DISCORD_WEBHOOK or (DISCORD_BOT_TOKEN + DISCORD_ALERT_CHANNEL_ID).
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERVICE_NAME = "Discord MCP"

WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK", "")
BOT_TOKEN: str = os.getenv("DISCORD_BOT_TOKEN", "")
ALERT_CHANNEL_ID: str = os.getenv("DISCORD_ALERT_CHANNEL_ID", "")
OPS_CHANNEL_ID: str = os.getenv("DISCORD_OPS_CHANNEL_ID", "")
TIMEOUT = 10.0


async def send_rich_embed(params: dict[str, Any]) -> dict:
    """Send a rich embed to Discord.

    Params:
        embeds: list[dict] — Discord embed objects.
        channel_id: str — optional override (defaults to alert channel).
        content: str — optional text content.

    Returns:
        {"status": "sent", "message_id": str}
    """
    embeds = params.get("embeds", [])
    content = params.get("content", "")
    channel_id = params.get("channel_id", ALERT_CHANNEL_ID)

    payload: dict[str, Any] = {}
    if embeds:
        payload["embeds"] = embeds
    if content:
        payload["content"] = content

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        # Prefer webhook if set (simpler, no bot needed)
        if WEBHOOK_URL:
            resp = await client.post(WEBHOOK_URL, json=payload)
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            return {"status": "sent", "message_id": data.get("id", "webhook")}

        # Fall back to bot API
        if BOT_TOKEN and channel_id:
            resp = await client.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                json=payload,
                headers={"Authorization": f"Bot {BOT_TOKEN}"},
            )
            resp.raise_for_status()
            data = resp.json()
            return {"status": "sent", "message_id": data.get("id", "bot")}

    logger.warning("No Discord credentials configured — embed not sent")
    return {"status": "skipped", "message_id": "", "reason": "no credentials"}


TOOLS: dict[str, Any] = {
    "send_rich_embed": send_rich_embed,
}
