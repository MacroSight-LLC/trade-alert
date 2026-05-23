"""Discord HTTP delivery for trade-alert (webhook and bot API)."""

from __future__ import annotations

import json
import logging
import os
import random
import time
from atexit import register as _atexit_register
from collections.abc import Callable

import httpx

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from log_config import configure_logging
from metrics import DISCORD_SENDS

configure_logging()
logger = logging.getLogger(__name__)

DISCORD_HTTP_TIMEOUT: float = float(os.getenv("DISCORD_HTTP_TIMEOUT", "10.0"))
DISCORD_SEND_MAX_RETRIES: int = int(os.getenv("DISCORD_SEND_MAX_RETRIES", "3"))
DISCORD_SEND_BACKOFF_BASE: float = float(os.getenv("DISCORD_SEND_BACKOFF_BASE", "1.0"))

_discord_client: httpx.Client | None = None

_DISCORD_CB_THRESHOLD: int = int(os.getenv("DISCORD_CB_THRESHOLD", "2"))
_DISCORD_CB_RESET_SECS: float = float(os.getenv("DISCORD_CB_RESET_SECS", "120.0"))
_discord_consecutive_failures: int = 0
_discord_cb_open_since: float = 0.0


def _sleep(seconds: float) -> None:
    """Sleep hook for backoff; patch ``notifier.time.sleep`` in tests."""
    time.sleep(seconds)


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


def _is_retryable(exc: httpx.HTTPStatusError) -> bool:
    """Return True for transient HTTP status codes worth retrying."""
    return exc.response.status_code in {429, 500, 502, 503, 504}


def _backoff_seconds(attempt: int, status_code: int | None) -> float:
    """Exponential backoff; 429 responses add jitter to reduce thundering herd."""
    delay = DISCORD_SEND_BACKOFF_BASE * (2 ** (attempt - 1))
    if status_code == 429:
        return float(delay + random.uniform(0, 1))
    return float(delay)


def _send_with_backoff(
    send_once: Callable[[], bool],
    *,
    on_http_error: Callable[[httpx.HTTPStatusError], bool] | None = None,
) -> bool:
    """Run *send_once* with retries on transient Discord failures.

    Args:
        send_once: Callable that performs one HTTP attempt; returns True on 2xx.
        on_http_error: Optional hook returning True to retry after HTTPStatusError.

    Returns:
        True if any attempt succeeded.
    """
    global _discord_consecutive_failures, _discord_cb_open_since  # noqa: PLW0603

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
        logger.info("Discord circuit breaker RESET after %.0fs cooldown", elapsed)
        _discord_consecutive_failures = 0

    last_exc: httpx.HTTPStatusError | httpx.RequestError | None = None
    for attempt in range(1, DISCORD_SEND_MAX_RETRIES + 1):
        try:
            if send_once():
                _discord_consecutive_failures = 0
                DISCORD_SENDS.labels(status="success").inc()
                return True
            DISCORD_SENDS.labels(status="unconfigured").inc()
            return False

        except httpx.HTTPStatusError as exc:
            last_exc = exc
            should_retry = on_http_error(exc) if on_http_error else _is_retryable(exc)
            if should_retry and attempt < DISCORD_SEND_MAX_RETRIES:
                delay = _backoff_seconds(attempt, exc.response.status_code)
                logger.warning(
                    "Discord API %s (attempt %d/%d), retrying in %.1fs",
                    exc.response.status_code,
                    attempt,
                    DISCORD_SEND_MAX_RETRIES,
                    delay,
                )
                _sleep(delay)
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
                delay = _backoff_seconds(attempt, None)
                logger.warning(
                    "Discord request failed (attempt %d/%d): %s, retrying in %.1fs",
                    attempt,
                    DISCORD_SEND_MAX_RETRIES,
                    exc,
                    delay,
                )
                _sleep(delay)
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
    logger.error(
        "Discord send exhausted %d retries, last error: %s",
        DISCORD_SEND_MAX_RETRIES,
        last_exc,
    )
    DISCORD_SENDS.labels(status="failure").inc()
    return False


def _post_alert_payload(
    embed_payload: dict,
    chart_png: bytes | None,
    channel_override: str | None,
) -> bool:
    """Single HTTP attempt for alert-channel delivery."""
    webhook = _discord_webhook()
    client = _get_discord_client()
    if webhook:
        if chart_png:
            resp = client.post(
                webhook,
                data={"payload_json": json.dumps(embed_payload)},
                files={"files[0]": ("chart.png", chart_png, "image/png")},
            )
        else:
            resp = client.post(webhook, json=embed_payload)
        resp.raise_for_status()
        return True

    bot_token = _discord_bot_token()
    alert_channel = channel_override or _discord_alert_channel_id()
    if bot_token and alert_channel:
        url = f"https://discord.com/api/v10/channels/{alert_channel}/messages"
        headers = {"Authorization": f"Bot {bot_token}"}
        if chart_png:
            resp = client.post(
                url,
                headers=headers,
                data={"payload_json": json.dumps(embed_payload)},
                files={"files[0]": ("chart.png", chart_png, "image/png")},
            )
        else:
            resp = client.post(url, json=embed_payload, headers=headers)
        resp.raise_for_status()
        return True

    logger.warning("No Discord credentials configured — skipping send")
    return False


def send_discord_embed(
    embed_payload: dict,
    chart_png: bytes | None = None,
    *,
    channel_override: str | None = None,
) -> bool:
    """Send embed to Discord alert channel with retry on transient errors."""
    return _send_with_backoff(
        lambda: _post_alert_payload(embed_payload, chart_png, channel_override),
        on_http_error=_is_retryable,
    )


def send_ops_message(message: str) -> None:
    """Send a plain text message to the ops/health Discord channel."""
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
    """Send a rich embed to the ops/health Discord channel."""
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
