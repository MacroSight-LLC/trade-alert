"""Unit tests for gates/candidate_pipeline.py stage isolation."""

from __future__ import annotations

from gates.candidate_pipeline import (
    DEFAULT_STAGES,
    CandidateContext,
    ReasonTracker,
    _stage_macro_veto,
    _stage_source_hallucination,
)
from models import PlaybookAlert
from validate_and_filter import _build_candidate_gate_config


def _make_alert(
    *,
    symbol: str = "AAPL",
    direction: str = "LONG",
    ep: float = 0.80,
    sa: int = 5,
    conf: float = 0.85,
) -> PlaybookAlert:
    return PlaybookAlert(
        symbol=symbol,
        direction=direction,
        edge_probability=ep,
        confidence=conf,
        timeframe="1h",
        thesis="Pipeline stage test.",
        entry={"level": 100.0, "stop": 95.0, "target": 115.0},
        timeframe_rationale="test",
        sentiment_context="neutral",
        unusual_activity=[],
        macro_regime="risk-on",
        sources_agree=sa,
    )


def _base_ctx(
    alert: PlaybookAlert,
    *,
    snap_types: dict[str, set[str]] | None = None,
    risk_off: bool = False,
    vix: float = 15.0,
) -> CandidateContext:
    return CandidateContext(
        alert=alert,
        config=_build_candidate_gate_config(),
        timeframe="1h",
        regime="normal",
        vix=vix,
        risk_off=risk_off,
        ep_gate=0.70,
        sa_gate=3,
        conf_gate=0.75,
        market_session="regular",
        snap_types=snap_types if snap_types is not None else {alert.symbol: {"technical_trend"}},
        family_scores_index={alert.symbol: {"trend": 2.0}},
        forecast_scores={},
        volume_scores={alert.symbol: 1.0},
        ref_prices={alert.symbol: 100.0},
    )


class TestReasonTracker:
    def test_dedupes_repeated_reasons(self) -> None:
        from gates.types import GateRejection

        tracker = ReasonTracker()
        tracker.add(GateRejection.EP_THRESHOLD)
        tracker.add(GateRejection.EP_THRESHOLD)
        assert tracker.reasons == [GateRejection.EP_THRESHOLD]


class TestSourceHallucinationStage:
    def test_adds_reason_when_symbol_missing_from_snapshots(self) -> None:
        from gates.types import GateRejection

        alert = _make_alert(symbol="GHOST")
        ctx = _base_ctx(alert, snap_types={})
        _stage_source_hallucination(ctx)
        assert GateRejection.SOURCE_HALLUCINATION in ctx.tracker.reason_set

    def test_no_reason_when_symbol_present(self) -> None:
        alert = _make_alert()
        ctx = _base_ctx(alert)
        _stage_source_hallucination(ctx)
        assert not ctx.tracker.reasons

    def test_emits_symbol_hallucination_score_via_telemetry(self) -> None:
        from telemetry.context import TelemetryContext

        scores: list[tuple[str, float]] = []
        telemetry = TelemetryContext.for_trace("trace-test")

        def _capture_score(name: str, value: float, *, comment: str = "") -> None:
            scores.append((name, value))

        telemetry.score = _capture_score  # type: ignore[method-assign]

        alert = _make_alert(symbol="GHOST")
        ctx = _base_ctx(alert, snap_types={})
        ctx.telemetry = telemetry
        _stage_source_hallucination(ctx)
        assert scores == [("symbol_hallucination", 1.0)]


class TestMacroVetoStage:
    def test_vetoes_long_1h_risk_off_high_vix(self) -> None:
        from gates.types import GateRejection

        alert = _make_alert(direction="LONG", ep=0.75, sa=3)
        ctx = _base_ctx(alert, risk_off=True, vix=26.0)
        _stage_macro_veto(ctx)
        assert GateRejection.MACRO_VETO in ctx.tracker.reason_set


class TestDefaultPipeline:
    def test_stage_count_and_order(self) -> None:
        names = [stage.__name__ for stage in DEFAULT_STAGES]
        assert names[0] == "_stage_source_hallucination"
        assert names[1] == "_stage_reconciliation"
        assert names[-1] == "_stage_volume_unconfirmed"
        assert len(names) == 13
