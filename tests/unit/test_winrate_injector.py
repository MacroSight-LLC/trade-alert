"""Unit tests for winrate_injector module.

Tests get_winrate_context() (with mocked DB) and format_winrate_section().
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from winrate_injector import MIN_WINRATE_SAMPLES, format_winrate_section, get_winrate_context

# ── format_winrate_section Tests ──────────────────────────────────


class TestFormatWinrateSection:
    """Tests for the markdown formatting helper."""

    def test_empty_buckets_returns_empty_string(self) -> None:
        ctx: dict = {"buckets": {}, "bucket_counts": {}, "total_resolved": 0, "calibration_warning": True}
        assert format_winrate_section(ctx) == ""

    def test_single_bucket_formatted(self) -> None:
        ctx: dict = {
            "buckets": {"AAPL_LONG_15m": 0.72},
            "bucket_counts": {"AAPL_LONG_15m": 10},
            "total_resolved": 10,
            "calibration_warning": True,
        }
        result = format_winrate_section(ctx)
        assert "72%" in result
        assert "winrate_sample_count=10" in result
        assert "AAPL_LONG_15m" in result

    def test_calibration_warning_shown(self) -> None:
        ctx: dict = {
            "buckets": {"MSFT_SHORT_15m": 0.50},
            "bucket_counts": {"MSFT_SHORT_15m": 8},
            "total_resolved": 8,
            "calibration_warning": True,
        }
        result = format_winrate_section(ctx)
        assert "Calibration data thin" in result

    def test_no_warning_above_threshold(self) -> None:
        ctx: dict = {
            "buckets": {"AAPL_LONG_15m": 0.65},
            "bucket_counts": {"AAPL_LONG_15m": 35},
            "total_resolved": 35,
            "calibration_warning": False,
        }
        result = format_winrate_section(ctx)
        assert "Calibration data thin" not in result
        assert "65%" in result


# ── get_winrate_context Tests ─────────────────────────────────────


def _make_row(symbol: str, direction: str, outcome: str) -> tuple[str, str, str]:
    return (symbol, direction, outcome)


class TestGetWinrateContext:
    """Tests using a mocked database connection."""

    @patch("db._put_conn")
    @patch("db.get_conn")
    def test_no_rows_returns_empty(self, mock_get: MagicMock, mock_put: MagicMock) -> None:
        cur = MagicMock()
        cur.fetchall.return_value = []
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get.return_value = conn

        result = get_winrate_context("15m")
        assert result["buckets"] == {}
        assert result["total_resolved"] == 0
        assert result["calibration_warning"] is True

    @patch("db._put_conn")
    @patch("db.get_conn")
    def test_buckets_below_min_excluded(self, mock_get: MagicMock, mock_put: MagicMock) -> None:
        rows = [_make_row("AAPL", "LONG", "WIN") for _ in range(MIN_WINRATE_SAMPLES - 1)]
        cur = MagicMock()
        cur.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get.return_value = conn

        result = get_winrate_context("15m")
        assert result["buckets"] == {}
        assert result["total_resolved"] == MIN_WINRATE_SAMPLES - 1

    @patch("db._put_conn")
    @patch("db.get_conn")
    def test_bucket_at_min_included(self, mock_get: MagicMock, mock_put: MagicMock) -> None:
        rows = [
            _make_row("AAPL", "LONG", "WIN"),
            _make_row("AAPL", "LONG", "WIN"),
            _make_row("AAPL", "LONG", "WIN"),
            _make_row("AAPL", "LONG", "LOSS"),
            _make_row("AAPL", "LONG", "LOSS"),
            _make_row("AAPL", "LONG", "LOSS"),
            _make_row("AAPL", "LONG", "LOSS"),
            _make_row("AAPL", "LONG", "LOSS"),
            _make_row("AAPL", "LONG", "LOSS"),
            _make_row("AAPL", "LONG", "LOSS"),
        ]
        cur = MagicMock()
        cur.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get.return_value = conn

        result = get_winrate_context("15m")
        key = "AAPL_LONG_15m"
        assert key in result["buckets"]
        assert result["buckets"][key] == 0.3  # 3/10
        assert result["bucket_counts"][key] == 10

    @patch("db._put_conn")
    @patch("db.get_conn")
    def test_db_error_returns_empty_dict(self, mock_get: MagicMock, mock_put: MagicMock) -> None:
        mock_get.side_effect = Exception("connection refused")

        result = get_winrate_context("15m")
        assert result["buckets"] == {}
        assert result["total_resolved"] == 0
