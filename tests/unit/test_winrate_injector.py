"""Unit tests for winrate_injector module.

Tests get_winrate_context() (with mocked DB) and format_winrate_section().
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from winrate_injector import format_winrate_section, get_winrate_context

# ── format_winrate_section Tests ──────────────────────────────────


class TestFormatWinrateSection:
    """Tests for the markdown formatting helper."""

    def test_empty_buckets_returns_empty_string(self) -> None:
        ctx: dict = {"buckets": {}, "bucket_counts": {}, "total_resolved": 0, "calibration_warning": True}
        assert format_winrate_section(ctx) == ""

    def test_single_bucket_formatted(self) -> None:
        ctx: dict = {
            "buckets": {"LONG_technical_trend": 0.72},
            "bucket_counts": {"LONG_technical_trend": 10},
            "total_resolved": 10,
            "calibration_warning": True,
        }
        result = format_winrate_section(ctx)
        assert "72%" in result
        assert "n=10" in result
        assert "LONG technical" in result

    def test_calibration_warning_shown(self) -> None:
        ctx: dict = {
            "buckets": {"SHORT_volume_spike": 0.50},
            "bucket_counts": {"SHORT_volume_spike": 8},
            "total_resolved": 8,
            "calibration_warning": True,
        }
        result = format_winrate_section(ctx)
        assert "Calibration data thin" in result

    def test_no_warning_above_threshold(self) -> None:
        ctx: dict = {
            "buckets": {"LONG_technical_trend": 0.65},
            "bucket_counts": {"LONG_technical_trend": 35},
            "total_resolved": 35,
            "calibration_warning": False,
        }
        result = format_winrate_section(ctx)
        assert "Calibration data thin" not in result
        assert "65%" in result

    def test_multiple_buckets_sorted(self) -> None:
        ctx: dict = {
            "buckets": {
                "SHORT_volume_spike": 0.40,
                "LONG_technical_trend": 0.70,
            },
            "bucket_counts": {
                "SHORT_volume_spike": 5,
                "LONG_technical_trend": 10,
            },
            "total_resolved": 15,
            "calibration_warning": True,
        }
        result = format_winrate_section(ctx)
        lines = result.split("\n")
        # Sorted: LONG before SHORT
        assert "LONG" in lines[1]
        assert "SHORT" in lines[2]


# ── get_winrate_context Tests ─────────────────────────────────────


def _make_row(direction: str, outcome: str, sig_types: list[str]) -> tuple:
    """Build a faux DB row (direction, outcome, raw_snapshots JSONB)."""
    snapshots = [
        {
            "symbol": "AAPL",
            "signals": [{"type": t, "score": 1.0} for t in sig_types],
        }
    ]
    return (direction, outcome, snapshots)


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
    def test_buckets_below_5_excluded(self, mock_get: MagicMock, mock_put: MagicMock) -> None:
        """4 rows for a bucket → excluded (min_sample=5)."""
        rows = [_make_row("LONG", "WIN", ["technical_trend"]) for _ in range(4)]
        cur = MagicMock()
        cur.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get.return_value = conn

        result = get_winrate_context("15m")
        assert result["buckets"] == {}
        assert result["total_resolved"] == 4
        assert result["calibration_warning"] is True

    @patch("db._put_conn")
    @patch("db.get_conn")
    def test_bucket_at_5_included(self, mock_get: MagicMock, mock_put: MagicMock) -> None:
        """Exactly 5 rows for one bucket → included."""
        rows = [
            _make_row("LONG", "WIN", ["technical_trend"]),
            _make_row("LONG", "WIN", ["technical_trend"]),
            _make_row("LONG", "WIN", ["technical_trend"]),
            _make_row("LONG", "LOSS", ["technical_trend"]),
            _make_row("LONG", "LOSS", ["technical_trend"]),
        ]
        cur = MagicMock()
        cur.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get.return_value = conn

        result = get_winrate_context("15m")
        assert "LONG_technical_trend" in result["buckets"]
        assert result["buckets"]["LONG_technical_trend"] == 0.6  # 3/5
        assert result["bucket_counts"]["LONG_technical_trend"] == 5

    @patch("db._put_conn")
    @patch("db.get_conn")
    def test_calibration_warning_true_below_30(self, mock_get: MagicMock, mock_put: MagicMock) -> None:
        rows = [_make_row("LONG", "WIN", ["technical_trend"]) for _ in range(10)]
        cur = MagicMock()
        cur.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get.return_value = conn

        result = get_winrate_context("15m")
        assert result["calibration_warning"] is True

    @patch("db._put_conn")
    @patch("db.get_conn")
    def test_calibration_warning_false_at_30(self, mock_get: MagicMock, mock_put: MagicMock) -> None:
        rows = [_make_row("LONG", "WIN", ["technical_trend"]) for _ in range(30)]
        cur = MagicMock()
        cur.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get.return_value = conn

        result = get_winrate_context("15m")
        assert result["calibration_warning"] is False

    @patch("db._put_conn")
    @patch("db.get_conn")
    def test_db_error_returns_empty_dict(self, mock_get: MagicMock, mock_put: MagicMock) -> None:
        mock_get.side_effect = Exception("connection refused")

        result = get_winrate_context("15m")
        assert result["buckets"] == {}
        assert result["total_resolved"] == 0

    @patch("db._put_conn")
    @patch("db.get_conn")
    def test_multiple_signal_types_expand_buckets(self, mock_get: MagicMock, mock_put: MagicMock) -> None:
        """Row with 2 signal types → creates 2 separate bucket entries."""
        rows = [_make_row("LONG", "WIN", ["technical_trend", "volume_spike"]) for _ in range(6)]
        cur = MagicMock()
        cur.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get.return_value = conn

        result = get_winrate_context("15m")
        assert "LONG_technical_trend" in result["buckets"]
        assert "LONG_volume_spike" in result["buckets"]
        assert result["buckets"]["LONG_technical_trend"] == 1.0  # 6/6

    @patch("db._put_conn")
    @patch("db.get_conn")
    def test_fallback_bucket_when_no_signals(self, mock_get: MagicMock, mock_put: MagicMock) -> None:
        """Rows with empty raw_snapshots → bucket key is just direction."""
        rows = [("LONG", "WIN", []) for _ in range(6)]
        cur = MagicMock()
        cur.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get.return_value = conn

        result = get_winrate_context("15m")
        assert "LONG" in result["buckets"]
        assert result["buckets"]["LONG"] == 1.0
