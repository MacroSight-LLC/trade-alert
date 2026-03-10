"""Unit tests for db.py (SSOT §11/§12).

All Postgres interactions are mocked — no real database required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from models import PlaybookAlert

# ── insert_alert ────────────────────────────────────────────────


class TestInsertAlert:
    """Tests for db.insert_alert()."""

    @patch("db.get_conn")
    def test_returns_generated_id(self, mock_conn_fn: MagicMock, sample_alert: PlaybookAlert) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (42,)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn_fn.return_value = mock_conn

        from db import insert_alert

        result = insert_alert(sample_alert, [{"raw": "data"}])
        assert result == 42
        mock_conn.commit.assert_called_once()
        mock_conn.close.assert_called_once()

    @patch("db.get_conn")
    def test_passes_all_fields(self, mock_conn_fn: MagicMock, sample_alert: PlaybookAlert) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (1,)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn_fn.return_value = mock_conn

        from db import insert_alert

        insert_alert(sample_alert, [])
        args = mock_cur.execute.call_args[0][1]
        assert args[0] == "NVDA"  # symbol
        assert args[1] == "LONG"  # direction
        assert args[2] == pytest.approx(0.82)  # edge_probability

    @patch("db.get_conn")
    def test_closes_conn_on_error(self, mock_conn_fn: MagicMock, sample_alert: PlaybookAlert) -> None:
        mock_cur = MagicMock()
        mock_cur.execute.side_effect = Exception("SQL error")
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn_fn.return_value = mock_conn

        from db import insert_alert

        with pytest.raises(Exception, match="SQL error"):
            insert_alert(sample_alert, [])
        mock_conn.close.assert_called_once()

    @patch("db.get_conn")
    def test_empty_snapshots(self, mock_conn_fn: MagicMock, sample_alert: PlaybookAlert) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (99,)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
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
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn_fn.return_value = mock_conn

        from db import update_outcome

        update_outcome(1, "WIN", 5.0)
        args = mock_cur.execute.call_args[0][1]
        assert args == ("WIN", 5.0, 1)
        mock_conn.commit.assert_called_once()

    @patch("db.get_conn")
    def test_scratch_outcome(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn_fn.return_value = mock_conn

        from db import update_outcome

        update_outcome(42, "SCRATCH", 0.0)
        args = mock_cur.execute.call_args[0][1]
        assert args == ("SCRATCH", 0.0, 42)

    @patch("db.get_conn")
    def test_closes_conn_on_error(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_cur.execute.side_effect = Exception("DB down")
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn_fn.return_value = mock_conn

        from db import update_outcome

        with pytest.raises(Exception, match="DB down"):
            update_outcome(1, "WIN", 5.0)
        mock_conn.close.assert_called_once()


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
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn_fn.return_value = mock_conn

        from db import get_recent_alerts

        rows = get_recent_alerts(limit=10)
        assert len(rows) == 2
        assert rows[0]["symbol"] == "AAPL"

    @patch("db.get_conn")
    def test_empty_table(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn_fn.return_value = mock_conn

        from db import get_recent_alerts

        rows = get_recent_alerts()
        assert rows == []

    @patch("db.get_conn")
    def test_default_limit_is_50(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
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
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn_fn.return_value = mock_conn

        from db import get_winrate_by_bucket

        stats = get_winrate_by_bucket()
        assert len(stats) == 1
        assert stats[0]["wins"] == 7

    @patch("db.get_conn")
    def test_empty_results(self, mock_conn_fn: MagicMock) -> None:
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn_fn.return_value = mock_conn

        from db import get_winrate_by_bucket

        stats = get_winrate_by_bucket()
        assert stats == []
