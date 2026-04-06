"""End-to-end integration tests for the trade-alert pipeline.

Covers three integration paths with all external dependencies mocked:
1. Full pipeline: merger → decision → validate_and_filter → notifier
2. Outcome tracker: open alert → price check → WIN/LOSS/EXPIRED
3. Dashboard API: insert sample data → hit endpoints → verify schemas
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("redis", reason="redis not installed")
pytest.importorskip("slowapi", reason="slowapi not installed")

from models import PlaybookAlert

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_alert_dict(**overrides: object) -> dict:
    """Build a valid PlaybookAlert dict with sensible defaults."""
    base = {
        "symbol": "AAPL",
        "direction": "LONG",
        "edge_probability": 0.82,
        "confidence": 0.85,
        "timeframe": "15m",
        "thesis": "Bollinger squeeze with 2.8x volume and options sweep.",
        "entry": {"level": 185.0, "stop": 182.0, "target": 195.0},
        "timeframe_rationale": "15m breakout with 1h confirmation.",
        "sentiment_context": "Retail bullish, institutional neutral.",
        "unusual_activity": ["IV spike 2x avg"],
        "macro_regime": "Risk-on, VIX 14.",
        "sources_agree": 4,
    }
    base.update(overrides)
    return base


def _make_snapshot(symbol: str, signal_types: list[str]) -> dict:
    """Build a snapshot dict with the given signal types."""
    return {
        "symbol": symbol,
        "timeframe": "15m",
        "timestamp": "2026-04-06T14:00:00Z",
        "signals": [
            {
                "source": "test",
                "type": st,
                "score": 1.5,
                "confidence": 0.8,
                "reason": f"Test {st}",
            }
            for st in signal_types
        ],
    }


# ── 1. Full pipeline path ───────────────────────────────────────────────


class TestFullPipelinePath:
    """Merger → validate_and_filter → notifier end-to-end."""

    def test_valid_alert_flows_through_pipeline(self) -> None:
        """A well-formed alert with sufficient sources passes all gates
        and is persisted + sent to Discord."""
        from notifier_and_logger import notify
        from validate_and_filter import validate_and_filter

        snapshots = [
            _make_snapshot(
                "AAPL",
                ["technical_trend", "volume_spike", "sentiment_bull", "options_flow"],
            ),
        ]
        alert = _make_alert_dict(sources_agree=4, edge_probability=0.82)

        # Phase 1: validate_and_filter
        results, _trace = validate_and_filter(
            llm_response=json.dumps([alert]),
            snapshots_json=json.dumps(snapshots),
            macro={"risk_on": True},
            vix=14.0,
            timeframe="15m",
        )
        assert len(results) == 1
        validated = results[0]
        assert isinstance(validated, PlaybookAlert)
        assert validated.symbol == "AAPL"
        assert validated.direction == "LONG"

        # Phase 2: notifier (mock DB + Discord + Redis dedup + chart gen)
        with (
            patch("notifier_and_logger._is_duplicate_alert", return_value=False),
            patch("notifier_and_logger.format_embed", return_value={"embeds": [{}]}),
            patch("notifier_and_logger.generate_chart", return_value=None),
            patch("notifier_and_logger.insert_alert") as mock_insert,
            patch("notifier_and_logger.send_discord_embed", return_value=True),
        ):
            sent = notify(json.dumps([validated.model_dump()]))

        assert sent == 1
        mock_insert.assert_called_once()

    def test_weak_alert_rejected_by_gates(self) -> None:
        """A low-EP / low-source alert is rejected before notification."""
        from validate_and_filter import validate_and_filter

        snapshots = [_make_snapshot("AAPL", ["technical_trend"])]
        alert = _make_alert_dict(
            edge_probability=0.50,
            confidence=0.40,
            sources_agree=1,
        )

        results, _ = validate_and_filter(
            llm_response=json.dumps([alert]),
            snapshots_json=json.dumps(snapshots),
            macro={"risk_on": True},
            vix=14.0,
            timeframe="15m",
        )
        assert len(results) == 0

    def test_hallucinated_sources_blocked(self) -> None:
        """LLM claims 6 sources, snapshot has 2 → rejected."""
        from validate_and_filter import validate_and_filter

        snapshots = [_make_snapshot("AAPL", ["technical_trend", "volume_spike"])]
        alert = _make_alert_dict(sources_agree=6)

        results, _ = validate_and_filter(
            llm_response=json.dumps([alert]),
            snapshots_json=json.dumps(snapshots),
            macro={"risk_on": True},
            vix=14.0,
            timeframe="15m",
        )
        assert len(results) == 0

    def test_multiple_alerts_independently_gated(self) -> None:
        """Two alerts submitted — one strong (passes), one weak (rejected)."""
        from validate_and_filter import validate_and_filter

        snapshots = [
            _make_snapshot(
                "AAPL",
                ["technical_trend", "volume_spike", "sentiment_bull", "options_flow"],
            ),
            _make_snapshot("SPY", ["technical_trend"]),
        ]
        strong = _make_alert_dict(symbol="AAPL", sources_agree=4, edge_probability=0.82)
        weak = _make_alert_dict(
            symbol="SPY",
            sources_agree=1,
            edge_probability=0.45,
            confidence=0.40,
        )

        results, _ = validate_and_filter(
            llm_response=json.dumps([strong, weak]),
            snapshots_json=json.dumps(snapshots),
            macro={"risk_on": True},
            vix=14.0,
            timeframe="15m",
        )
        assert len(results) == 1
        assert results[0].symbol == "AAPL"


# ── 2. Outcome tracker round-trip ────────────────────────────────────────


class TestOutcomeTrackerRoundTrip:
    """Insert alert → mock price → verify WIN/LOSS/EXPIRED resolution."""

    @patch("outcome_tracker._is_market_open", return_value=True)
    @patch("outcome_tracker.update_outcome")
    @patch("outcome_tracker.get_current_price")
    @patch("outcome_tracker.get_open_alerts")
    def test_long_hitting_target_resolves_win(
        self,
        mock_alerts: MagicMock,
        mock_price: MagicMock,
        mock_update: MagicMock,
        _mkt: MagicMock,
    ) -> None:
        """LONG alert where price reaches target → WIN with positive pnl_pct."""
        from outcome_tracker import run_tracker_cycle

        mock_alerts.return_value = [
            {
                "id": 100,
                "symbol": "NVDA",
                "direction": "LONG",
                "entry": {"level": 800.0, "stop": 780.0, "target": 850.0},
                "created_at": datetime.now(timezone.utc) - timedelta(hours=1),
                "outcome": None,
                "timeframe": "15m",
            },
        ]
        mock_price.return_value = 855.0  # Above target

        resolved = run_tracker_cycle()
        assert resolved == 1
        mock_update.assert_called_once()
        args = mock_update.call_args
        assert args[0][0] == 100  # alert_id
        assert args[0][1] == "WIN"  # outcome
        assert args[0][2] > 0  # pnl positive
        assert args[1]["pnl_pct"] > 0  # pnl_pct positive

    @patch("outcome_tracker._is_market_open", return_value=True)
    @patch("outcome_tracker.update_outcome")
    @patch("outcome_tracker.get_current_price")
    @patch("outcome_tracker.get_open_alerts")
    def test_short_hitting_stop_resolves_loss(
        self,
        mock_alerts: MagicMock,
        mock_price: MagicMock,
        mock_update: MagicMock,
        _mkt: MagicMock,
    ) -> None:
        """SHORT alert where price rises to stop → LOSS."""
        from outcome_tracker import run_tracker_cycle

        mock_alerts.return_value = [
            {
                "id": 101,
                "symbol": "TSLA",
                "direction": "SHORT",
                "entry": {"level": 250.0, "stop": 260.0, "target": 230.0},
                "created_at": datetime.now(timezone.utc) - timedelta(hours=1),
                "outcome": None,
                "timeframe": "1h",
            },
        ]
        mock_price.return_value = 262.0  # Above stop

        resolved = run_tracker_cycle()
        assert resolved == 1
        args = mock_update.call_args
        assert args[0][1] == "LOSS"
        assert args[0][2] < 0  # negative pnl

    @patch("outcome_tracker._is_market_open", return_value=True)
    @patch("outcome_tracker.update_outcome")
    @patch("outcome_tracker.get_current_price")
    @patch("outcome_tracker.get_open_alerts")
    def test_expired_alert_resolved_as_expired(
        self,
        mock_alerts: MagicMock,
        mock_price: MagicMock,
        mock_update: MagicMock,
        _mkt: MagicMock,
    ) -> None:
        """Alert past expiry window → EXPIRED (not SCRATCH)."""
        from outcome_tracker import run_tracker_cycle

        mock_alerts.return_value = [
            {
                "id": 102,
                "symbol": "META",
                "direction": "LONG",
                "entry": {"level": 500.0, "stop": 490.0, "target": 520.0},
                "created_at": datetime.now(timezone.utc) - timedelta(hours=8),
                "outcome": None,
                "timeframe": "15m",
            },
        ]
        mock_price.return_value = 505.0  # In between — no WIN/LOSS

        resolved = run_tracker_cycle()
        assert resolved == 1
        args = mock_update.call_args
        assert args[0][1] == "EXPIRED"

    @patch("outcome_tracker._is_market_open", return_value=False)
    def test_market_closed_skips_cycle(self, _mkt: MagicMock) -> None:
        """When market is closed, no alerts are processed."""
        from outcome_tracker import run_tracker_cycle

        assert run_tracker_cycle() == 0


# ── 3. Dashboard API endpoint schemas ───────────────────────────────────


class TestDashboardAPISchemas:
    """Test dashboard API endpoints return correct response schemas."""

    @pytest.fixture()
    def client(self) -> TestClient:
        """Create a test client with mocked DB functions."""
        from dashboard_api import app

        return TestClient(app)

    @patch("dashboard_api.get_summary_stats")
    def test_summary_endpoint(self, mock_stats: MagicMock, client: TestClient) -> None:
        """GET /api/summary returns expected keys."""
        mock_stats.return_value = {
            "total_alerts": 100,
            "resolved": 60,
            "wins": 40,
            "losses": 15,
            "scratches": 5,
            "overall_winrate": 0.667,
            "avg_edge": 0.78,
            "avg_pnl": 2.50,
            "alerts_today": 3,
            "kpi_winrate_70": 0.72,
        }
        resp = client.get("/api/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_alerts" in data
        assert "wins" in data
        assert "overall_winrate" in data
        assert data["total_alerts"] == 100

    @patch("dashboard_api.get_winrate_by_bucket")
    def test_winrate_endpoint(self, mock_wr: MagicMock, client: TestClient) -> None:
        """GET /api/winrate returns a list of bucket dicts."""
        mock_wr.return_value = [
            {"bucket": 0.7, "total": 20, "wins": 14, "avg_pnl": 3.1},
            {"bucket": 0.8, "total": 15, "wins": 12, "avg_pnl": 4.5},
        ]
        resp = client.get("/api/winrate")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["bucket"] == 0.7

    @patch("dashboard_api.get_alert_frequency")
    def test_frequency_endpoint(self, mock_freq: MagicMock, client: TestClient) -> None:
        """GET /api/frequency returns daily counts."""
        mock_freq.return_value = [
            {"date": "2026-04-05", "total": 5, "longs": 3, "shorts": 1, "watches": 1},
        ]
        resp = client.get("/api/frequency?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data[0]["date"] == "2026-04-05"

    @patch("dashboard_api.get_symbol_performance")
    def test_symbols_endpoint(self, mock_sym: MagicMock, client: TestClient) -> None:
        """GET /api/symbols returns per-symbol stats."""
        mock_sym.return_value = [
            {
                "symbol": "AAPL",
                "total": 10,
                "wins": 7,
                "losses": 2,
                "winrate": 0.778,
                "avg_edge": 0.80,
                "avg_pnl": 3.2,
            },
        ]
        resp = client.get("/api/symbols?limit=5")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["symbol"] == "AAPL"
        assert "winrate" in data[0]

    @patch("dashboard_api.get_recent_alerts")
    def test_alerts_endpoint(self, mock_alerts: MagicMock, client: TestClient) -> None:
        """GET /api/alerts returns a list of alert dicts."""
        mock_alerts.return_value = [
            {
                "id": 1,
                "symbol": "NVDA",
                "direction": "LONG",
                "outcome": "WIN",
                "created_at": "2026-04-05T12:00:00",
            },
        ]
        resp = client.get("/api/alerts?limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data[0]["symbol"] == "NVDA"

    @patch("dashboard_api.get_summary_stats")
    def test_auth_rejects_bad_key(self, mock_stats: MagicMock, client: TestClient) -> None:
        """When DASHBOARD_API_KEY is set, wrong key returns 401."""
        mock_stats.return_value = {}
        with patch("dashboard_api.DASHBOARD_API_KEY", "correct-key"):
            resp = client.get("/api/summary", headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401

    @patch("dashboard_api.get_summary_stats")
    def test_auth_accepts_valid_key(self, mock_stats: MagicMock, client: TestClient) -> None:
        """When DASHBOARD_API_KEY is set, correct key returns 200."""
        mock_stats.return_value = {
            "total_alerts": 0,
            "resolved": 0,
            "wins": 0,
            "losses": 0,
            "scratches": 0,
            "overall_winrate": None,
            "avg_edge": None,
            "avg_pnl": None,
            "alerts_today": 0,
            "kpi_winrate_70": None,
        }
        with patch("dashboard_api.DASHBOARD_API_KEY", "correct-key"):
            resp = client.get("/api/summary", headers={"X-API-Key": "correct-key"})
        assert resp.status_code == 200
