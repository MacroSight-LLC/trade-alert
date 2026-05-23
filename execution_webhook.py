"""Outbound webhook delivery client for trade-execute integration.

Sends ExecutionPayload / ExecutionTriggerV1 to trade-execute via HTTP POST with
HMAC-SHA256 request signing, configurable retry/backoff, dry-run support,
and Postgres audit logging.

Signing format:
    X-TradeAlert-Timestamp: <unix epoch seconds>
    X-TradeAlert-Signature: sha256=HMAC-SHA256(secret, "{timestamp}.{body}")

See README.md §Downstream Execution Integration.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

import httpx

from constants import (
    EXECUTION_WEBHOOK_MAX_RETRIES,
    TRADE_EXECUTE_DRY_RUN,
    TRADE_EXECUTE_ENABLED,
    TRADE_EXECUTE_RETRY_BACKOFF_SECONDS,
    TRADE_EXECUTE_TIMEOUT_SECONDS,
    TRADE_EXECUTE_WEBHOOK_URL,
)
from execution_mapper import ExecutionPayload
from execution_trigger import ExecutionTriggerV1
from metrics import GATE_REJECTIONS

logger = logging.getLogger(__name__)

# ── HTTP client (module-level singleton, lazy init) ────────────────────────

_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    """Return a lazily-initialised module-level httpx.Client."""
    global _client  # noqa: PLW0603
    if _client is None or _client.is_closed:
        _client = httpx.Client(
            timeout=httpx.Timeout(
                connect=5.0,
                read=TRADE_EXECUTE_TIMEOUT_SECONDS,
                write=5.0,
                pool=5.0,
            ),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )
    return _client


# ── Signing ────────────────────────────────────────────────────────────────


def sign_payload(body: bytes, timestamp: str, secret: str) -> str:
    """Compute HMAC-SHA256 signature for a webhook payload.

    Signs the concatenation of ``"{timestamp}.{body}"`` using the shared
    secret.  The returned string is prefixed with ``"sha256="`` to match
    the format expected by trade-execute for header-based validation.

    Args:
        body: Raw JSON bytes of the serialised payload.
        timestamp: Unix epoch string (seconds) included in the signed message.
        secret: Shared HMAC secret.

    Returns:
        Signature string of the form ``"sha256=<hex_digest>"``.
    """
    msg = f"{timestamp}.{body.decode('utf-8', errors='replace')}".encode()
    digest = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _parse_ack_response(resp: httpx.Response) -> tuple[bool, str | None]:
    """Return (accepted, execution_id) when HTTP 200 and body matches contract."""
    if resp.status_code != 200:
        return False, None
    try:
        data = resp.json()
    except ValueError:
        return False, None
    if not isinstance(data, dict):
        return False, None
    if data.get("accepted") is not True:
        return False, None
    execution_id = data.get("execution_id")
    if not isinstance(execution_id, str) or not execution_id:
        return False, None
    return True, execution_id


# ── Delivery ───────────────────────────────────────────────────────────────


def _audit_delivery(
    *,
    event_id: str,
    symbol: str,
    direction: str,
    alert_class: str,
    status: str,
    http_status: int | None,
    attempt_count: int,
    error_detail: str | None,
    payload_hash: str,
) -> None:
    from db import insert_execution_delivery  # noqa: PLC0415

    insert_execution_delivery(
        event_id=event_id,
        symbol=symbol,
        direction=direction,
        alert_class=alert_class,
        status=status,
        http_status=http_status,
        attempt_count=attempt_count,
        error_detail=error_detail,
        payload_hash=payload_hash,
    )


def _deliver_webhook(
    *,
    body: bytes,
    event_id: str,
    symbol: str,
    direction: str,
    alert_class: str,
) -> bool:
    """POST *body* to trade-execute with retry/backoff and strict ack handling."""
    if not TRADE_EXECUTE_ENABLED:
        return True

    payload_hash = hashlib.sha256(body).hexdigest()

    if TRADE_EXECUTE_DRY_RUN:
        _ts = str(int(time.time()))
        secret = os.environ.get("TRADE_EXECUTE_WEBHOOK_SECRET", "")
        sig = sign_payload(body, _ts, secret) if secret else "sha256=<no-secret-configured>"
        logger.info(
            "TRADE_EXECUTE_DRY_RUN: would POST to %s | "
            "X-TradeAlert-Timestamp: %s | X-TradeAlert-Signature: %s | payload: %s",
            TRADE_EXECUTE_WEBHOOK_URL,
            _ts,
            sig,
            body.decode(),
        )
        try:
            _audit_delivery(
                event_id=event_id,
                symbol=symbol,
                direction=direction,
                alert_class=alert_class,
                status="dry_run",
                http_status=None,
                attempt_count=0,
                error_detail=None,
                payload_hash=payload_hash,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write dry_run audit row: %s", exc)
        return True

    if not TRADE_EXECUTE_WEBHOOK_URL:
        logger.error(
            "TRADE_EXECUTE_ENABLED=true but TRADE_EXECUTE_WEBHOOK_URL is not set — skipping"
        )
        GATE_REJECTIONS.labels(gate="execution_webhook_failed").inc()
        return False

    secret = os.environ.get("TRADE_EXECUTE_WEBHOOK_SECRET", "")
    url = TRADE_EXECUTE_WEBHOOK_URL
    last_error: str | None = None
    last_status: int | None = None
    attempt = 0

    for attempt in range(1, EXECUTION_WEBHOOK_MAX_RETRIES + 1):
        timestamp = str(int(time.time()))
        sig = sign_payload(body, timestamp, secret)
        headers = {
            "Content-Type": "application/json",
            "X-TradeAlert-Timestamp": timestamp,
            "X-TradeAlert-Signature": sig,
        }
        try:
            resp = _get_client().post(url, content=body, headers=headers)
            last_status = resp.status_code
            accepted, execution_id = _parse_ack_response(resp)

            if accepted:
                logger.info(
                    "ExecutionPayload delivered: event_id=%s symbol=%s execution_id=%s "
                    "status=%d attempt=%d",
                    event_id,
                    symbol,
                    execution_id,
                    resp.status_code,
                    attempt,
                )
                try:
                    _audit_delivery(
                        event_id=event_id,
                        symbol=symbol,
                        direction=direction,
                        alert_class=alert_class,
                        status="success",
                        http_status=resp.status_code,
                        attempt_count=attempt,
                        error_detail=None,
                        payload_hash=payload_hash,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to write success audit row: %s", exc)
                return True

            last_error = f"HTTP {resp.status_code}: {resp.text[:500]}"
            logger.error(
                "Execution webhook rejected: event_id=%s symbol=%s body=%s",
                event_id,
                symbol,
                resp.text[:500],
            )

            # Non-retryable 4xx (except 429)
            if resp.status_code != 429 and 400 <= resp.status_code < 500:
                break

            if attempt < EXECUTION_WEBHOOK_MAX_RETRIES:
                delay = TRADE_EXECUTE_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Execution webhook transient error: event_id=%s symbol=%s status=%d "
                    "attempt=%d/%d, retrying in %.1fs",
                    event_id,
                    symbol,
                    resp.status_code,
                    attempt,
                    EXECUTION_WEBHOOK_MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)

        except httpx.TimeoutException as exc:
            last_error = f"Timeout: {exc}"
            if attempt < EXECUTION_WEBHOOK_MAX_RETRIES:
                delay = TRADE_EXECUTE_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Execution webhook timeout: event_id=%s attempt=%d/%d, retrying in %.1fs",
                    event_id,
                    attempt,
                    EXECUTION_WEBHOOK_MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)

        except httpx.RequestError as exc:
            last_error = f"RequestError: {exc}"
            if attempt < EXECUTION_WEBHOOK_MAX_RETRIES:
                delay = TRADE_EXECUTE_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Execution webhook request error: event_id=%s attempt=%d/%d, retrying in %.1fs",
                    event_id,
                    attempt,
                    EXECUTION_WEBHOOK_MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)

    logger.error(
        "Execution webhook delivery failed: event_id=%s symbol=%s attempts=%d error=%s",
        event_id,
        symbol,
        attempt,
        last_error,
    )
    GATE_REJECTIONS.labels(gate="execution_webhook_failed").inc()
    try:
        _audit_delivery(
            event_id=event_id,
            symbol=symbol,
            direction=direction,
            alert_class=alert_class,
            status="failed",
            http_status=last_status,
            attempt_count=attempt,
            error_detail=last_error,
            payload_hash=payload_hash,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to write failure audit row: %s", exc)
    return False


def deliver_execution_payload(payload: ExecutionPayload) -> bool:
    """POST an ExecutionPayload to trade-execute with HMAC signing and retry.

    Success requires HTTP 200 and ``{"accepted": true, "execution_id": "<id>"}``.
    Failure is non-fatal to the caller.
    """
    body: bytes = payload.model_dump_json().encode()
    return _deliver_webhook(
        body=body,
        event_id=payload.idempotency_key,
        symbol=payload.symbol,
        direction=payload.direction,
        alert_class="execute",
    )


def deliver_execution_trigger(trigger: ExecutionTriggerV1) -> bool:
    """POST an ExecutionTriggerV1 to trade-execute with HMAC signing and retry.

    Behaviour:
    - ``TRADE_EXECUTE_ENABLED=false`` → returns ``True`` immediately (no-op).
    - ``TRADE_EXECUTE_DRY_RUN=true``  → logs the payload + headers, inserts a
      ``dry_run`` audit row, returns ``True``.  No HTTP call is made.
    - Otherwise → POSTs with retry/backoff; inserts an audit row on completion.

    Retry policy:
    - Retries on 429, 5xx, invalid ack, or network/timeout errors with exponential backoff.
    - Does NOT retry on 4xx (except 429) — treat as a permanent sender error.
    - After all retries are exhausted, inserts a ``failed`` audit row and
      returns ``False``.

    Failure is non-fatal to the caller.  The notifier continues to Discord
    delivery even when this function returns ``False``.

    Args:
        trigger: Validated ExecutionTriggerV1 ready for transport.

    Returns:
        ``True`` on success (ack accepted or dry-run), ``False`` on permanent failure.
    """
    body: bytes = trigger.model_dump_json().encode()
    return _deliver_webhook(
        body=body,
        event_id=trigger.event_id,
        symbol=trigger.symbol,
        direction=trigger.direction,
        alert_class=trigger.alert_class,
    )
