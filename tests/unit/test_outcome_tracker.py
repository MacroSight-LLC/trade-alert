"""Unit tests for outcome_tracker.py (SSOT §12)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from outcome_tracker import _map_db_row, evaluate_outcome, get_current_price, run_tracker_cycle


@pytest.fixture()
def long_alert() -> dict:
    return {
        "direction": "LONG",
        "entry_level": 185.0,
        "stop_level": 182.0,
        "target_level": 192.0,
        "fired_at": datetime.now(timezone.utc) - timedelta(hours=1),
    }


@pytest.fixture()
def short_alert() -> dict:
    return {
        "direction": "SHORT",
        "entry_level": 185.0,
        "stop_level": 188.0,
        "target_level": 178.0,
        "fired_at": datetime.now(timezone.utc) - timedelta(hours=1),
    }


class TestEvaluateOutcome:
    """Tests for evaluate_outcome function."""

    # ── LONG outcomes ──

    def test_long_win(self, long_alert: dict) -> None:
        assert evaluate_outcome(long_alert, 193.0) == "WIN"

    def test_long_win_exact_target(self, long_alert: dict) -> None:
        assert evaluate_outcome(long_alert, 192.0) == "WIN"

    def test_long_loss(self, long_alert: dict) -> None:
        assert evaluate_outcome(long_alert, 181.0) == "LOSS"

    def test_long_loss_exact_stop(self, long_alert: dict) -> None:
        assert evaluate_outcome(long_alert, 182.0) == "LOSS"

    def test_long_open(self, long_alert: dict) -> None:
        assert evaluate_outcome(long_alert, 186.0) is None

    # ── SHORT outcomes ──

    def test_short_win(self, short_alert: dict) -> None:
        assert evaluate_outcome(short_alert, 177.0) == "WIN"

    def test_short_win_exact_target(self, short_alert: dict) -> None:
        assert evaluate_outcome(short_alert, 178.0) == "WIN"

    def test_short_loss(self, short_alert: dict) -> None:
        assert evaluate_outcome(short_alert, 189.0) == "LOSS"

    def test_short_loss_exact_stop(self, short_alert: dict) -> None:
        assert evaluate_outcome(short_alert, 188.0) == "LOSS"

    def test_short_open(self, short_alert: dict) -> None:
        assert evaluate_outcome(short_alert, 184.0) is None

    # ── Expiry ──

    def test_expired_long(self, long_alert: dict) -> None:
        long_alert["fired_at"] = datetime.now(timezone.utc) - timedelta(hours=5)
        assert evaluate_outcome(long_alert, 186.0) == "EXPIRED"

    def test_expired_short(self, short_alert: dict) -> None:
        short_alert["fired_at"] = datetime.now(timezone.utc) - timedelta(hours=5)
        assert evaluate_outcome(short_alert, 184.0) == "EXPIRED"

    def test_not_expired_within_window(self, long_alert: dict) -> None:
        long_alert["fired_at"] = datetime.now(timezone.utc) - timedelta(hours=3)
        assert evaluate_outcome(long_alert, 186.0) is None

    # ── Edge cases ──

    def test_missing_direction(self) -> None:
        bad = {
            "entry_level": 100,
            "stop_level": 95,
            "target_level": 110,
            "fired_at": datetime.now(timezone.utc),
        }
        assert evaluate_outcome(bad, 100.0) is None

    def test_invalid_direction_returns_none(self) -> None:
        bad = {
            "direction": "INVALID",
            "entry_level": 100,
            "stop_level": 95,
            "target_level": 110,
            "fired_at": datetime.now(timezone.utc),
        }
        assert evaluate_outcome(bad, 100.0) is None

    def test_missing_stop_level(self, long_alert: dict) -> None:
        del long_alert["stop_level"]
        assert evaluate_outcome(long_alert, 100.0) is None

    def test_none_price_handled(self, long_alert: dict) -> None:
        # evaluate_outcome expects float, but test defensive behavior
        assert evaluate_outcome(long_alert, None) is None  # type: ignore[arg-type]


# ── _map_db_row ─────────────────────────────────────────────────


class TestMapDbRow:
    """Tests for _map_db_row mapping Postgres rows to flat dicts."""

    def test_json_entry_column(self) -> None:
        row = {
            "id": 1,
            "entry": {"level": 100.0, "stop": 95.0, "target": 110.0},
            "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        }
        mapped = _map_db_row(row)
        assert mapped["entry_level"] == 100.0
        assert mapped["stop_level"] == 95.0
        assert mapped["target_level"] == 110.0
        assert mapped["fired_at"] == row["created_at"]

    def test_string_entry_column(self) -> None:
        row = {
            "id": 2,
            "entry": json.dumps({"level": 50.0, "stop": 48.0, "target": 55.0}),
            "created_at": datetime(2024, 6, 1, tzinfo=timezone.utc),
        }
        mapped = _map_db_row(row)
        assert mapped["entry_level"] == 50.0
        assert mapped["stop_level"] == 48.0

    def test_missing_entry_keys_default_zero(self) -> None:
        row = {"id": 3, "entry": {}, "created_at": None}
        mapped = _map_db_row(row)
        assert mapped["entry_level"] == 0.0
        assert mapped["stop_level"] == 0.0
        assert mapped["target_level"] == 0.0

    def test_preserves_other_keys(self) -> None:
        row = {"id": 5, "symbol": "SPY", "entry": {}, "created_at": None}
        mapped = _map_db_row(row)
        assert mapped["symbol"] == "SPY"


# ── get_current_price ───────────────────────────────────────────


class TestGetCurrentPrice:
    """Tests for Polygon.io price fetching."""

    @patch("outcome_tracker.POLYGON_API_KEY", "test-key")
    @patch("outcome_tracker.httpx.get")
    def test_success(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"ticker": {"day": {"c": 185.50}}}),
        )
        mock_get.return_value.raise_for_status = MagicMock()
        assert get_current_price("AAPL") == pytest.approx(185.50)

    @patch("outcome_tracker.POLYGON_API_KEY", "test-key")
    @patch("outcome_tracker.PRICE_FETCH_MAX_RETRIES", 1)
    @patch("outcome_tracker.httpx.get")
    def test_http_error_returns_none(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = httpx.HTTPError("500")
        assert get_current_price("AAPL") is None

    @patch("outcome_tracker.POLYGON_API_KEY", "test-key")
    @patch("outcome_tracker.PRICE_FETCH_MAX_RETRIES", 1)
    @patch("outcome_tracker.httpx.get")
    def test_bad_json_returns_none(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"bad": "shape"}),
        )
        mock_get.return_value.raise_for_status = MagicMock()
        assert get_current_price("AAPL") is None

    @patch("outcome_tracker.POLYGON_API_KEY", None)
    def test_no_api_key_returns_none(self) -> None:
        assert get_current_price("AAPL") is None

    @patch("outcome_tracker.POLYGON_API_KEY", "test-key")
    @patch("outcome_tracker.PRICE_FETCH_MAX_RETRIES", 3)
    @patch("outcome_tracker.time.sleep")
    @patch("outcome_tracker.httpx.get")
    def test_retries_on_failure(self, mock_get: MagicMock, mock_sleep: MagicMock) -> None:
        """Verify retry logic with exponential backoff."""
        mock_get.side_effect = [
            httpx.HTTPError("503"),
            httpx.HTTPError("503"),
            MagicMock(
                status_code=200,
                json=MagicMock(return_value={"ticker": {"day": {"c": 190.0}}}),
                raise_for_status=MagicMock(),
            ),
        ]
        result = get_current_price("AAPL")
        assert result == pytest.approx(190.0)
        assert mock_get.call_count == 3
        assert mock_sleep.call_count == 2


# ── run_tracker_cycle ───────────────────────────────────────────


class TestRunTrackerCycle:
    """Tests for run_tracker_cycle with mocked DB and price API."""

    @patch("outcome_tracker.update_outcome")
    @patch("outcome_tracker.get_current_price", return_value=195.0)
    @patch("outcome_tracker.get_recent_alerts")
    def test_resolves_winning_alert(
        self,
        mock_alerts: MagicMock,
        _price: MagicMock,
        mock_update: MagicMock,
    ) -> None:
        mock_alerts.return_value = [
            {
                "id": 10,
                "symbol": "AAPL",
                "direction": "LONG",
                "entry": {"level": 185.0, "stop": 182.0, "target": 192.0},
                "created_at": datetime.now(timezone.utc) - timedelta(hours=1),
                "outcome": None,
            },
        ]
        resolved = run_tracker_cycle()
        assert resolved == 1
        mock_update.assert_called_once()
        call_args = mock_update.call_args[0]
        assert call_args[0] == 10  # alert_id
        assert call_args[1] == "WIN"

    @patch("outcome_tracker.update_outcome")
    @patch("outcome_tracker.get_current_price", return_value=186.0)
    @patch("outcome_tracker.get_recent_alerts")
    def test_skips_already_resolved(
        self,
        mock_alerts: MagicMock,
        _price: MagicMock,
        mock_update: MagicMock,
    ) -> None:
        mock_alerts.return_value = [
            {
                "id": 11,
                "symbol": "AAPL",
                "direction": "LONG",
                "entry": {"level": 185.0, "stop": 182.0, "target": 192.0},
                "created_at": datetime.now(timezone.utc) - timedelta(hours=1),
                "outcome": "WIN",
            },
        ]
        assert run_tracker_cycle() == 0
        mock_update.assert_not_called()

    @patch("outcome_tracker.update_outcome")
    @patch("outcome_tracker.get_current_price", return_value=186.0)
    @patch("outcome_tracker.get_recent_alerts")
    def test_skips_watch_alerts(
        self,
        mock_alerts: MagicMock,
        _price: MagicMock,
        mock_update: MagicMock,
    ) -> None:
        mock_alerts.return_value = [
            {
                "id": 12,
                "symbol": "SPY",
                "direction": "WATCH",
                "entry": {"level": 500.0, "stop": 490.0, "target": 510.0},
                "created_at": datetime.now(timezone.utc) - timedelta(hours=1),
                "outcome": None,
            },
        ]
        assert run_tracker_cycle() == 0
        mock_update.assert_not_called()

    @patch("outcome_tracker.get_recent_alerts", side_effect=Exception("DB down"))
    def test_db_error_returns_zero(self, _alerts: MagicMock) -> None:
        assert run_tracker_cycle() == 0

    @patch("outcome_tracker.update_outcome")
    @patch("outcome_tracker.get_current_price", return_value=None)
    @patch("outcome_tracker.get_recent_alerts")
    def test_no_price_skips(
        self,
        mock_alerts: MagicMock,
        _price: MagicMock,
        mock_update: MagicMock,
    ) -> None:
        mock_alerts.return_value = [
            {
                "id": 13,
                "symbol": "AAPL",
                "direction": "LONG",
                "entry": {"level": 185.0, "stop": 182.0, "target": 192.0},
                "created_at": datetime.now(timezone.utc) - timedelta(hours=1),
                "outcome": None,
            },
        ]
        assert run_tracker_cycle() == 0
        mock_update.assert_not_called()

    @patch("outcome_tracker.update_outcome")
    @patch("outcome_tracker.get_current_price", return_value=186.0)
    @patch("outcome_tracker.get_recent_alerts")
    def test_expired_maps_to_scratch(
        self,
        mock_alerts: MagicMock,
        _price: MagicMock,
        mock_update: MagicMock,
    ) -> None:
        mock_alerts.return_value = [
            {
                "id": 14,
                "symbol": "AAPL",
                "direction": "LONG",
                "entry": {"level": 185.0, "stop": 182.0, "target": 192.0},
                "created_at": datetime.now(timezone.utc) - timedelta(hours=5),
                "outcome": None,
            },
        ]
        resolved = run_tracker_cycle()
        assert resolved == 1
        call_args = mock_update.call_args[0]
        assert call_args[1] == "SCRATCH"  # EXPIRED → SCRATCH for DB
