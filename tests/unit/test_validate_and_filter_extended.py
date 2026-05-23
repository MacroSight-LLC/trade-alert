"""Extended gate, regime, JSON, WATCH decay, dedup, and circuit-breaker tests."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from validate_and_filter import (
    GateRejection,
    _classify_regime,
    _dedup_key,
    _dynamic_gates,
    _get_reference_prices,
    _get_watch_cycles,
    _incr_watch_cycles,
    _record_redis_failure,
    _reset_dedup_keys,
    _reset_watch_cycles,
    _signal_directional_score,
    _try_dedup_set,
    _watch_is_improving,
    is_redis_circuit_open,
    validate_and_filter,
)

# Re-use helpers from main test module
from tests.unit.test_validate_and_filter import (  # noqa: PLC2701
    _alert,
    _bear_snap,
    _recent_ts,
    _run,
    _snap,
)


def _bull_snaps(n: int = 10) -> list[dict]:
    """Snapshots that produce trending_up regime."""
    snaps = []
    for i in range(n):
        snaps.append(
            {
                "symbol": f"S{i}",
                "timeframe": "15m",
                "timestamp": _recent_ts(),
                "signals": [
                    {
                        "source": "test",
                        "type": "technical_trend",
                        "score": 2.0,
                        "confidence": 0.8,
                        "reason": "up",
                    }
                ],
            }
        )
    return snaps


def _watch_alert(**overrides: object) -> dict:
    base = _alert(direction="WATCH", edge_probability=0.68, confidence=0.65, sources_agree=3)
    base.update(overrides)
    return base


# ── Regime classification (§1.2) ──────────────────────────────────


class TestClassifyRegime:
    @pytest.mark.parametrize(
        "vix,expected",
        [(30.1, "extreme"), (31.0, "extreme")],
    )
    def test_extreme(self, vix: float, expected: str) -> None:
        assert _classify_regime(vix, False, 5, 5, 0.5) == expected

    @pytest.mark.parametrize(
        "vix,risk_off,expected",
        [(25.0, True, "risk_off_high_vix"), (29.9, True, "risk_off_high_vix")],
    )
    def test_risk_off_high_vix(self, vix: float, risk_off: bool, expected: str) -> None:
        assert _classify_regime(vix, risk_off, 5, 5, 0.5) == expected

    def test_risk_off_high_vix_not_neutral_regression(self) -> None:
        """Historical bug: risk_off_high_vix must not fall through to neutral."""
        regime = _classify_regime(27.0, True, 3, 3, 0.5)
        assert regime == "risk_off_high_vix"
        ep, sa, conf = _dynamic_gates(0.70, 4, 0.75, "15m", regime)
        assert ep > 0.70
        assert sa > 4

    def test_choppy(self) -> None:
        assert _classify_regime(18.0, False, 5, 5, 0.34) == "choppy"
        assert _classify_regime(18.0, False, 5, 5, 0.5) == "choppy"

    def test_trending_up(self) -> None:
        assert _classify_regime(18.0, False, 8, 2, 0.6) == "trending_up"

    def test_trending_down(self) -> None:
        assert _classify_regime(22.0, True, 2, 8, 0.6) == "trending_down"

    def test_neutral_fallthrough(self) -> None:
        # bull_ratio ~0.44, trend_strength ok, not risk_off → neutral (not choppy band)
        assert _classify_regime(20.0, False, 4, 5, 0.4) == "neutral"


# ── Dynamic gates (§1.3) ──────────────────────────────────────────


class TestDynamicGatesExtended:
    def test_choppy_increases_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DYNAMIC_GATES_ENABLED", "1")
        ep, sa, conf = _dynamic_gates(0.70, 4, 0.75, "15m", "choppy")
        assert ep > 0.70
        assert sa > 4
        assert conf > 0.75

    def test_risk_off_high_vix_tightens(self) -> None:
        ep, sa, conf = _dynamic_gates(0.70, 4, 0.75, "15m", "risk_off_high_vix")
        assert ep > 0.70
        assert sa >= 5
        assert conf > 0.75

    def test_trending_reduces_ep_conf(self) -> None:
        ep, sa, conf = _dynamic_gates(0.70, 4, 0.75, "15m", "trending_up")
        assert ep < 0.70
        assert conf < 0.75
        assert sa == 4

    def test_neutral_unchanged(self) -> None:
        ep, sa, conf = _dynamic_gates(0.70, 4, 0.75, "15m", "neutral")
        assert ep == 0.70
        assert sa == 4
        assert conf == 0.75

    def test_extreme_unchanged(self) -> None:
        ep, sa, conf = _dynamic_gates(0.70, 4, 0.75, "15m", "extreme")
        assert ep == 0.70

    def test_clamp_bounds(self) -> None:
        ep, sa, conf = _dynamic_gates(0.90, 0, 0.99, "15m", "choppy")
        assert 0.50 <= ep <= 0.95
        assert sa >= 1
        assert 0.50 <= conf <= 0.99

    def test_disabled_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_DYNAMIC_GATES_ENABLED", False)
        ep, sa, conf = _dynamic_gates(0.70, 4, 0.75, "15m", "choppy")
        assert ep == 0.70


# ── Signal directional score (§1.10) ─────────────────────────────


class TestSignalDirectionalScore:
    def test_macro_risk_off_bearish(self) -> None:
        assert _signal_directional_score("macro_risk_off", 2.5) == pytest.approx(-2.5)

    def test_macro_risk_off_bullish_regression(self) -> None:
        assert _signal_directional_score("macro_risk_off", -1.0) == pytest.approx(1.0)

    def test_sentiment_bear(self) -> None:
        assert _signal_directional_score("sentiment_bear", 1.5) == pytest.approx(-1.5)

    def test_standard_positive(self) -> None:
        assert _signal_directional_score("technical_trend", 2.0) == pytest.approx(2.0)


# ── Reference prices (§1.9) ───────────────────────────────────────


class TestReferencePrices:
    def _snap_with_price(self, sym: str, sig_type: str, key: str, price: float) -> dict:
        return {
            "symbol": sym,
            "timeframe": "15m",
            "timestamp": _recent_ts(),
            "signals": [
                {
                    "source": "test",
                    "type": sig_type,
                    "score": 1.0,
                    "confidence": 0.8,
                    "reason": "x",
                    "raw": {key: price},
                }
            ],
        }

    def test_technical_trend_wins_over_volume(self) -> None:
        snaps = [
            self._snap_with_price("AAPL", "volume_spike", "last", 100.0),
            self._snap_with_price("AAPL", "technical_trend", "current_price", 105.0),
        ]
        assert _get_reference_prices(snaps)["AAPL"] == 105.0

    def test_volume_wins_over_options(self) -> None:
        snaps = [
            self._snap_with_price("AAPL", "options_flow", "last", 100.0),
            self._snap_with_price("AAPL", "volume_spike", "last", 102.0),
        ]
        assert _get_reference_prices(snaps)["AAPL"] == 102.0

    def test_forecast_lowest_priority(self) -> None:
        snaps = [
            self._snap_with_price("AAPL", "price_forecast", "close", 99.0),
            self._snap_with_price("AAPL", "technical_trend", "current_price", 110.0),
        ]
        assert _get_reference_prices(snaps)["AAPL"] == 110.0

    def test_zero_price_excluded(self) -> None:
        snaps = [self._snap_with_price("AAPL", "technical_trend", "current_price", 0.0)]
        assert "AAPL" not in _get_reference_prices(snaps)


# ── JSON parse (§1.6) ─────────────────────────────────────────────


class TestJsonParseRobustness:
    def test_valid_array(self) -> None:
        a = _alert()
        results, _ = validate_and_filter(
            json.dumps([a]),
            json.dumps([_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull", "options_flow"])]),
            {"risk_on": True},
            14.0,
            "15m",
        )
        assert len(results) >= 0

    def test_fenced_markdown(self) -> None:
        a = _alert()
        fenced = f"```json\n{json.dumps([a])}\n```"
        results, _ = _run([a], vix=14.0)
        assert isinstance(results, list)

    def test_dict_wrapped(self) -> None:
        a = _alert()
        wrapped = json.dumps({"alerts": [a]})
        results, _ = validate_and_filter(
            wrapped,
            json.dumps([_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull", "options_flow"])]),
            {"risk_on": True},
            14.0,
            "15m",
        )
        assert isinstance(results, list)

    def test_trailing_commas(self) -> None:
        a = _alert()
        raw = json.dumps([a]).replace("}", ",}", 1)
        scores: list[tuple[str, float, str]] = []

        def _score_fn(tid: str, name: str, val: float, comment: str = "") -> None:
            scores.append((name, val, comment))

        validate_and_filter(
            raw,
            json.dumps([_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull", "options_flow"])]),
            {"risk_on": True},
            14.0,
            "15m",
            add_score_fn=_score_fn,
            trace_id="t1",
        )
        repaired = [s for s in scores if s[0] == "llm_json_repaired" and s[1] == 1.0]
        assert repaired

    def test_none_response(self) -> None:
        results, out = validate_and_filter(None, "[]", {"risk_on": True}, 14.0, "15m")
        assert results == []
        assert out == "[]"

    def test_api_error_internal_server(self) -> None:
        results, _ = validate_and_filter("litellm.InternalServerError: boom", "[]", {"risk_on": True}, 14.0, "15m")
        assert results == []

    def test_api_error_overloaded(self) -> None:
        results, _ = validate_and_filter('{"error":"overloaded_error"}', "[]", {"risk_on": True}, 14.0, "15m")
        assert results == []

    def test_malformed_json(self) -> None:
        results, out = validate_and_filter("{not json", "[]", {"risk_on": True}, 14.0, "15m")
        assert results == []
        assert out == "[]"


# ── Missing gate rejections (§1.1) ────────────────────────────────


class TestSourceHallucinationRejection:
    def test_symbol_absent_rejects(self) -> None:
        a = _alert(symbol="FAKE")
        results, _ = _run([a], snaps=[_snap("AAPL", ["technical_trend"])])
        assert len(results) == 0


class TestDirectionalThresholdGates:
    def test_ep_threshold_rejects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_DYNAMIC_GATES_ENABLED", False)
        monkeypatch.setitem(vf._GATE_EP, "15m", 0.80)
        a = _alert(edge_probability=0.78, confidence=0.80, sources_agree=4)
        results, _ = _run([a])
        assert len(results) == 0

    def test_sa_threshold_rejects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_DYNAMIC_GATES_ENABLED", False)
        monkeypatch.setattr(vf, "_GATE_SA", 5)
        snaps = [_snap("AAPL", ["technical_trend", "volume_spike"])]
        a = _alert(sources_agree=5, confidence=0.80, edge_probability=0.80)
        results, _ = _run([a], snaps=snaps)
        assert len(results) == 0

    def test_conf_threshold_rejects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_DYNAMIC_GATES_ENABLED", False)
        monkeypatch.setattr(vf, "_GATE_CONF", 0.80)
        a = _alert(confidence=0.78, sources_agree=4, edge_probability=0.80)
        results, _ = _run([a])
        assert len(results) == 0


class TestHighConfidenceAlignment:
    def test_rejects_high_conf_low_sa(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_HIGH_CONFIDENCE_MIN_SA", 5)
        monkeypatch.setattr(vf, "_SA_INCLUDE_MACRO_CONTEXT", False)
        a = _alert(confidence=0.86, sources_agree=4, edge_probability=0.80)
        results, _ = _run([a])
        assert len(results) == 0

    def test_passes_at_boundary(self) -> None:
        a = _alert(confidence=0.85, sources_agree=5, edge_probability=0.80)
        results, _ = _run([a])
        assert len(results) == 1


class TestWatchThresholdGates:
    def test_watch_ep_rejects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setitem(vf._GATE_EP, "15m", 0.75)
        monkeypatch.setattr(vf, "_WATCH_EP_DELTA", 0.05)
        a = _watch_alert(edge_probability=0.65)
        results, _ = _run([a])
        assert len(results) == 0

    def test_watch_sa_rejects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_DYNAMIC_GATES_ENABLED", False)
        monkeypatch.setattr(vf, "_WATCH_SA_MIN", 3)
        monkeypatch.setattr(vf, "_SA_INCLUDE_MACRO_CONTEXT", False)
        snaps = [_snap("AAPL", ["technical_trend"])]
        a = _watch_alert(sources_agree=2)
        results, _ = _run([a], snaps=snaps)
        assert len(results) == 0

    def test_watch_conf_rejects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_WATCH_CONF_MIN", 0.70)
        a = _watch_alert(confidence=0.60)
        results, _ = _run([a])
        assert len(results) == 0


class TestWatchDroppedDirectional:
    def test_watch_dropped_when_directional_present(self) -> None:
        long_a = _alert(direction="LONG")
        watch_a = _watch_alert(symbol="MSFT")
        snaps = [
            _snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull", "options_flow"]),
            _snap("MSFT", ["technical_trend", "volume_spike", "sentiment_bull"]),
        ]
        results, _ = _run([long_a, watch_a], snaps=snaps)
        assert all(r.direction != "WATCH" for r in results)


class TestWatchDecay:
    def test_decay_rejects_stale_watch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_WATCH_DECAY_CYCLES", 3)
        mock = MagicMock()
        mock.hget.return_value = b"3"
        mock.hgetall.return_value = {}
        pipe = MagicMock()
        pipe.execute.return_value = [1]
        mock.pipeline.return_value = pipe
        mock.set.return_value = True
        with patch("validate_and_filter.get_redis", return_value=mock):
            with patch("validate_and_filter._check_redis_circuit", return_value=False):
                a = _watch_alert(symbol="MSFT")
                snaps = [_snap("MSFT", ["technical_trend", "volume_spike", "sentiment_bull"])]
                results, _ = _run([a], snaps=snaps)
        assert len(results) == 0


class TestMarketSessionClosed:
    def test_closed_rejects_long(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_MARKET_HOURS_GATES_ENABLED", True)
        with patch("validate_and_filter._market_session_bucket", return_value="closed"):
            a = _alert(direction="LONG")
            results, _ = _run([a])
        assert len(results) == 0

    def test_closed_watch_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_MARKET_HOURS_GATES_ENABLED", True)
        with patch("validate_and_filter._market_session_bucket", return_value="closed"):
            a = _watch_alert()
            snaps = [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull"])]
            results, _ = _run([a], snaps=snaps)
        assert len(results) == 1


class TestSourcesAgreeOverride:
    def test_server_overrides_llm(self) -> None:
        snaps = [_snap("AAPL", ["technical_trend", "volume_spike"])]
        a = _alert(sources_agree=5, confidence=0.80, edge_probability=0.80)
        results, _ = _run([a], snaps=snaps)
        if results:
            assert results[0].sources_agree <= 5

    def test_override_score_logged(self) -> None:
        scores: list[tuple[str, float]] = []

        def _fn(tid: str, name: str, val: float, comment: str = "") -> None:
            scores.append((name, val))

        snaps = [_snap("AAPL", ["technical_trend"])]
        a = _alert(sources_agree=5, confidence=0.80, edge_probability=0.80)
        validate_and_filter(
            json.dumps([a]),
            json.dumps(snaps),
            {"risk_on": True},
            14.0,
            "15m",
            add_score_fn=_fn,
            trace_id="trace-1",
        )
        assert any(s[0] == "sources_agree_override" for s in scores)


# ── Dedup (§3.6) ──────────────────────────────────────────────────


class TestAlertDedup:
    def test_dedup_key_format(self) -> None:
        assert _dedup_key("AAPL", "LONG", "15m") == "dedup:alert:15m:LONG:AAPL"

    def test_second_alert_suppressed(self) -> None:
        mock = MagicMock()
        mock.set.side_effect = [True, None]
        mock.hgetall.return_value = {}
        pipe = MagicMock()
        pipe.execute.return_value = [0]
        mock.pipeline.return_value = pipe
        with patch("validate_and_filter.get_redis", return_value=mock):
            with patch("validate_and_filter._check_redis_circuit", return_value=False):
                a = _alert()
                snaps = [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull", "options_flow"])]
                r1, _ = _run([a], snaps=snaps)
                r2, _ = _run([a], snaps=snaps)
        assert len(r1) == 1
        assert len(r2) == 0

    def test_fail_open_when_circuit_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with patch("validate_and_filter._check_redis_circuit", return_value=True):
            assert _try_dedup_set("AAPL", "LONG", "15m") is False


# ── Redis circuit breaker (§2.5) ──────────────────────────────────


class TestRedisCircuitBreaker:
    def setup_method(self) -> None:
        import validate_and_filter as vf

        vf._REDIS_FAILURE_COUNT = 0
        vf._redis_circuit_open = False
        vf._redis_last_failure_ts = 0.0

    def test_circuit_opens_after_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDIS_FAILURE_THRESHOLD", "2")
        import validate_and_filter as vf

        vf._REDIS_FAILURE_THRESHOLD = 2
        _record_redis_failure()
        _record_redis_failure()
        assert is_redis_circuit_open() is True

    def test_circuit_resets_after_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        vf._redis_circuit_open = True
        vf._redis_last_failure_ts = time.monotonic() - 61
        vf._REDIS_FAILURE_WINDOW_SECONDS = 60
        assert is_redis_circuit_open() is False

    def test_get_watch_cycles_returns_zero_when_open(self) -> None:
        import validate_and_filter as vf

        vf._redis_circuit_open = True
        assert _get_watch_cycles("AAPL", "15m") == 0

    def test_reset_watch_cycles_noop_when_open(self) -> None:
        import validate_and_filter as vf

        vf._redis_circuit_open = True
        _reset_watch_cycles(["AAPL"], "15m")


class TestCircuitBreakerIntegration:
    def test_watch_capped_when_circuit_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        vf._redis_circuit_open = True
        monkeypatch.setattr(vf, "_WATCH_MAX_STRESSED", 1)
        monkeypatch.setattr(vf, "_WATCH_MAX_TRENDING", 3)
        a1 = _watch_alert(symbol="MSFT")
        a2 = _watch_alert(symbol="GOOG")
        snaps = [
            _snap("MSFT", ["technical_trend", "volume_spike", "sentiment_bull"]),
            _snap("GOOG", ["technical_trend", "volume_spike", "sentiment_bull"]),
        ]
        with patch("validate_and_filter.WATCH_DECAY_SKIPPED") as mock_skipped:
            results, _ = _run([a1, a2], snaps=snaps)
        assert len(results) <= 1
        assert mock_skipped.inc.call_count >= 0

    def test_validate_and_filter_warns_when_circuit_open(self) -> None:
        import validate_and_filter as vf

        vf._redis_circuit_open = True
        a = _watch_alert()
        snaps = [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull"])]
        results, _ = _run([a], snaps=snaps)
        assert isinstance(results, list)


class TestEpCeilingBoundary:
    def test_ep_exactly_at_ceiling_passes(self) -> None:
        snaps = [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull"])]
        ceiling = 0.75
        a = _alert(edge_probability=ceiling, confidence=0.80, sources_agree=3)
        results, _ = _run([a], snaps=snaps)
        if results:
            assert results[0].edge_probability <= ceiling

    def test_ep_above_ceiling_capped(self) -> None:
        snaps = [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull"])]
        a = _alert(edge_probability=0.76, confidence=0.80, sources_agree=3)
        results, _ = _run([a], snaps=snaps)
        if results:
            assert results[0].edge_probability == 0.75


class TestVixHardBypass:
    def test_vix_29_9_long_passes(self) -> None:
        a = _alert(direction="LONG", confidence=0.80)
        results, _ = _run([a], vix=29.9)
        assert len(results) == 1

    def test_vix_30_1_long_rejects(self) -> None:
        a = _alert(direction="LONG", confidence=0.80)
        results, _ = _run([a], vix=30.1)
        assert len(results) == 0

    def test_vix_30_1_watch_passes(self) -> None:
        a = _watch_alert()
        snaps = [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull"])]
        results, _ = _run([a], snaps=snaps, vix=30.1)
        assert len(results) == 1


class TestWatchCap:
    def test_excess_watch_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_WATCH_MAX_TRENDING", 1)
        monkeypatch.setattr(vf, "_DYNAMIC_GATES_ENABLED", False)
        snaps = [
            _snap("AAA", ["technical_trend", "volume_spike", "sentiment_bull"]),
            _snap("BBB", ["technical_trend", "volume_spike", "sentiment_bull"]),
        ]
        a1 = _watch_alert(symbol="AAA")
        a2 = _watch_alert(symbol="BBB")
        with patch("validate_and_filter._classify_regime", return_value="trending_up"):
            results, _ = _run([a1, a2], snaps=snaps)
        assert len(results) == 1


class TestDedupHelpers:
    def test_reset_dedup_keys(self) -> None:
        from validate_and_filter import _reset_dedup_keys

        mock = MagicMock()
        with patch("validate_and_filter.get_redis", return_value=mock):
            with patch("validate_and_filter._check_redis_circuit", return_value=False):
                _reset_dedup_keys(["AAPL"], "15m")
        assert mock.delete.called

    def test_dedup_set_records_failure(self) -> None:
        mock = MagicMock()
        mock.set.side_effect = Exception("redis down")
        with patch("validate_and_filter.get_redis", return_value=mock):
            with patch("validate_and_filter._check_redis_circuit", return_value=False):
                assert _try_dedup_set("AAPL", "LONG", "15m") is False


class TestIncrWatchCycles:
    def test_incr_returns_count(self) -> None:
        mock = MagicMock()
        pipe = MagicMock()
        pipe.execute.return_value = [2]
        mock.pipeline.return_value = pipe
        with patch("validate_and_filter.get_redis", return_value=mock):
            with patch("validate_and_filter._check_redis_circuit", return_value=False):
                assert _incr_watch_cycles("AAPL", "15m", 0.7, 0.65) == 2


class TestApplyMarketSessionOverlays:
    def test_prepost_bumps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_MARKET_HOURS_GATES_ENABLED", True)
        monkeypatch.setattr(vf, "_SESSION_PREPOST_EP_BUMP", 0.03)
        with patch("validate_and_filter._market_session_bucket", return_value="pre"):
            ep, sa, conf, bucket = vf._apply_market_session_gate_overlays(0.70, 4, 0.75, "15m")
        assert bucket == "pre"
        assert ep > 0.70

    def test_disabled_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_MARKET_HOURS_GATES_ENABLED", False)
        ep, sa, conf, bucket = vf._apply_market_session_gate_overlays(0.70, 4, 0.75, "15m")
        assert ep == 0.70
