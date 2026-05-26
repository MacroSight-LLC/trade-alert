"""Unit tests for workflow_sandbox import allowlist."""

from __future__ import annotations

import importlib

import pytest

from workflow_sandbox import SandboxExecutor, _safe_import


class TestWorkflowSandboxAllowlist:
    """Auxiliary workflow imports must be explicitly allowlisted."""

    @pytest.mark.parametrize(
        ("module", "symbols"),
        [
            ("outcome_tracker", ("run_tracker_cycle",)),
            ("db", ("get_winrate_by_bucket",)),
            ("gate_config", ("GATE_EP", "GATE_SA", "GATE_CONF", "classify_regime")),
            ("gates.regime", ("_dynamic_gates",)),
            ("gates.watch", ("_watch_decay_key",)),
            ("validate_and_filter", ("_WATCH_DECAY_CYCLES",)),
            ("zoneinfo", ("ZoneInfo",)),
            ("math", ()),
        ],
    )
    def test_allowed_from_imports(self, module: str, symbols: tuple[str, ...]) -> None:
        imported = _safe_import(module, fromlist=symbols)
        if symbols:
            for symbol in symbols:
                assert hasattr(imported, symbol)
        else:
            assert imported is importlib.import_module(module)

    def test_blocked_import_raises(self) -> None:
        with pytest.raises(ImportError, match="os"):
            _safe_import("os")

    def test_state_summary_code_compiles_in_sandbox(self) -> None:
        """read-state step body executes with mocked Redis + macro regime."""
        from unittest.mock import MagicMock, patch

        code = """
import json, math, logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from merger import get_macro_regime
from gate_config import GATE_EP, GATE_SA, GATE_CONF, classify_regime
from gates.regime import _dynamic_gates
result = {"regime": classify_regime(20.0, False, bulls=0, bears=0, trend_strength=0.5)}
"""
        mock_redis = MagicMock()
        mock_redis.hgetall.return_value = {}
        executor = SandboxExecutor(get_redis=lambda: mock_redis)
        with patch("merger.get_macro_regime", return_value={"risk_on": True}):
            out = executor.execute(code, {})
        assert out["regime"]
