"""Unit tests for vault_env_loader VAULT_REQUIRED fail-fast paths."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

import vault_env_loader as vel


@pytest.fixture(autouse=True)
def _reset_loader_state() -> None:
    vel._loaded = False
    yield
    vel._loaded = False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("TRUE", True),
        ("false", False),
        ("", False),
        ("0", False),
    ],
)
def test_vault_required_truthy_values(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("VAULT_REQUIRED", value)
    assert vel._vault_required() is expected


def test_missing_creds_raises_when_vault_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    monkeypatch.setenv("VAULT_REQUIRED", "true")

    with pytest.raises(RuntimeError, match="VAULT_ADDR/VAULT_TOKEN not set"):
        vel.load_vault_secrets()


def test_missing_creds_silent_when_vault_not_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    monkeypatch.delenv("VAULT_REQUIRED", raising=False)

    assert vel.load_vault_secrets() == 0


def test_hvac_missing_raises_when_vault_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
    monkeypatch.setenv("VAULT_TOKEN", "test-token")
    monkeypatch.setenv("VAULT_REQUIRED", "true")

    with patch.dict("sys.modules", {"hvac": None}):
        with pytest.raises(RuntimeError, match="hvac package not installed"):
            vel.load_vault_secrets()


def test_auth_failure_raises_when_vault_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
    monkeypatch.setenv("VAULT_TOKEN", "bad-token")
    monkeypatch.setenv("VAULT_REQUIRED", "true")

    mock_client = MagicMock()
    mock_client.is_authenticated.return_value = False

    with patch("hvac.Client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="Vault authentication failed"):
            vel.load_vault_secrets()


def test_empty_secret_path_raises_when_vault_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
    monkeypatch.setenv("VAULT_TOKEN", "test-token")
    monkeypatch.setenv("VAULT_REQUIRED", "true")

    mock_client = MagicMock()
    mock_client.is_authenticated.return_value = True
    mock_client.secrets.kv.v2.read_secret_version.return_value = {"data": {"data": {}}}

    with patch("hvac.Client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="is empty"):
            vel.load_vault_secrets()


def test_unreachable_raises_when_vault_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
    monkeypatch.setenv("VAULT_TOKEN", "test-token")
    monkeypatch.setenv("VAULT_REQUIRED", "true")

    mock_client = MagicMock()
    mock_client.is_authenticated.return_value = True
    mock_client.secrets.kv.v2.read_secret_version.side_effect = ConnectionError("connection refused")

    with (
        patch("hvac.Client", return_value=mock_client),
        patch("time.sleep"),
    ):
        with pytest.raises(RuntimeError, match="Vault read failed after 3 attempts"):
            vel.load_vault_secrets()


def test_successful_load_injects_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
    monkeypatch.setenv("VAULT_TOKEN", "test-token")
    monkeypatch.delenv("VAULT_REQUIRED", raising=False)
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)

    mock_client = MagicMock()
    mock_client.is_authenticated.return_value = True
    mock_client.secrets.kv.v2.read_secret_version.return_value = {
        "data": {"data": {"polygon_api_key": "secret-value", "discord_bot_token": "bot-token"}}
    }

    with patch("hvac.Client", return_value=mock_client):
        count = vel.load_vault_secrets()

    assert count == 2
    assert os.environ["POLYGON_API_KEY"] == "secret-value"
    assert os.environ["DISCORD_BOT_TOKEN"] == "bot-token"


def test_loader_runs_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
    monkeypatch.setenv("VAULT_TOKEN", "test-token")
    monkeypatch.delenv("VAULT_REQUIRED", raising=False)

    mock_client = MagicMock()
    mock_client.is_authenticated.return_value = True
    mock_client.secrets.kv.v2.read_secret_version.return_value = {
        "data": {"data": {"polygon_api_key": "secret-value"}}
    }

    with patch("hvac.Client", return_value=mock_client):
        first = vel.load_vault_secrets()
        second = vel.load_vault_secrets()

    assert first == 1
    assert second == 0
    mock_client.secrets.kv.v2.read_secret_version.assert_called_once()
