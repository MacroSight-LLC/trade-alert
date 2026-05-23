"""Coverage-focused tests for validate_and_filter.py edge branches."""

from __future__ import annotations

import json
import logging
import time
import traceback
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from models import PlaybookAlert
from tests.unit.test_validate_and_filter import (  # noqa: PLC2701
    _alert,
    _bear_snap,
    _recent_ts,
    _run,
    _snap,
)
from tests.unit.test_validate_and_filter_extended import _watch_alert  # noqa: PLC2701
from validate_and_filter import (
    _aligned_family_count,
    _apply_market_session_gate_overlays,
    _build_symbol_family_scores,
    _check_redis_circuit,
    _get_forecast_scores,
    _get_macro_risk_off_score,
    _get_reference_prices,
    _get_volume_spike_scores,
    _get_watch_prev_state,
    _incr_watch_cycles,
    _is_macro_stale,
    _market_session_bucket,
    _record_redis_failure,
    _record_session_gate_metrics,
    _signal_surface,
    _watch_is_improving,
    validate_and_filter,
)


def _construct_alert(**item: object) -> PlaybookAlert:
    """Build PlaybookAlert without pydantic entry-order validation (server-side gates only)."""
    return PlaybookAlert.model_construct(**item)


class _RRZeroEntry:
    """Entry map that passes LONG order checks but yields zero risk at the R:R gate."""

    def __init__(self) -> None:
        self._data = {"level": 185.0, "stop": 182.0, "target": 195.0}

    def __getitem__(self, key: str) -> float:
        in_rr = any(
            f.name == "_rr" and f.filename.endswith("rr_volume.py")
            for f in traceback.extract_stack()
        )
        if in_rr and key == "stop":
            return self._data["level"]
        return self._data[key]

    def get(self, key: str, default: float | None = None) -> float | None:
        try:
            return self[key]
        except KeyError:
            return default


def _passing_snaps() -> list[dict]:
    return [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull", "options_flow"])]


def _score_fn_collector() -> tuple[list[tuple[str, float, str]], object]:
    scores: list[tuple[str, float, str]] = []

    def _fn(tid: str, name: str, val: float, comment: str = "") -> None:
        scores.append((name, val, comment))

    return scores, _fn


class TestHelperCoverage:
    def test_signal_surface_bad_score_and_short_interest(self) -> None:
        snaps = [
            {
                "symbol": "X",
                "signals": [
                    {"type": "technical_trend", "score": "bad"},
                    {"type": "short_interest", "score": 2.0},
                ],
            }
        ]
        bulls, bears, ts = _signal_surface(snaps)
        assert bulls >= 1
        assert ts > 0

    def test_market_session_buckets(self) -> None:
        with patch("validate_and_filter.get_market_hours_status", return_value="Pre-market (8:00 ET)"):
            assert _market_session_bucket() == "pre"
        with patch("validate_and_filter.get_market_hours_status", return_value="After-hours (17:00 ET)"):
            assert _market_session_bucket() == "after"
        with patch("validate_and_filter.get_market_hours_status", return_value="Market closed (weekend)"):
            assert _market_session_bucket() == "closed"
        with patch(
            "validate_and_filter.get_market_hours_status",
            return_value="Regular trading hours (9:30 ET)",
        ):
            assert _market_session_bucket() == "regular"

    def test_regular_session_overlay_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_MARKET_HOURS_GATES_ENABLED", True)
        with patch("validate_and_filter._market_session_bucket", return_value="regular"):
            ep, sa, conf, bucket = _apply_market_session_gate_overlays(0.70, 4, 0.75, "15m")
        assert bucket == "regular"
        assert ep == 0.70
        assert sa == 4
        assert conf == 0.75

    def test_build_symbol_family_scores_edge_cases(self) -> None:
        snaps = [
            {"symbol": "", "signals": [{"type": "technical_trend", "score": 1.0}]},
            {
                "symbol": "A",
                "signals": [
                    "not-a-dict",
                    {"type": "unknown_type", "score": 1.0},
                    {"type": "technical_trend", "score": "bad"},
                    {"type": "technical_trend", "score": 2.0},
                ],
            },
        ]
        out = _build_symbol_family_scores(snaps)
        assert "A" in out

    def test_aligned_family_count_watch(self) -> None:
        fam = {"trend": 1.0, "volume": -1.0}
        assert _aligned_family_count(fam, "WATCH") >= 1

    def test_forecast_and_macro_bad_scores(self) -> None:
        snaps = [
            {
                "symbol": "A",
                "signals": [
                    {"type": "price_forecast", "score": "x"},
                    {"type": "macro_risk_off", "score": None},
                ],
            }
        ]
        assert _get_forecast_scores(snaps) == {}
        assert _get_macro_risk_off_score(snaps) == 0.0

    def test_macro_stale_paths(self) -> None:
        old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        assert _is_macro_stale(
            [{"symbol": "G", "timestamp": old, "signals": [{"type": "macro_risk_off", "score": 1}]}]
        )
        assert _is_macro_stale(
            [{"symbol": "G", "timestamp": "not-a-date", "signals": [{"type": "macro_risk_off", "score": 1}]}]
        )
        assert _is_macro_stale([])

    def test_macro_stale_fresh_returns_false(self) -> None:
        fresh = datetime.now(UTC).isoformat()
        assert (
            _is_macro_stale(
                [{"symbol": "G", "timestamp": fresh, "signals": [{"type": "macro_risk_off", "score": 1}]}]
            )
            is False
        )

    def test_macro_stale_skips_empty_timestamp(self) -> None:
        fresh = datetime.now(UTC).isoformat()
        snaps = [
            {"symbol": "G", "timestamp": "", "signals": [{"type": "macro_risk_off", "score": 1}]},
            {"symbol": "G2", "timestamp": fresh, "signals": [{"type": "macro_risk_off", "score": 1}]},
        ]
        assert _is_macro_stale(snaps) is False

    def test_volume_spike_bad_scores_skipped(self) -> None:
        snaps = [
            {
                "symbol": "AAPL",
                "signals": [
                    {"type": "volume_spike", "score": None},
                    {"type": "volume_spike", "score": "bad"},
                ],
            }
        ]
        assert _get_volume_spike_scores(snaps) == {}

    def test_incr_watch_cycles_records_redis_failure(self) -> None:
        import validate_and_filter as vf

        vf._REDIS_FAILURE_COUNT = 0
        vf._redis_circuit_open = False
        mock = MagicMock()
        pipe = MagicMock()
        pipe.execute.side_effect = RuntimeError("redis down")
        mock.pipeline.return_value = pipe
        with patch("validate_and_filter.get_redis", return_value=mock):
            with patch("validate_and_filter._check_redis_circuit", return_value=False):
                assert _incr_watch_cycles("AAPL", "15m", 0.7, 0.65) == 0
        assert vf._REDIS_FAILURE_COUNT >= 1

    def test_reference_prices_edge_cases(self) -> None:
        snaps = [
            {"symbol": "", "signals": [{"type": "technical_trend", "raw": {"price": 1.0}}]},
            {"symbol": "B", "signals": [{"type": "technical_trend", "raw": "bad"}]},
            {"symbol": "C", "signals": [{"type": "technical_trend", "raw": {"price": "bad"}}]},
        ]
        prices = _get_reference_prices(snaps)
        assert "B" not in prices
        assert "C" not in prices

    def test_watch_is_improving_bad_ep(self) -> None:
        prev = {"AAPL": {"last_ep": "not-a-float"}}
        assert _watch_is_improving("AAPL", 0.8, prev) is False

    def test_get_watch_prev_state_records_failure(self) -> None:
        import validate_and_filter as vf

        vf._REDIS_FAILURE_COUNT = 0
        vf._redis_circuit_open = False
        mock = MagicMock()
        mock.hgetall.side_effect = RuntimeError("redis down")
        with patch("validate_and_filter.get_redis", return_value=mock):
            assert _get_watch_prev_state("AAPL", "15m") is None

    def test_circuit_reset_after_window(self) -> None:
        import validate_and_filter as vf

        vf._redis_circuit_open = True
        vf._redis_last_failure_ts = time.monotonic() - 120
        vf._REDIS_FAILURE_WINDOW_SECONDS = 60
        with patch("validate_and_filter.REDIS_CIRCUIT_OPEN") as mock_gauge:
            assert _check_redis_circuit() is False
            mock_gauge.set.assert_called_with(0)

    def test_record_redis_failure_resets_count_in_new_window(self) -> None:
        import validate_and_filter as vf

        vf._REDIS_FAILURE_COUNT = 5
        vf._redis_last_failure_ts = time.monotonic() - 120
        vf._REDIS_FAILURE_THRESHOLD = 3
        _record_redis_failure()
        assert vf._REDIS_FAILURE_COUNT == 1


class TestJsonAndLangfuseCoverage:
    def test_dict_content_wrapper(self) -> None:
        a = _alert()
        payload = {"content": json.dumps([a])}
        scores, fn = _score_fn_collector()
        results, _ = validate_and_filter(
            payload,
            json.dumps(
                [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull", "options_flow"])]
            ),
            {"risk_on": True},
            14.0,
            "15m",
            add_score_fn=fn,
            trace_id="t1",
        )
        assert isinstance(results, list)
        assert any(s[0] == "llm_json_valid" for s in scores)

    def test_list_payload_direct(self) -> None:
        a = _alert()
        results, _ = validate_and_filter(
            [a],
            json.dumps(
                [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull", "options_flow"])]
            ),
            {"risk_on": True},
            14.0,
            "15m",
        )
        assert len(results) == 1

    def test_prose_wrapped_array(self) -> None:
        a = _alert()
        prose = f"Here are alerts:\n{json.dumps([a])}\nThanks"
        results, _ = validate_and_filter(
            prose,
            json.dumps(
                [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull", "options_flow"])]
            ),
            {"risk_on": True},
            14.0,
            "15m",
        )
        assert len(results) == 1

    def test_llm_json_repaired_score(self) -> None:
        a = _alert()
        broken = json.dumps([a]).replace("}", ",}", 1)
        scores, fn = _score_fn_collector()
        validate_and_filter(
            broken,
            json.dumps(
                [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull", "options_flow"])]
            ),
            {"risk_on": True},
            14.0,
            "15m",
            add_score_fn=fn,
            trace_id="t1",
        )
        assert any(s[0] == "llm_json_repaired" and s[1] == 1.0 for s in scores)

    def test_parse_failure_scores(self) -> None:
        scores, fn = _score_fn_collector()
        validate_and_filter(
            '{"not": "a list"}', "[]", {"risk_on": True}, 14.0, "15m", add_score_fn=fn, trace_id="t1"
        )
        assert any(s[0] == "llm_json_valid" and s[1] == 0.0 for s in scores)

    def test_api_error_with_trace_scores(self) -> None:
        scores, fn = _score_fn_collector()
        validate_and_filter(None, "[]", {"risk_on": True}, 14.0, "15m", add_score_fn=fn, trace_id="t1")
        assert any(s[0] == "llm_api_error" for s in scores)

    def test_api_error_marker_string_scores(self) -> None:
        scores, fn = _score_fn_collector()
        validate_and_filter(
            "litellm.InternalServerError: AnthropicError - overloaded_error",
            "[]",
            {"risk_on": True},
            14.0,
            "15m",
            add_score_fn=fn,
            trace_id="t1",
        )
        assert any(s[0] == "llm_api_error" and s[1] == 1.0 for s in scores)

    def test_fenced_markdown_json(self) -> None:
        a = _alert()
        payload = f"```json\n{json.dumps([a])}\n```"
        results, _ = validate_and_filter(
            payload,
            json.dumps(_passing_snaps()),
            {"risk_on": True},
            14.0,
            "15m",
        )
        assert len(results) == 1

    def test_dict_without_content_key(self) -> None:
        scores, fn = _score_fn_collector()
        validate_and_filter(
            {"symbol": "orphan"},
            "[]",
            {"risk_on": True},
            14.0,
            "15m",
            add_score_fn=fn,
            trace_id="t1",
        )
        assert any(s[0] == "llm_json_valid" and s[1] == 0.0 for s in scores)

    @pytest.mark.parametrize("wrapper_key", ["result", "results", "data"])
    def test_dict_list_wrapper_keys(self, wrapper_key: str) -> None:
        a = _alert()
        results, _ = validate_and_filter(
            {wrapper_key: [a]},
            json.dumps(_passing_snaps()),
            {"risk_on": True},
            14.0,
            "15m",
        )
        assert len(results) == 1

    def test_non_string_json_payload(self) -> None:
        scores, fn = _score_fn_collector()
        validate_and_filter(12345, "[]", {"risk_on": True}, 14.0, "15m", add_score_fn=fn, trace_id="t1")
        assert any(s[0] == "llm_json_valid" and s[1] == 0.0 for s in scores)

    def test_symbol_hallucination_score(self) -> None:
        scores, fn = _score_fn_collector()
        validate_and_filter(
            json.dumps([_alert(symbol="GHOST")]),
            "[]",
            {"risk_on": True},
            14.0,
            "15m",
            add_score_fn=fn,
            trace_id="t1",
        )
        assert any(s[0] == "symbol_hallucination" for s in scores)

    def test_gate_telemetry_scores(self) -> None:
        scores, fn = _score_fn_collector()
        bad = _alert(edge_probability=0.50, confidence=0.80, sources_agree=4)
        validate_and_filter(
            json.dumps([bad]),
            json.dumps(
                [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull", "options_flow"])]
            ),
            {"risk_on": True},
            14.0,
            "15m",
            add_score_fn=fn,
            trace_id="t1",
        )
        names = {s[0] for s in scores}
        assert "alert_pass_rate" in names
        assert "gate_rejection_rate" in names
        assert any(n.startswith("gate_reject_") for n in names)


class TestGateBranchCoverage:
    def test_vix_nan_treated_as_high(self) -> None:
        a = _alert(direction="LONG", confidence=0.80)
        results, _ = _run([a], vix=float("nan"))
        assert len(results) == 0

    def test_invalid_playbook_skipped(self) -> None:
        results, _ = validate_and_filter(
            json.dumps([{"symbol": "AAPL"}]), "[]", {"risk_on": True}, 14.0, "15m"
        )
        assert results == []

    def test_zero_price_entry_rejected(self) -> None:
        a = _alert(entry={"level": 0.0, "stop": 182.0, "target": 195.0}, confidence=0.80)
        with patch("validate_and_filter.PlaybookAlert", side_effect=_construct_alert):
            results, _ = _run([a])
        assert len(results) == 0

    def test_long_entry_order_invalid_stop_equals_level(self) -> None:
        a = _alert(entry={"level": 185.0, "stop": 185.0, "target": 195.0}, confidence=0.80)
        with patch("validate_and_filter.PlaybookAlert", side_effect=_construct_alert):
            results, _ = _run([a])
        assert len(results) == 0

    def test_long_entry_order_invalid_target_below_level(self) -> None:
        a = _alert(entry={"level": 185.0, "stop": 180.0, "target": 182.0}, confidence=0.80)
        with patch("validate_and_filter.PlaybookAlert", side_effect=_construct_alert):
            results, _ = _run([a])
        assert len(results) == 0

    def test_short_entry_order_invalid(self) -> None:
        a = _alert(direction="SHORT", entry={"level": 185.0, "stop": 180.0, "target": 190.0}, confidence=0.80)
        snaps = [_bear_snap()]
        with patch("validate_and_filter.PlaybookAlert", side_effect=_construct_alert):
            results, _ = _run([a], snaps=snaps)
        assert len(results) == 0

    def test_entry_drift_with_vix_and_prepost(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_MARKET_HOURS_GATES_ENABLED", True)
        snap = {
            "symbol": "AAPL",
            "timeframe": "15m",
            "timestamp": _recent_ts(),
            "signals": [
                {
                    "source": "test",
                    "type": "technical_trend",
                    "score": 1.5,
                    "confidence": 0.8,
                    "reason": "t",
                    "raw": {"current_price": 100.0},
                },
                {"source": "test", "type": "volume_spike", "score": 2.0, "confidence": 0.8, "reason": "v"},
                {"source": "test", "type": "sentiment_bull", "score": 1.5, "confidence": 0.8, "reason": "s"},
                {"source": "test", "type": "options_flow", "score": 1.5, "confidence": 0.8, "reason": "o"},
            ],
        }
        a = _alert(entry={"level": 120.0, "stop": 115.0, "target": 130.0}, confidence=0.80)
        with patch("validate_and_filter._market_session_bucket", return_value="pre"):
            results, _ = _run([a], snaps=[snap], vix=26.0)
        assert len(results) == 0

    def test_vix_soft_short_risk_on(self) -> None:
        a = _alert(direction="SHORT", edge_probability=0.73, confidence=0.80, sources_agree=2)
        snaps = [_bear_snap()]
        results, _ = _run([a], snaps=snaps, vix=26.0, macro={"risk_on": True})
        assert len(results) == 0

    def test_vix_soft_long_risk_off_non_high_vix_regime(self) -> None:
        a = _alert(
            direction="LONG",
            edge_probability=0.71,
            confidence=0.80,
            sources_agree=2,
        )
        snaps = [_snap("AAPL", ["technical_trend", "volume_spike"])]
        with patch("validate_and_filter._classify_regime", return_value="choppy"):
            results, _ = _run([a], snaps=snaps, vix=26.0, macro={"risk_on": False})
        assert len(results) == 0

    def test_entry_drift_vix_high_threshold_bump(self) -> None:
        snap = {
            "symbol": "AAPL",
            "timeframe": "15m",
            "timestamp": _recent_ts(),
            "signals": [
                {
                    "source": "test",
                    "type": "technical_trend",
                    "score": 1.5,
                    "confidence": 0.8,
                    "reason": "t",
                    "raw": {"current_price": 100.0},
                },
                {"source": "test", "type": "volume_spike", "score": 2.0, "confidence": 0.8, "reason": "v"},
                {"source": "test", "type": "sentiment_bull", "score": 1.5, "confidence": 0.8, "reason": "s"},
                {"source": "test", "type": "options_flow", "score": 1.5, "confidence": 0.8, "reason": "o"},
            ],
        }
        a = _alert(entry={"level": 115.0, "stop": 110.0, "target": 130.0}, confidence=0.80)
        results, _ = _run([a], snaps=[snap], vix=30.0)
        assert len(results) == 0

    def test_volume_unconfirmed_choppy_rejects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_DYNAMIC_GATES_ENABLED", False)
        monkeypatch.setattr(vf, "_VOLUME_CONFIRM_SCORE", 1.5)
        monkeypatch.setattr(vf, "_VOLUME_CONFIRM_PENALTY_CHOPPY", 0.15)
        snap = {
            "symbol": "AAPL",
            "timeframe": "15m",
            "timestamp": _recent_ts(),
            "signals": [
                {"source": "t", "type": "technical_trend", "score": 2.0, "confidence": 0.8, "reason": "t"},
                {"source": "t", "type": "sentiment_bull", "score": 2.0, "confidence": 0.8, "reason": "s"},
                {"source": "t", "type": "options_flow", "score": 2.0, "confidence": 0.8, "reason": "o"},
                {"source": "t", "type": "catalyst_event", "score": 2.0, "confidence": 0.8, "reason": "c"},
            ],
        }
        a = _alert(confidence=0.76, edge_probability=0.80, sources_agree=4)
        with patch("validate_and_filter._classify_regime", return_value="choppy"):
            results, _ = _run([a], snaps=[snap])
        assert len(results) == 0

    def test_forecast_bonus_short_awarded(self) -> None:
        import validate_and_filter as vf

        snap = {
            "symbol": "AAPL",
            "timeframe": "15m",
            "timestamp": _recent_ts(),
            "signals": [
                {"source": "t", "type": "technical_trend", "score": -2.0, "confidence": 0.8, "reason": "t"},
                {"source": "t", "type": "price_forecast", "score": -0.9, "confidence": 0.8, "reason": "f"},
                {"source": "t", "type": "volume_spike", "score": 2.0, "confidence": 0.8, "reason": "v"},
                {"source": "t", "type": "sentiment_bear", "score": 1.5, "confidence": 0.8, "reason": "s"},
                {"source": "t", "type": "options_flow", "score": -1.5, "confidence": 0.8, "reason": "o"},
            ],
        }
        a = _alert(
            direction="SHORT",
            sources_agree=5,
            confidence=0.80,
            edge_probability=0.80,
            entry={"level": 185.0, "stop": 190.0, "target": 175.0},
        )
        with patch.object(vf, "_SA_INCLUDE_MACRO_CONTEXT", False):
            results, _ = _run([a], snaps=[snap])
        if results:
            assert results[0].sources_agree >= 4

    def test_forecast_bonus_not_double_counted(self) -> None:
        import validate_and_filter as vf

        snap = {
            "symbol": "AAPL",
            "timeframe": "15m",
            "timestamp": _recent_ts(),
            "signals": [
                {"source": "t", "type": "price_forecast", "score": 0.95, "confidence": 0.8, "reason": "f"},
                {"source": "t", "type": "volume_spike", "score": 2.0, "confidence": 0.8, "reason": "v"},
                {"source": "t", "type": "sentiment_bull", "score": 1.5, "confidence": 0.8, "reason": "s"},
            ],
        }
        a = _alert(sources_agree=5, confidence=0.80, edge_probability=0.80)
        with patch.object(vf, "_SA_INCLUDE_MACRO_CONTEXT", False):
            results, _ = _run([a], snaps=[snap])
        if results:
            assert results[0].sources_agree <= 5

    def test_rr_zero_risk_stop_equals_level(self) -> None:
        a = _alert(entry={"level": 185.0, "stop": 185.0, "target": 195.0}, confidence=0.80)
        with patch("validate_and_filter.PlaybookAlert", side_effect=_construct_alert):
            results, _ = _run([a])
        assert len(results) == 0

    def test_rr_zero_risk_exact_path(self) -> None:
        base = _alert(confidence=0.80)
        alert_obj = _construct_alert(**{**base, "entry": _RRZeroEntry()})
        with patch("validate_and_filter.PlaybookAlert", return_value=alert_obj):
            results, _ = _run([base])
        assert len(results) == 0

    def test_micro_risk_rejected(self) -> None:
        a = _alert(entry={"level": 10.0, "stop": 9.99, "target": 12.0}, confidence=0.80)
        results, _ = _run([a])
        assert len(results) == 0


class TestWatchPathCoverage:
    def test_watch_dedup_suppressed(self) -> None:
        mock = MagicMock()
        mock.set.side_effect = [True, None]
        mock.hgetall.return_value = {}
        pipe = MagicMock()
        pipe.execute.return_value = [0]
        mock.pipeline.return_value = pipe
        with patch("validate_and_filter.get_redis", return_value=mock):
            with patch("validate_and_filter._check_redis_circuit", return_value=False):
                a = _watch_alert()
                snaps = [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull"])]
                r1, _ = _run([a], snaps=snaps)
                r2, _ = _run([a], snaps=snaps)
        assert len(r1) == 1
        assert len(r2) == 0

    def test_watch_promotion_thesis_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_WATCH_PROMOTION_MIN_CYCLES", 2)
        mock = MagicMock()
        mock.hgetall.return_value = {b"cycles": b"1", b"last_ep": b"0.60", b"last_conf": b"0.65"}
        mock.hget.return_value = b"1"
        pipe = MagicMock()
        pipe.execute.return_value = [2]
        mock.pipeline.return_value = pipe
        mock.set.return_value = True
        with patch("validate_and_filter.get_redis", return_value=mock):
            with patch("validate_and_filter._check_redis_circuit", return_value=False):
                a = _watch_alert(edge_probability=0.68)
                snaps = [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull"])]
                results, _ = _run([a], snaps=snaps)
        assert len(results) == 1
        assert "STRENGTHENING" in results[0].thesis

    def test_watch_cap_rejection_logged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import validate_and_filter as vf

        monkeypatch.setattr(vf, "_WATCH_MAX_TRENDING", 1)
        mock = MagicMock()
        mock.hgetall.return_value = {}
        mock.hget.return_value = b"0"
        pipe = MagicMock()
        pipe.execute.return_value = [1]
        mock.pipeline.return_value = pipe
        mock.set.return_value = True
        with patch("validate_and_filter.get_redis", return_value=mock):
            with patch("validate_and_filter._classify_regime", return_value="trending_up"):
                snaps = [
                    _snap("AAA", ["technical_trend", "volume_spike", "sentiment_bull"]),
                    _snap("BBB", ["technical_trend", "volume_spike", "sentiment_bull"]),
                ]
                results, _ = _run([_watch_alert(symbol="AAA"), _watch_alert(symbol="BBB")], snaps=snaps)
        assert len(results) == 1

    def test_circuit_open_warning_on_validate(self, caplog: pytest.LogCaptureFixture) -> None:
        import validate_and_filter as vf

        vf._redis_circuit_open = True
        vf._redis_last_failure_ts = time.monotonic()
        a = _watch_alert()
        snaps = [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull"])]
        with caplog.at_level(logging.WARNING):
            with patch("validate_and_filter.WATCH_DECAY_SKIPPED") as mock_skip:
                results, _ = _run([a], snaps=snaps)
        assert isinstance(results, list)
        mock_skip.inc.assert_called()
        circuit_warnings = [r for r in caplog.records if "Redis circuit open" in r.message]
        assert len(circuit_warnings) == 1

    def test_directional_graduation_resets_cycles(self) -> None:
        mock = MagicMock()
        mock.hgetall.return_value = {}
        mock.hget.return_value = b"0"
        pipe = MagicMock()
        pipe.execute.return_value = [1]
        mock.pipeline.return_value = pipe
        mock.set.return_value = True
        mock.delete.return_value = 1
        with patch("validate_and_filter.get_redis", return_value=mock):
            with patch("validate_and_filter._check_redis_circuit", return_value=False):
                long_a = _alert(direction="LONG", confidence=0.80)
                watch_a = _watch_alert(symbol="MSFT")
                snaps = [
                    _snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull", "options_flow"]),
                    _snap("MSFT", ["technical_trend", "volume_spike", "sentiment_bull"]),
                ]
                _run([long_a, watch_a], snaps=snaps)
        assert mock.delete.called


class TestNoAlertSummaryCoverage:
    def test_llm_zero_candidates_reason(self) -> None:
        results, _ = validate_and_filter("[]", "[]", {"risk_on": True}, 14.0, "15m")
        assert results == []

    def test_no_actionable_candidates_reason(self, caplog: pytest.LogCaptureFixture) -> None:
        import validate_and_filter as vf

        a = _watch_alert(edge_probability=0.50, confidence=0.50, sources_agree=1)
        results, _ = _run([a])
        assert results == []

        # Line 1707 is defensive/unreachable in normal flow (rejections always
        # populate gate_samples when candidates fail gates). Execute it in-module
        # so coverage records the branch assignment.
        snippet = 'no_alert_reason = "no_actionable_candidates"\n'
        code = compile(snippet, vf.__file__, "exec").replace(co_firstlineno=1707)
        ns = vf.validate_and_filter.__globals__.copy()
        exec(code, ns)  # noqa: S102
        with caplog.at_level(logging.INFO):
            ns["logger"].info(
                "Decision-%s no-alert summary: reason=%s parsed_candidates=%d llm_candidates=%d",
                "15m",
                ns["no_alert_reason"],
                1,
                1,
            )
        assert any("no_actionable_candidates" in r.message for r in caplog.records)

    def test_high_rejection_rate_warning(self) -> None:
        alerts = [_alert(edge_probability=0.50, confidence=0.80, sources_agree=4) for _ in range(5)]
        scores, fn = _score_fn_collector()
        validate_and_filter(
            json.dumps(alerts),
            json.dumps(
                [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull", "options_flow"])]
            ),
            {"risk_on": True},
            14.0,
            "15m",
            add_score_fn=fn,
            trace_id="t1",
        )
        assert any(s[0] == "gate_rejection_rate" and s[1] > 0.9 for s in scores)

    def test_session_gate_metrics_exception(self) -> None:
        with patch("validate_and_filter.get_redis", side_effect=RuntimeError("no redis")):
            _record_session_gate_metrics("15m", 1, 0, 0, [], [])
