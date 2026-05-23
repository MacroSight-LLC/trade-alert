"""Unit tests for scripts/purge_old_data.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "purge_old_data",
    _ROOT / "scripts" / "purge_old_data.py",
)
purge = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(purge)


class TestPurgeOldData:
    @patch("db._put_conn")
    @patch("db.get_conn")
    def test_delete_uses_retention_days(self, mock_get_conn: MagicMock, _mock_put: MagicMock) -> None:
        purge.DATA_RETENTION_DAYS = 90
        purge.PURGE_VACUUM_ENABLED = False
        conn = MagicMock()
        cur = MagicMock()
        cur.rowcount = 3
        conn.cursor.return_value.__enter__.return_value = cur
        mock_get_conn.return_value = conn

        purge.purge_old_alerts()

        sql, params = cur.execute.call_args_list[0][0]
        assert "DELETE FROM alerts" in sql
        assert "make_interval(days => %s)" in sql
        assert params == (90,)
        assert cur.execute.call_count == 1

    @patch("db._put_conn")
    @patch("db.get_conn")
    def test_vacuum_when_enabled(self, mock_get_conn: MagicMock, _mock_put: MagicMock) -> None:
        purge.DATA_RETENTION_DAYS = 180
        purge.PURGE_VACUUM_ENABLED = True
        conn = MagicMock()
        cur = MagicMock()
        cur.rowcount = 0
        conn.cursor.return_value.__enter__.return_value = cur
        mock_get_conn.return_value = conn

        purge.purge_old_alerts()

        assert any("VACUUM ANALYZE alerts" in str(c) for c in cur.execute.call_args_list)
