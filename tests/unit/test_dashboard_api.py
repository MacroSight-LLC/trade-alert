"""Unit tests for dashboard_api.py and db analytics queries.

Tests cover all API endpoints with mocked DB functions, response
model validation, query parameter handling, and edge cases.
"""

from __future__ import annotations

import json
from decimal import Decimal
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from dashboard_api import _clean_dict, _clean_rows, _serialize, app

client = TestClient(app)


# ── Serialization helpers ────────────────────────────────────────────


class TestSerialize:
    """Tests for Decimal/date/datetime serialization."""

    def test_decimal_to_float(self) -> None:
        assert _serialize(Decimal("1.2345")) == 1.2345

    def test_date_to_iso(self) -> None:
        assert _serialize(date(2026, 3, 12)) == "2026-03-12"

    def test_datetime_to_iso(self) -> None:
        dt = datetime(2026, 3, 12, 10, 30, 0, tzinfo=timezone.utc)
        assert "2026-03-12" in _serialize(dt)

    def test_passthrough(self) -> None:
        assert _serialize(42) == 42
        assert _serialize("hello") == "hello"
        assert _serialize(None) is None

    def test_clean_rows(self) -> None:
        rows = [{"a": Decimal("1.5"), "b": "x"}, {"a": Decimal("2.0"), "b": "y"}]
        result = _clean_rows(rows)
        assert result == [{"a": 1.5, "b": "x"}, {"a": 2.0, "b": "y"}]

    def test_clean_dict(self) -> None:
        d = {"x": Decimal("0.75"), "y": date(2026, 1, 1)}
        result = _clean_dict(d)
        assert result["x"] == 0.75
        assert result["y"] == "2026-01-01"


# ── Dashboard HTML ───────────────────────────────────────────────────


class TestDashboardHTML:
    """Tests for the root HTML endpoint."""

    def test_serves_html(self) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Trade Alert Dashboard" in resp.text


# ── /api/summary ─────────────────────────────────────────────────────

_MOCK_SUMMARY = {
    "total_alerts": 150,
    "resolved": 100,
    "wins": 65,
    "losses": 30,
    "scratches": 5,
    "overall_winrate": Decimal("0.6500"),
    "avg_edge": Decimal("0.7800"),
    "avg_pnl": Decimal("0.0123"),
    "alerts_today": 12,
    "kpi_winrate_70": Decimal("0.7000"),
}


class TestSummaryEndpoint:
    """Tests for GET /api/summary."""

    @patch("dashboard_api.get_summary_stats", return_value=_MOCK_SUMMARY)
    def test_returns_summary(self, _mock: MagicMock) -> None:
        resp = client.get("/api/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_alerts"] == 150
        assert data["wins"] == 65
        assert data["kpi_winrate_70"] == 0.70

    @patch("dashboard_api.get_summary_stats", return_value={
        "total_alerts": 0, "resolved": 0, "wins": 0, "losses": 0,
        "scratches": 0, "overall_winrate": None, "avg_edge": None,
        "avg_pnl": None, "alerts_today": 0, "kpi_winrate_70": None,
    })
    def test_empty_db(self, _mock: MagicMock) -> None:
        resp = client.get("/api/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_alerts"] == 0
        assert data["overall_winrate"] is None


# ── /api/winrate ─────────────────────────────────────────────────────


class TestWinrateEndpoint:
    """Tests for GET /api/winrate."""

    @patch("dashboard_api.get_winrate_by_bucket", return_value=[
        {"bucket": Decimal("0.9"), "total": 10, "wins": 8, "avg_pnl": Decimal("0.05")},
        {"bucket": Decimal("0.8"), "total": 20, "wins": 14, "avg_pnl": Decimal("0.03")},
        {"bucket": Decimal("0.7"), "total": 30, "wins": 18, "avg_pnl": Decimal("0.01")},
    ])
    def test_returns_buckets(self, _mock: MagicMock) -> None:
        resp = client.get("/api/winrate")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        assert data[0]["bucket"] == 0.9
        assert data[0]["wins"] == 8

    @patch("dashboard_api.get_winrate_by_bucket", return_value=[])
    def test_empty(self, _mock: MagicMock) -> None:
        resp = client.get("/api/winrate")
        assert resp.status_code == 200
        assert resp.json() == []


# ── /api/frequency ───────────────────────────────────────────────────


class TestFrequencyEndpoint:
    """Tests for GET /api/frequency."""

    @patch("dashboard_api.get_alert_frequency", return_value=[
        {"date": date(2026, 3, 10), "total": 15, "longs": 8, "shorts": 5, "watches": 2},
        {"date": date(2026, 3, 11), "total": 12, "longs": 6, "shorts": 4, "watches": 2},
    ])
    def test_returns_days(self, _mock: MagicMock) -> None:
        resp = client.get("/api/frequency?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["total"] == 15
        _mock.assert_called_once_with(7)

    @patch("dashboard_api.get_alert_frequency", return_value=[])
    def test_default_days(self, _mock: MagicMock) -> None:
        resp = client.get("/api/frequency")
        assert resp.status_code == 200
        _mock.assert_called_once_with(30)

    def test_invalid_days(self) -> None:
        resp = client.get("/api/frequency?days=0")
        assert resp.status_code == 422

    def test_days_too_large(self) -> None:
        resp = client.get("/api/frequency?days=999")
        assert resp.status_code == 422


# ── /api/symbols ─────────────────────────────────────────────────────


class TestSymbolsEndpoint:
    """Tests for GET /api/symbols."""

    @patch("dashboard_api.get_symbol_performance", return_value=[
        {"symbol": "AAPL", "total": 25, "wins": 15, "losses": 8,
         "winrate": Decimal("0.6522"), "avg_edge": Decimal("0.78"), "avg_pnl": Decimal("0.02")},
        {"symbol": "NVDA", "total": 20, "wins": 14, "losses": 5,
         "winrate": Decimal("0.7368"), "avg_edge": Decimal("0.82"), "avg_pnl": Decimal("0.04")},
    ])
    def test_returns_symbols(self, _mock: MagicMock) -> None:
        resp = client.get("/api/symbols")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["symbol"] == "AAPL"
        assert isinstance(data[0]["winrate"], float)

    @patch("dashboard_api.get_symbol_performance", return_value=[])
    def test_custom_limit(self, _mock: MagicMock) -> None:
        resp = client.get("/api/symbols?limit=5")
        assert resp.status_code == 200
        _mock.assert_called_once_with(5)


# ── /api/alerts ──────────────────────────────────────────────────────


class TestAlertsEndpoint:
    """Tests for GET /api/alerts."""

    @patch("dashboard_api.get_recent_alerts", return_value=[
        {
            "id": 1, "symbol": "AAPL", "direction": "LONG",
            "edge_probability": Decimal("0.75"), "confidence": Decimal("0.80"),
            "timeframe": "15m", "thesis": "BB squeeze.",
            "entry": {"level": 185.0, "stop": 182.0, "target": 192.0},
            "created_at": datetime(2026, 3, 12, 10, 0, 0, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 3, 12, 10, 0, 0, tzinfo=timezone.utc),
            "outcome": "WIN", "outcome_pnl": Decimal("0.0378"),
            "timeframe_rationale": "15m trend.", "sentiment_context": "Bullish.",
            "unusual_activity": ["IV spike"], "macro_regime": "Risk-on.",
            "sources_agree": 4, "raw_snapshots": None,
        },
    ])
    def test_returns_alerts(self, _mock: MagicMock) -> None:
        resp = client.get("/api/alerts")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["symbol"] == "AAPL"
        assert data[0]["outcome"] == "WIN"
        # Decimal serialized to float
        assert isinstance(data[0]["edge_probability"], float)

    @patch("dashboard_api.get_recent_alerts", return_value=[])
    def test_empty(self, _mock: MagicMock) -> None:
        resp = client.get("/api/alerts")
        assert resp.status_code == 200
        assert resp.json() == []

    @patch("dashboard_api.get_recent_alerts", return_value=[])
    def test_custom_limit(self, _mock: MagicMock) -> None:
        resp = client.get("/api/alerts?limit=10")
        assert resp.status_code == 200
        _mock.assert_called_once_with(10)

    def test_limit_too_large(self) -> None:
        resp = client.get("/api/alerts?limit=1000")
        assert resp.status_code == 422
