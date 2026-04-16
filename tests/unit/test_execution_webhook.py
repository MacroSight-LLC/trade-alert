"""Unit tests for HMAC signing, retry logic, and delivery in execution_webhook.py."""

from __future__ import annotations

import hashlib
import hmac
import sys
from unittest.mock import MagicMock, patch

import pytest

from execution_trigger import EntryV1, ExecutionTriggerV1


@pytest.fixture()
def sample_trigger() -> ExecutionTriggerV1:
    return ExecutionTriggerV1(
        event_id="evt-test-001",
        correlation_id="corr-test-001",
        generated_at="2026-04-15T12:00:00+00:00",
        expires_at="2026-04-15T12:15:00+00:00",
        symbol="AAPL",
        direction="LONG",
        alert_class="execute",
        entry=EntryV1(price=185.0, stop=182.0, target=192.0, risk_reward=2.3),
        timeframe="15m",
        strategy_id="cuga-playbook-15m",
        conviction_score=0.697,
        conviction_band="high",
        thesis_summary="Breakout with volume.",
    )


def _mock_response(status_code: int, text: str = "OK") -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    return r


def _make_mock_db() -> MagicMock:
    """Return a MagicMock that looks enough like the db module for tests."""
    return MagicMock()


# ── sign_payload ───────────────────────────────────────────────────────────


def test_sign_payload_format():
    from execution_webhook import sign_payload

    sig = sign_payload(b'{"test": "data"}', "1744718400", "mysecret")
    assert sig.startswith("sha256=")
    assert len(sig) == 71  # "sha256=" (7) + 64 hex chars


def test_sign_payload_deterministic():
    from execution_webhook import sign_payload

    body = b'{"test": "data"}'
    assert sign_payload(body, "1744718400", "s") == sign_payload(body, "1744718400", "s")


def test_sign_payload_different_timestamp_produces_different_sig():
    from execution_webhook import sign_payload

    body = b'{"test": "data"}'
    assert sign_payload(body, "1000000", "s") != sign_payload(body, "2000000", "s")


def test_sign_payload_matches_manual_hmac():
    from execution_webhook import sign_payload

    body = b'{"symbol": "AAPL"}'
    ts = "1744718400"
    secret = "test-secret-key"
    msg = f"{ts}.{body.decode()}".encode()
    expected = "sha256=" + hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    assert sign_payload(body, ts, secret) == expected


# ── disabled / dry-run ────────────────────────────────────────────────────


def test_deliver_disabled_returns_true_no_http(sample_trigger: ExecutionTriggerV1):
    with patch("execution_webhook.TRADE_EXECUTE_ENABLED", False):
        mock_get_client = MagicMock()
        with patch("execution_webhook._get_client", mock_get_client):
            from execution_webhook import deliver_execution_trigger

            result = deliver_execution_trigger(sample_trigger)
    assert result is True
    mock_get_client.assert_not_called()


def test_deliver_dry_run_no_http_call(sample_trigger: ExecutionTriggerV1):
    mock_db = _make_mock_db()
    with (
        patch.dict(sys.modules, {"db": mock_db}),
        patch("execution_webhook.TRADE_EXECUTE_ENABLED", True),
        patch("execution_webhook.TRADE_EXECUTE_DRY_RUN", True),
        patch("execution_webhook.TRADE_EXECUTE_WEBHOOK_URL", "http://localhost:9999"),
    ):
        mock_get_client = MagicMock()
        with patch("execution_webhook._get_client", mock_get_client):
            from execution_webhook import deliver_execution_trigger

            result = deliver_execution_trigger(sample_trigger)
    assert result is True
    mock_get_client.assert_not_called()


def test_deliver_dry_run_inserts_dry_run_audit_row(sample_trigger: ExecutionTriggerV1):
    mock_db = _make_mock_db()
    with (
        patch.dict(sys.modules, {"db": mock_db}),
        patch("execution_webhook.TRADE_EXECUTE_ENABLED", True),
        patch("execution_webhook.TRADE_EXECUTE_DRY_RUN", True),
        patch("execution_webhook.TRADE_EXECUTE_WEBHOOK_URL", "http://localhost:9999"),
        patch("execution_webhook._get_client"),
    ):
        from execution_webhook import deliver_execution_trigger

        result = deliver_execution_trigger(sample_trigger)
    assert result is True
    mock_db.insert_execution_delivery.assert_called_once()
    assert mock_db.insert_execution_delivery.call_args.kwargs["status"] == "dry_run"


# ── live delivery — success paths ─────────────────────────────────────────


def test_deliver_success_on_first_attempt(sample_trigger: ExecutionTriggerV1):
    mock_db = _make_mock_db()
    mock_client = MagicMock()
    mock_client.post.return_value = _mock_response(200)
    with (
        patch.dict(sys.modules, {"db": mock_db}),
        patch("execution_webhook.TRADE_EXECUTE_ENABLED", True),
        patch("execution_webhook.TRADE_EXECUTE_DRY_RUN", False),
        patch("execution_webhook.TRADE_EXECUTE_WEBHOOK_URL", "http://trade-execute:8000/webhook"),
        patch("execution_webhook.TRADE_EXECUTE_MAX_RETRIES", 3),
        patch("execution_webhook.TRADE_EXECUTE_RETRY_BACKOFF_SECONDS", 0.0),
        patch("execution_webhook._get_client", return_value=mock_client),
    ):
        from execution_webhook import deliver_execution_trigger

        result = deliver_execution_trigger(sample_trigger)
    assert result is True
    assert mock_client.post.call_count == 1


def test_deliver_retries_on_503_then_succeeds(sample_trigger: ExecutionTriggerV1):
    mock_db = _make_mock_db()
    mock_client = MagicMock()
    mock_client.post.side_effect = [_mock_response(503), _mock_response(200)]
    with (
        patch.dict(sys.modules, {"db": mock_db}),
        patch("execution_webhook.TRADE_EXECUTE_ENABLED", True),
        patch("execution_webhook.TRADE_EXECUTE_DRY_RUN", False),
        patch("execution_webhook.TRADE_EXECUTE_WEBHOOK_URL", "http://trade-execute:8000/webhook"),
        patch("execution_webhook.TRADE_EXECUTE_MAX_RETRIES", 3),
        patch("execution_webhook.TRADE_EXECUTE_RETRY_BACKOFF_SECONDS", 0.0),
        patch("execution_webhook._get_client", return_value=mock_client),
        patch("execution_webhook.time.sleep"),
    ):
        from execution_webhook import deliver_execution_trigger

        result = deliver_execution_trigger(sample_trigger)
    assert result is True
    assert mock_client.post.call_count == 2


# ── live delivery — failure paths ─────────────────────────────────────────


def test_deliver_no_retry_on_400(sample_trigger: ExecutionTriggerV1):
    mock_db = _make_mock_db()
    mock_client = MagicMock()
    mock_client.post.return_value = _mock_response(400, "Bad Request")
    with (
        patch.dict(sys.modules, {"db": mock_db}),
        patch("execution_webhook.TRADE_EXECUTE_ENABLED", True),
        patch("execution_webhook.TRADE_EXECUTE_DRY_RUN", False),
        patch("execution_webhook.TRADE_EXECUTE_WEBHOOK_URL", "http://trade-execute:8000/webhook"),
        patch("execution_webhook.TRADE_EXECUTE_MAX_RETRIES", 3),
        patch("execution_webhook.TRADE_EXECUTE_RETRY_BACKOFF_SECONDS", 0.0),
        patch("execution_webhook._get_client", return_value=mock_client),
    ):
        from execution_webhook import deliver_execution_trigger

        result = deliver_execution_trigger(sample_trigger)
    assert result is False
    assert mock_client.post.call_count == 1


def test_deliver_exhausted_retries_returns_false(sample_trigger: ExecutionTriggerV1):
    mock_db = _make_mock_db()
    mock_client = MagicMock()
    mock_client.post.return_value = _mock_response(503)
    with (
        patch.dict(sys.modules, {"db": mock_db}),
        patch("execution_webhook.TRADE_EXECUTE_ENABLED", True),
        patch("execution_webhook.TRADE_EXECUTE_DRY_RUN", False),
        patch("execution_webhook.TRADE_EXECUTE_WEBHOOK_URL", "http://trade-execute:8000/webhook"),
        patch("execution_webhook.TRADE_EXECUTE_MAX_RETRIES", 3),
        patch("execution_webhook.TRADE_EXECUTE_RETRY_BACKOFF_SECONDS", 0.0),
        patch("execution_webhook._get_client", return_value=mock_client),
        patch("execution_webhook.time.sleep"),
    ):
        from execution_webhook import deliver_execution_trigger

        result = deliver_execution_trigger(sample_trigger)
    assert result is False
    assert mock_client.post.call_count == 3


# ── audit row assertions ──────────────────────────────────────────────────


def test_deliver_inserts_success_audit_row(sample_trigger: ExecutionTriggerV1):
    mock_db = _make_mock_db()
    mock_client = MagicMock()
    mock_client.post.return_value = _mock_response(200)
    with (
        patch.dict(sys.modules, {"db": mock_db}),
        patch("execution_webhook.TRADE_EXECUTE_ENABLED", True),
        patch("execution_webhook.TRADE_EXECUTE_DRY_RUN", False),
        patch("execution_webhook.TRADE_EXECUTE_WEBHOOK_URL", "http://trade-execute:8000/webhook"),
        patch("execution_webhook.TRADE_EXECUTE_MAX_RETRIES", 1),
        patch("execution_webhook.TRADE_EXECUTE_RETRY_BACKOFF_SECONDS", 0.0),
        patch("execution_webhook._get_client", return_value=mock_client),
    ):
        from execution_webhook import deliver_execution_trigger

        deliver_execution_trigger(sample_trigger)
    mock_db.insert_execution_delivery.assert_called_once()
    assert mock_db.insert_execution_delivery.call_args.kwargs["status"] == "success"


def test_deliver_inserts_failed_audit_row_on_exhaustion(sample_trigger: ExecutionTriggerV1):
    mock_db = _make_mock_db()
    mock_client = MagicMock()
    mock_client.post.return_value = _mock_response(503)
    with (
        patch.dict(sys.modules, {"db": mock_db}),
        patch("execution_webhook.TRADE_EXECUTE_ENABLED", True),
        patch("execution_webhook.TRADE_EXECUTE_DRY_RUN", False),
        patch("execution_webhook.TRADE_EXECUTE_WEBHOOK_URL", "http://trade-execute:8000/webhook"),
        patch("execution_webhook.TRADE_EXECUTE_MAX_RETRIES", 2),
        patch("execution_webhook.TRADE_EXECUTE_RETRY_BACKOFF_SECONDS", 0.0),
        patch("execution_webhook._get_client", return_value=mock_client),
        patch("execution_webhook.time.sleep"),
    ):
        from execution_webhook import deliver_execution_trigger

        result = deliver_execution_trigger(sample_trigger)
    assert result is False
    mock_db.insert_execution_delivery.assert_called_once()
    assert mock_db.insert_execution_delivery.call_args.kwargs["status"] == "failed"


def test_deliver_inserts_failed_audit_row_on_4xx(sample_trigger: ExecutionTriggerV1):
    mock_db = _make_mock_db()
    mock_client = MagicMock()
    mock_client.post.return_value = _mock_response(422, "Unprocessable")
    with (
        patch.dict(sys.modules, {"db": mock_db}),
        patch("execution_webhook.TRADE_EXECUTE_ENABLED", True),
        patch("execution_webhook.TRADE_EXECUTE_DRY_RUN", False),
        patch("execution_webhook.TRADE_EXECUTE_WEBHOOK_URL", "http://trade-execute:8000/webhook"),
        patch("execution_webhook.TRADE_EXECUTE_MAX_RETRIES", 3),
        patch("execution_webhook.TRADE_EXECUTE_RETRY_BACKOFF_SECONDS", 0.0),
        patch("execution_webhook._get_client", return_value=mock_client),
    ):
        from execution_webhook import deliver_execution_trigger

        result = deliver_execution_trigger(sample_trigger)
    assert result is False
    mock_db.insert_execution_delivery.assert_called_once()
    assert mock_db.insert_execution_delivery.call_args.kwargs["status"] == "failed"


# ── HMAC headers ──────────────────────────────────────────────────────────


def test_hmac_headers_present_in_post_call(sample_trigger: ExecutionTriggerV1):
    mock_db = _make_mock_db()
    mock_client = MagicMock()
    mock_client.post.return_value = _mock_response(200)
    with (
        patch.dict(sys.modules, {"db": mock_db}),
        patch("execution_webhook.TRADE_EXECUTE_ENABLED", True),
        patch("execution_webhook.TRADE_EXECUTE_DRY_RUN", False),
        patch("execution_webhook.TRADE_EXECUTE_WEBHOOK_URL", "http://trade-execute:8000/webhook"),
        patch("execution_webhook.TRADE_EXECUTE_MAX_RETRIES", 1),
        patch("execution_webhook.TRADE_EXECUTE_RETRY_BACKOFF_SECONDS", 0.0),
        patch("execution_webhook._get_client", return_value=mock_client),
    ):
        from execution_webhook import deliver_execution_trigger

        deliver_execution_trigger(sample_trigger)

    headers = mock_client.post.call_args.kwargs["headers"]
    assert "X-TradeAlert-Timestamp" in headers
    assert "X-TradeAlert-Signature" in headers
    assert headers["X-TradeAlert-Signature"].startswith("sha256=")
    assert headers["X-TradeAlert-Timestamp"].isdigit()


def test_hmac_content_type_json(sample_trigger: ExecutionTriggerV1):
    mock_db = _make_mock_db()
    mock_client = MagicMock()
    mock_client.post.return_value = _mock_response(200)
    with (
        patch.dict(sys.modules, {"db": mock_db}),
        patch("execution_webhook.TRADE_EXECUTE_ENABLED", True),
        patch("execution_webhook.TRADE_EXECUTE_DRY_RUN", False),
        patch("execution_webhook.TRADE_EXECUTE_WEBHOOK_URL", "http://trade-execute:8000/webhook"),
        patch("execution_webhook.TRADE_EXECUTE_MAX_RETRIES", 1),
        patch("execution_webhook.TRADE_EXECUTE_RETRY_BACKOFF_SECONDS", 0.0),
        patch("execution_webhook._get_client", return_value=mock_client),
    ):
        from execution_webhook import deliver_execution_trigger

        deliver_execution_trigger(sample_trigger)

    headers = mock_client.post.call_args.kwargs["headers"]
    assert headers["Content-Type"] == "application/json"

