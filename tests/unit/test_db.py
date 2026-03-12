"""Unit tests for db.py (SSOT §11/§12).

All Postgres interactions are mocked — no real database required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from models import PlaybookAlert


def _mock_conn_with_cursor(mock_cur: MagicMock) -> MagicMock:
    """Create a MagicMock connection supporting context-manager protocol."""
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn


# ── insert_alert ────────────────────────────────────────────────


class TestInsertAlert:
    """Tests for db.insert_alert()."""

    @patch("db.get_conn")
    def test_returns_generated_id(self, mock_conn_fn: MagicMock, sample_alert: PlaybookAlert) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (42,)
        mock_conn = _mock_conn_with_cursor(mock_cur)
        mock_conn_fn.return_value = mock_conn

        from db import insert_alert

        result = insert_alert(sample_alert, [{"raw": "data"}])
        assert result == 42
        mock_conn.commit.assert_called_once()


class TestGetConn:
    """Tests for db.get_conn()."""

    @patch("db._pool", None)
    @patch("db.DATABASE_URL", None)
    def test_raises_when_database_url_not_set(self) -> None:
        from db import get_conn

        with pytest.raises(RuntimeError, match="DATABASE_URL not set"):
            get_conn()

    @patch("db._pool", None)
    @patch("db.psycopg2.pool.SimpleConnectionPool")
    @patch("db.DATABASE_URL", "postgresql://user:pass@host/db")
    def test_creates_pool_with_params(self, mock_pool_cls: MagicMock) -> None:
        mock_pool = MagicMock()
        mock_pool.closed = False
        mock_pool.getconn.return_value = MagicMock()
        mock_pool_cls.return_value = mock_pool

        from db import get_conn

        get_conn()
        mock_pool_cls.assert_called_once_with(
            minconn=1,
            maxconn=5,
            dsn="postgresql://user:pass@host/db",
            connect_timeout=30,
        )

    @patch("db.get_conn")
    def test_passes_all_fields(self, mock_conn_fn: MagicMock, sample_alert: PlaybookAlert) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (1,)
        mock_conn = _mock_conn_with_cursor(mock_cur)
        mock_conn_fn.return_value = mock_conn

        from db import insert_alert

        insert_alert(sample_alert, [])
        args = mock_cur.execute.call_args[0][1]
        assert args[0] == "NVDA"  # symbol
        assert args[1] == "LONG"  # direction
        assert args[2] == pytest.approx(0.82)  # edge_probability

    @patch("db.get_conn")
    def test_raises_on_error(self, mock_conn_fn: MagicMock, sample_alert: PlaybookAlert) -> None:
        mock_cur = MagicMock()
        mock_cur.execute.side_effect = Exception("SQL error")
        mock_conn = _mock_conn_with_cursor(mock_cur)
        mock_conn_fn.return_value = mock_conn

        from db import insert_alert

        with pytest.raises(Exception, match="SQL error"):
            insert_alert(sample_alert, [])

    @patch("db.get_conn")
    def test_empty_snapshots(self, mock_conn_fn: MagicMock, sample_alert: PlaybookAlert) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (99,)
        mock_conn = _mock_conn_with_cursor(mock_cur)
        mock_conn_fn.return_value = mock_conn

        from db import insert_alert

        result = insert_alert(sample_alert, [])
        assert result == 99


# ── update_outcome ──────────────────────────────────────────────


class TestUpdateOutcome:
    """Tests for db.update_outcome()."""

    @patch("db.get_conn")
    def test_win_outcome(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_conn = _mock_conn_with_cursor(mock_cur)
        mock_conn_fn.return_value = mock_conn

        from db import update_outcome

        update_outcome(1, "WIN", 5.0)
        args = mock_cur.execute.call_args[0][1]
        assert args == ("WIN", 5.0, 1)
        mock_conn.commit.assert_called_once()

    @patch("db.get_conn")
    def test_scratch_outcome(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_conn = _mock_conn_with_cursor(mock_cur)
        mock_conn_fn.return_value = mock_conn

        from db import update_outcome

        update_outcome(42, "SCRATCH", 0.0)
        args = mock_cur.execute.call_args[0][1]
        assert args == ("SCRATCH", 0.0, 42)

    @patch("db.get_conn")
    def test_raises_on_error(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_cur.execute.side_effect = Exception("DB down")
        mock_conn = _mock_conn_with_cursor(mock_cur)
        mock_conn_fn.return_value = mock_conn

        from db import update_outcome

        with pytest.raises(Exception, match="DB down"):
            update_outcome(1, "WIN", 5.0)


# ── get_recent_alerts ───────────────────────────────────────────


class TestGetRecentAlerts:
    """Tests for db.get_recent_alerts()."""

    @patch("db.get_conn")
    def test_returns_list_of_dicts(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            {"id": 1, "symbol": "AAPL"},
            {"id": 2, "symbol": "NVDA"},
        ]
        mock_conn = _mock_conn_with_cursor(mock_cur)
        mock_conn_fn.return_value = mock_conn

        from db import get_recent_alerts

        rows = get_recent_alerts(limit=10)
        assert len(rows) == 2
        assert rows[0]["symbol"] == "AAPL"

    @patch("db.get_conn")
    def test_empty_table(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn = _mock_conn_with_cursor(mock_cur)
        mock_conn_fn.return_value = mock_conn

        from db import get_recent_alerts

        rows = get_recent_alerts()
        assert rows == []

    @patch("db.get_conn")
    def test_default_limit_is_50(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn = _mock_conn_with_cursor(mock_cur)
        mock_conn_fn.return_value = mock_conn

        from db import get_recent_alerts

        get_recent_alerts()
        args = mock_cur.execute.call_args[0][1]
        assert args == (50,)


# ── get_winrate_by_bucket ───────────────────────────────────────


class TestGetWinrateByBucket:
    """Tests for db.get_winrate_by_bucket()."""

    @patch("db.get_conn")
    def test_returns_bucket_stats(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            {"bucket": 0.8, "total": 10, "wins": 7, "avg_pnl": 2.5},
        ]
        mock_conn = _mock_conn_with_cursor(mock_cur)
        mock_conn_fn.return_value = mock_conn

        from db import get_winrate_by_bucket

        stats = get_winrate_by_bucket()
        assert len(stats) == 1
        assert stats[0]["wins"] == 7

    @patch("db.get_conn")
    def test_empty_results(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn = _mock_conn_with_cursor(mock_cur)
        mock_conn_fn.return_value = mock_conn

        from db import get_winrate_by_bucket

        stats = get_winrate_by_bucket()
        assert stats == []


# ── get_alert_frequency ─────────────────────────────────────────


class TestGetAlertFrequency:
    """Tests for db.get_alert_frequency()."""

    @patch("db.get_conn")
    def test_returns_daily_counts(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            {"date": "2026-03-10", "total": 15, "longs": 8, "shorts": 5, "watches": 2},
            {"date": "2026-03-11", "total": 12, "longs": 6, "shorts": 4, "watches": 2},
        ]
        mock_conn = _mock_conn_with_cursor(mock_cur)
        mock_conn_fn.return_value = mock_conn

        from db import get_alert_frequency

        rows = get_alert_frequency(days=7)
        assert len(rows) == 2
        assert rows[0]["total"] == 15

    @patch("db.get_conn")
    def test_empty(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn = _mock_conn_with_cursor(mock_cur)
        mock_conn_fn.return_value = mock_conn

        from db import get_alert_frequency

        assert get_alert_frequency() == []


# ── get_symbol_performance ──────────────────────────────────────


class TestGetSymbolPerformance:
    """Tests for db.get_symbol_performance()."""

    @patch("db.get_conn")
    def test_returns_symbols(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            {
                "symbol": "AAPL",
                "total": 25,
                "wins": 15,
                "losses": 8,
                "winrate": 0.65,
                "avg_edge": 0.78,
                "avg_pnl": 0.02,
            },
        ]
        mock_conn = _mock_conn_with_cursor(mock_cur)
        mock_conn_fn.return_value = mock_conn

        from db import get_symbol_performance

        rows = get_symbol_performance(limit=10)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "AAPL"

    @patch("db.get_conn")
    def test_default_limit_20(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn = _mock_conn_with_cursor(mock_cur)
        mock_conn_fn.return_value = mock_conn

        from db import get_symbol_performance

        get_symbol_performance()
        args = mock_cur.execute.call_args[0][1]
        assert args == (20,)


# ── get_summary_stats ───────────────────────────────────────────


class TestGetSummaryStats:
    """Tests for db.get_summary_stats()."""

    @patch("db.get_conn")
    def test_returns_summary(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        # Two queries: summary + kpi
        mock_cur.fetchone.side_effect = [
            {
                "total_alerts": 100,
                "resolved": 80,
                "wins": 50,
                "losses": 25,
                "scratches": 5,
                "overall_winrate": 0.625,
                "avg_edge": 0.78,
                "avg_pnl": 0.01,
                "alerts_today": 8,
            },
            {"kpi_winrate_70": 0.70},
        ]
        mock_conn = _mock_conn_with_cursor(mock_cur)
        mock_conn_fn.return_value = mock_conn

        from db import get_summary_stats

        result = get_summary_stats()
        assert result["total_alerts"] == 100
        assert result["kpi_winrate_70"] == 0.70
        assert result["alerts_today"] == 8
