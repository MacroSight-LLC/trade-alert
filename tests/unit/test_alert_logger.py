"""Unit tests for alert_logger.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from alert_logger import (
    batch_similar_alert_stats,
    extract_forecast_scores,
    get_similar_alert_stats,
    persist_alert,
)
from models import PlaybookAlert


@pytest.fixture()
def sample_alert() -> PlaybookAlert:
    return PlaybookAlert(
        symbol="AAPL",
        direction="LONG",
        edge_probability=0.75,
        confidence=0.80,
        timeframe="15m",
        thesis="Breakout with volume confirmation.",
        entry={"level": 100.0, "stop": 95.0, "target": 110.0},
        timeframe_rationale="15m structure aligns with 1h trend.",
        sentiment_context="Bullish retail flow.",
        unusual_activity=[],
        macro_regime="Risk-on.",
        sources_agree=4,
    )


class TestExtractForecastScores:
    def test_extracts_max_abs_score_per_symbol(self) -> None:
        snapshots = [
            {
                "symbol": "AAPL",
                "signals": [
                    {"type": "price_forecast", "score": 0.5},
                    {"type": "price_forecast", "score": -0.9},
                ],
            },
            {
                "symbol": "MSFT",
                "signals": [{"type": "price_forecast", "score": 0.3}],
            },
        ]
        scores = extract_forecast_scores(snapshots)
        assert scores["AAPL"] == -0.9
        assert scores["MSFT"] == 0.3


class TestGetSimilarAlertStats:
    @patch("db._put_conn")
    @patch("db.get_conn")
    def test_win_rate_string(self, mock_get_conn: MagicMock, mock_put_conn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [("WIN", 3), ("LOSS", 1)]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_get_conn.return_value = mock_conn

        result = get_similar_alert_stats("AAPL", "LONG", 0.75)

        assert "75%" in result
        assert "N=4" in result
        mock_put_conn.assert_called_once_with(mock_conn)

    @patch("db.get_conn", side_effect=Exception("db down"))
    def test_db_error_returns_empty(self, _mock_get_conn: MagicMock) -> None:
        assert get_similar_alert_stats("AAPL", "LONG", 0.75) == ""

    @patch("db._put_conn")
    @patch("db.get_conn")
    def test_first_alert_fallback(self, mock_get_conn: MagicMock, mock_put_conn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_get_conn.return_value = mock_conn

        result = get_similar_alert_stats("AAPL", "LONG", 0.75)

        assert "First alert" in result


class TestBatchSimilarAlertStats:
    @patch("db._put_conn")
    @patch("db.get_conn")
    def test_batch_returns_keyed_stats(
        self,
        mock_get_conn: MagicMock,
        mock_put_conn: MagicMock,
        sample_alert: PlaybookAlert,
    ) -> None:
        msft = sample_alert.model_copy(update={"symbol": "MSFT", "direction": "SHORT"})
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            ("AAPL", "LONG", "WIN", 2),
            ("AAPL", "LONG", "LOSS", 1),
            ("MSFT", "SHORT", "WIN", 1),
            ("MSFT", "SHORT", "LOSS", 1),
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_get_conn.return_value = mock_conn

        result = batch_similar_alert_stats([sample_alert, msft])

        assert "AAPL:LONG" in result
        assert "MSFT:SHORT" in result
        assert "66%" in result["AAPL:LONG"]
        assert "50%" in result["MSFT:SHORT"]
        mock_put_conn.assert_called_once_with(mock_conn)

    def test_empty_alerts_returns_empty_dict(self) -> None:
        assert batch_similar_alert_stats([]) == {}


class TestPersistAlert:
    @patch("alert_logger.insert_alert", return_value=42)
    @patch("alert_logger.DB_INSERTS")
    def test_persist_success(
        self,
        mock_db_inserts: MagicMock,
        mock_insert: MagicMock,
        sample_alert: PlaybookAlert,
    ) -> None:
        snapshots = [{"symbol": "AAPL", "signals": []}]
        forecast_scores = {"AAPL": 0.6}

        result = persist_alert(
            sample_alert,
            snapshots,
            forecast_scores,
            trace_id="trace-123",
        )

        assert result is not None
        alert_id, idempotency_key = result
        assert alert_id == 42
        assert idempotency_key
        mock_insert.assert_called_once()
        call_kwargs = mock_insert.call_args.kwargs
        assert call_kwargs["forecast_score"] == 0.6
        assert call_kwargs["trace_id"] == "trace-123"
        assert call_kwargs["idempotency_key"] == idempotency_key
        mock_db_inserts.labels.return_value.inc.assert_called_once()

    @patch("alert_logger.insert_alert", side_effect=RuntimeError("insert failed"))
    @patch("alert_logger.DB_INSERTS")
    def test_db_error_non_fatal(
        self,
        mock_db_inserts: MagicMock,
        _mock_insert: MagicMock,
        sample_alert: PlaybookAlert,
    ) -> None:
        result = persist_alert(sample_alert, [], {})

        assert result is None
        mock_db_inserts.labels.assert_any_call(status="failure")
