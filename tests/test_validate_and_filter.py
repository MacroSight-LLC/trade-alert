"""Unit tests for validate_and_filter module.

Tests the three new server-side rules (EP ceiling, VIX hard gate,
source hallucination) plus existing gate behaviour.  Uses
``pytest.mark.parametrize`` for boundary and rejection paths.
"""

from __future__ import annotations

import json

import pytest

from validate_and_filter import (
    EP_CEILING,
    _build_snap_type_index,
    _get_macro_risk_off_score,
    validate_and_filter,
)

# ── Helpers ────────────────────────────────────────────────────────


def _alert(**overrides: object) -> dict:
    """Build a valid PlaybookAlert dict with sensible test defaults."""
    base = {
        "symbol": "AAPL",
        "direction": "LONG",
        "edge_probability": 0.80,
        "confidence": 0.85,
        "timeframe": "15m",
        "thesis": "Bollinger squeeze with volume confirmation.",
        "entry": {"level": 185.0, "stop": 182.0, "target": 195.0},
        "timeframe_rationale": "15m breakout aligning with 1h.",
        "sentiment_context": "Retail bullish.",
        "unusual_activity": ["IV spike"],
        "macro_regime": "Risk-on.",
        "sources_agree": 4,
    }
    base.update(overrides)
    return base


def _snap(symbol: str, types: list[str]) -> dict:
    return {
        "symbol": symbol,
        "timeframe": "15m",
        "timestamp": "2026-03-12T14:00:00Z",
        "signals": [
            {"source": "test", "type": t, "score": 1.5, "confidence": 0.8, "reason": f"test {t}"}
            for t in types
        ],
    }


def _run(
    alerts: list[dict],
    snaps: list[dict] | None = None,
    vix: float = 14.0,
    macro: dict | None = None,
    timeframe: str = "15m",
):
    if snaps is None:
        snaps = [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull", "options_flow"])]
    return validate_and_filter(
        llm_response=json.dumps(alerts),
        snapshots_json=json.dumps(snaps),
        macro=macro or {"risk_on": True},
        vix=vix,
        timeframe=timeframe,
    )


# ── EP Ceiling Tests (Group 2a) ───────────────────────────────────


class TestEpCeiling:
    """EP ceiling caps edge_probability by actual source count."""

    @pytest.mark.parametrize(
        "n_sources, expected_ceiling",
        [(1, 0.55), (2, 0.65), (3, 0.75), (4, 0.85), (5, 0.90)],
    )
    def test_ceiling_lookup_values(self, n_sources: int, expected_ceiling: float) -> None:
        assert EP_CEILING[n_sources] == expected_ceiling

    def test_ep_capped_when_above_ceiling(self) -> None:
        """EP=0.90 with 3 sources → capped to 0.75."""
        snaps = [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull"])]
        a = _alert(sources_agree=3, edge_probability=0.90)
        results, _ = _run([a], snaps=snaps)
        assert len(results) == 1
        assert results[0].edge_probability == 0.75

    def test_ep_unchanged_when_below_ceiling(self) -> None:
        """EP=0.72 with 4 sources (ceiling=0.85) → no capping."""
        snaps = [
            _snap(
                "AAPL",
                [
                    "technical_trend",
                    "volume_spike",
                    "sentiment_bull",
                    "options_flow",
                ],
            )
        ]
        a = _alert(sources_agree=4, edge_probability=0.72)
        results, _ = _run([a], snaps=snaps)
        assert len(results) == 1
        assert results[0].edge_probability == 0.72

    def test_ep_at_exact_ceiling_passes(self) -> None:
        """EP exactly at ceiling → not capped."""
        snaps = [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull"])]
        a = _alert(sources_agree=3, edge_probability=0.75)
        results, _ = _run([a], snaps=snaps)
        assert len(results) == 1
        assert results[0].edge_probability == 0.75

    def test_ep_ceiling_causes_gate_rejection(self) -> None:
        """EP=0.80 with 2 sources → capped to 0.65, below 15m gate 0.70 → filtered."""
        snaps = [_snap("AAPL", ["technical_trend", "volume_spike"])]
        a = _alert(sources_agree=2, edge_probability=0.80)
        results, _ = _run([a], snaps=snaps)
        # Capped to 0.65, below 0.70 gate → filtered out.
        # Also sources_agree=2 < 3 gate → double filter.
        assert len(results) == 0


# ── VIX Hard Gate Tests (Group 2b) ────────────────────────────────


class TestVixHardGate:
    """VIX > 30 universal hard gate rejects all directional alerts."""

    @pytest.mark.parametrize("direction", ["LONG", "SHORT"])
    def test_vix_above_30_rejects_directional(self, direction: str) -> None:
        a = _alert(direction=direction)
        results, _ = _run([a], vix=31.0)
        assert len(results) == 0

    def test_vix_above_30_allows_watch(self) -> None:
        a = _alert(direction="WATCH", entry={"level": 185.0, "stop": 185.0, "target": 185.0})
        results, _ = _run([a], vix=35.0)
        assert len(results) == 1

    def test_vix_exactly_30_passes(self) -> None:
        """VIX=30.0 (not > 30) → gate does not fire."""
        a = _alert()
        results, _ = _run([a], vix=30.0)
        assert len(results) == 1

    def test_vix_zero_skips_gate(self) -> None:
        """VIX=0.0 (missing data) → gate skipped."""
        a = _alert()
        results, _ = _run([a], vix=0.0)
        assert len(results) == 1


# ── Source Hallucination Tests (Group 2c) ──────────────────────────


class TestSourceHallucination:
    """Source-count mismatch detection: delta ≥ 2 hard reject, == 1 downgrade."""

    def test_delta_2_hard_reject(self) -> None:
        """LLM claims 5, actual 3 → delta=2 → hard reject."""
        snaps = [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull"])]
        a = _alert(sources_agree=5)
        results, _ = _run([a], snaps=snaps)
        assert len(results) == 0

    def test_delta_3_hard_reject(self) -> None:
        """LLM claims 5, actual 2 → delta=3 → hard reject."""
        snaps = [_snap("AAPL", ["technical_trend", "volume_spike"])]
        a = _alert(sources_agree=5)
        results, _ = _run([a], snaps=snaps)
        assert len(results) == 0

    def test_delta_1_downgrade(self) -> None:
        """LLM claims 4, actual 3 → delta=1 → downgraded, not rejected."""
        snaps = [_snap("AAPL", ["technical_trend", "volume_spike", "sentiment_bull"])]
        a = _alert(sources_agree=4, edge_probability=0.75)
        results, _ = _run([a], snaps=snaps)
        assert len(results) == 1
        assert results[0].sources_agree == 3

    def test_delta_0_no_change(self) -> None:
        """LLM claims 4, actual 4 → no change."""
        snaps = [
            _snap(
                "AAPL",
                [
                    "technical_trend",
                    "volume_spike",
                    "sentiment_bull",
                    "options_flow",
                ],
            )
        ]
        a = _alert(sources_agree=4, edge_probability=0.80)
        results, _ = _run([a], snaps=snaps)
        assert len(results) == 1
        assert results[0].sources_agree == 4

    def test_negative_delta_no_change(self) -> None:
        """LLM claims 3, actual 4 → delta=-1 → no change."""
        snaps = [
            _snap(
                "AAPL",
                [
                    "technical_trend",
                    "volume_spike",
                    "sentiment_bull",
                    "options_flow",
                ],
            )
        ]
        a = _alert(sources_agree=3, edge_probability=0.75)
        results, _ = _run([a], snaps=snaps)
        assert len(results) == 1
        assert results[0].sources_agree == 3


# ── Helper Function Tests ────────────────────────────────────────


class TestBuildSnapTypeIndex:
    """Tests for _build_snap_type_index()."""

    def test_normal_case(self) -> None:
        snaps = [_snap("AAPL", ["technical_trend", "volume_spike"])]
        result = _build_snap_type_index(json.dumps(snaps))
        assert result == {"AAPL": {"technical_trend", "volume_spike"}}

    def test_empty_json(self) -> None:
        assert _build_snap_type_index("[]") == {}

    def test_malformed_json(self) -> None:
        assert _build_snap_type_index("not json") == {}


class TestMacroRiskOffScore:
    """Tests for _get_macro_risk_off_score()."""

    def test_extracts_score(self) -> None:
        snaps = [
            {
                "symbol": "AAPL",
                "signals": [
                    {"type": "macro_risk_off", "score": -2.5},
                    {"type": "technical_trend", "score": 1.0},
                ],
            }
        ]
        assert _get_macro_risk_off_score(json.dumps(snaps)) == 2.5

    def test_no_macro_signals(self) -> None:
        snaps = [{"symbol": "AAPL", "signals": [{"type": "technical_trend", "score": 1.0}]}]
        assert _get_macro_risk_off_score(json.dumps(snaps)) == 0.0


# ── R:R and Gate Integration Tests ─────────────────────────────────


class TestGateIntegration:
    """End-to-end gate interaction tests."""

    def test_rr_below_2_rejected(self) -> None:
        """R:R ratio below 2:1 → filtered."""
        a = _alert(entry={"level": 185.0, "stop": 182.0, "target": 188.0})
        # reward=3, risk=3 → 1:1 R:R
        results, _ = _run([a])
        assert len(results) == 0

    def test_1h_macro_veto(self) -> None:
        """1h timeframe with strong macro_risk_off → LONG vetoed."""
        snaps = [
            {
                "symbol": "AAPL",
                "timeframe": "1h",
                "timestamp": "2026-03-12T14:00:00Z",
                "signals": [
                    {
                        "source": "test",
                        "type": "technical_trend",
                        "score": 1.5,
                        "confidence": 0.8,
                        "reason": "t",
                    },
                    {
                        "source": "test",
                        "type": "volume_spike",
                        "score": 1.5,
                        "confidence": 0.8,
                        "reason": "t",
                    },
                    {
                        "source": "test",
                        "type": "sentiment_bull",
                        "score": 1.5,
                        "confidence": 0.8,
                        "reason": "t",
                    },
                    {
                        "source": "fred",
                        "type": "macro_risk_off",
                        "score": -2.5,
                        "confidence": 0.9,
                        "reason": "t",
                    },
                ],
            },
        ]
        a = _alert(direction="LONG", timeframe="1h", edge_probability=0.85, sources_agree=4)
        results, _ = _run([a], snaps=snaps, timeframe="1h")
        assert len(results) == 0

    def test_invalid_json_returns_empty(self) -> None:
        """Invalid LLM JSON → empty result."""
        results, json_str = validate_and_filter(
            llm_response="not valid json",
            snapshots_json="[]",
            macro={"risk_on": True},
            vix=14.0,
            timeframe="15m",
        )
        assert results == []
        assert json_str == "[]"
