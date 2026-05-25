"""Unit tests for GateConfig unified model."""

from __future__ import annotations

import validate_and_filter
from gate_config import GateConfig


class TestGateConfig:
    def test_from_module_matches_build_candidate_gate_config(self) -> None:
        expected = validate_and_filter._build_candidate_gate_config()
        actual = GateConfig.from_module(validate_and_filter).candidate_config()
        assert actual == expected

    def test_from_env_returns_candidate_gate_config(self) -> None:
        import os

        cfg = GateConfig.from_env().candidate_config()
        assert cfg.sa_family_min_score == 0.25
        expected_hours = os.environ.get("MARKET_HOURS_GATES_ENABLED", "1") == "1"
        assert cfg.market_hours_gates_enabled is expected_hours
