"""Unit tests for gate_config.py (stabilization sprint)."""

from __future__ import annotations

import importlib

import pytest

import gate_config
from gates.types import GateRejection


@pytest.fixture(autouse=True)
def _restore_gate_config_module() -> None:
    """Reload gate_config after env-mutation tests so later tests see defaults."""
    yield
    importlib.reload(gate_config)


class TestEnvThresholds:
    def test_default_ep_rr_sa_conf(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in (
            "GATE_EP_15M",
            "GATE_EP_1H",
            "GATE_SA",
            "GATE_CONF",
            "GATE_RR_15M",
            "GATE_RR_1H",
        ):
            monkeypatch.delenv(key, raising=False)
        importlib.reload(gate_config)
        assert gate_config.GATE_EP["15m"] == pytest.approx(0.70)
        assert gate_config.GATE_EP["1h"] == pytest.approx(0.75)
        assert gate_config.GATE_SA == 4
        assert gate_config.GATE_CONF == pytest.approx(0.75)
        assert gate_config.GATE_RR["15m"] == pytest.approx(2.0)
        assert gate_config.GATE_RR["1h"] == pytest.approx(2.5)

    def test_env_overrides_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GATE_EP_15M", "0.68")
        monkeypatch.setenv("GATE_EP_1H", "0.72")
        monkeypatch.setenv("GATE_SA", "5")
        monkeypatch.setenv("GATE_CONF", "0.80")
        monkeypatch.setenv("GATE_RR_15M", "1.8")
        monkeypatch.setenv("GATE_RR_1H", "2.2")
        importlib.reload(gate_config)
        assert gate_config.GATE_EP["15m"] == pytest.approx(0.68)
        assert gate_config.GATE_EP["1h"] == pytest.approx(0.72)
        assert gate_config.GATE_SA == 5
        assert gate_config.GATE_CONF == pytest.approx(0.80)
        assert gate_config.GATE_RR["15m"] == pytest.approx(1.8)
        assert gate_config.GATE_RR["1h"] == pytest.approx(2.2)

    def test_watch_and_extended_hours_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WATCH_MAX_PER_RUN", "2")
        monkeypatch.setenv("WATCH_MAX_NEUTRAL", "4")
        monkeypatch.setenv("EXTENDED_HOURS_ALERTS_ENABLED", "1")
        monkeypatch.setenv("EXTENDED_HOURS_CONFIDENCE_PENALTY", "-0.05")
        importlib.reload(gate_config)
        assert gate_config.WATCH_MAX_PER_RUN == 2
        assert gate_config.WATCH_MAX_NEUTRAL == 4
        assert gate_config.EXTENDED_HOURS_ALERTS_ENABLED is True
        assert gate_config.EXTENDED_HOURS_CONFIDENCE_PENALTY == pytest.approx(-0.05)


class TestClassifyRegime:
    @pytest.mark.parametrize(
        ("vix", "risk_off", "bulls", "bears", "trend_strength", "expected"),
        [
            (31.0, False, 5, 5, 0.8, "extreme"),
            (26.0, True, 5, 5, 0.8, "risk_off_high_vix"),
            (18.0, False, 5, 5, 0.2, "choppy"),
            (18.0, False, 5, 5, 0.5, "choppy"),
            (18.0, False, 6, 4, 0.8, "trending_up"),
            (18.0, True, 3, 7, 0.8, "trending_down"),
            (18.0, False, 3, 4, 0.8, "neutral"),
        ],
    )
    def test_regime_classification(
        self,
        vix: float,
        risk_off: bool,
        bulls: int,
        bears: int,
        trend_strength: float,
        expected: str,
    ) -> None:
        assert gate_config.classify_regime(vix, risk_off, bulls, bears, trend_strength) == expected

    def test_zero_breadth_defaults_to_choppy(self) -> None:
        # No bulls/bears → bull_ratio 0.5 → choppy band
        assert gate_config.classify_regime(20.0, False, 0, 0, 0.8) == "choppy"


class TestPromptGateVars:
    def test_15m_prompt_vars_match_defaults(self) -> None:
        vars_15m = gate_config.prompt_gate_vars("15m")
        assert vars_15m["ep_gate"] == f"{gate_config.GATE_EP['15m']:.2f}"
        assert vars_15m["sa_gate"] == str(gate_config.GATE_SA)
        assert vars_15m["conf_gate"] == f"{gate_config.GATE_CONF:.2f}"
        assert vars_15m["rr_gate"] == f"{gate_config.GATE_RR['15m']:.1f}"

    def test_1h_prompt_vars(self) -> None:
        vars_1h = gate_config.prompt_gate_vars("1h")
        assert vars_1h["rr_gate"] == f"{gate_config.GATE_RR['1h']:.1f}"

    def test_unknown_timeframe_falls_back_to_15m(self) -> None:
        assert gate_config.prompt_gate_vars("4h") == gate_config.prompt_gate_vars("15m")


class TestGateRejectionInventory:
    """All 23 GateRejection enum members (SSOT §10.4)."""

    EXPECTED_MEMBERS: frozenset[str] = frozenset(
        {
            "vix_hard",
            "source_hallucination",
            "ep_threshold",
            "sa_threshold",
            "high_confidence_alignment",
            "conf_threshold",
            "rr_minimum",
            "rr_zero_risk",
            "entry_order_invalid",
            "macro_veto",
            "vix_soft",
            "forecast_contradicts",
            "timeframe_invalid",
            "entry_market_drift",
            "volume_unconfirmed",
            "watch_ep_threshold",
            "watch_sa_threshold",
            "watch_conf_threshold",
            "watch_cap",
            "watch_dropped_directional_present",
            "watch_decay",
            "market_session_closed",
            "dedup_suppressed",
        }
    )

    def test_exactly_23_members(self) -> None:
        assert len(GateRejection) == 23

    def test_member_values_match_ssot(self) -> None:
        assert {m.value for m in GateRejection} == self.EXPECTED_MEMBERS

    def test_string_enum_values(self) -> None:
        for member in GateRejection:
            assert isinstance(member.value, str)
            assert member == member.value
