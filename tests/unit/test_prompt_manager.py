"""Unit tests for prompt_manager — Langfuse prompt management with YAML fallback."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import prompt_manager as pm

# ── Helpers ──────────────────────────────────────────────────────


def _base_variables() -> dict:
    """Return a minimal variables dict for prompt compilation."""
    return {
        "macro_summary": "Risk-on, VIX=14, Yield curve=18bps",
        "vix": "14",
        "yc": "18",
        "n": "5",
        "snapshots_json": '[{"symbol":"AAPL"}]',
    }


def _reset_module() -> None:
    """Reset module-level caches between tests."""
    pm._last_source = "not-loaded"
    pm._last_version = "yaml-fallback"
    pm._prompt_cache.clear()


# ── YAML Fallback Tests ─────────────────────────────────────────


class TestYAMLFallback:
    """Tests for the built-in YAML-equivalent fallback prompts."""

    def setup_method(self) -> None:
        _reset_module()

    @patch("prompt_manager.get_langfuse_client", return_value=None)
    def test_returns_system_and_user_tuple(self, _mock: MagicMock) -> None:
        system, user = pm.get_decision_prompts("15m", _base_variables())
        assert isinstance(system, str)
        assert isinstance(user, str)
        assert len(system) > 50
        assert len(user) > 50

    @patch("prompt_manager.get_langfuse_client", return_value=None)
    def test_15m_gate_defaults(self, _mock: MagicMock) -> None:
        _, user = pm.get_decision_prompts("15m", _base_variables())
        assert "edge_probability >= 0.70" in user
        assert "sources_agree >= 3" in user
        assert "confidence >= 0.75" in user

    @patch("prompt_manager.get_langfuse_client", return_value=None)
    def test_1h_gate_defaults(self, _mock: MagicMock) -> None:
        _, user = pm.get_decision_prompts("1h", _base_variables())
        assert "edge_probability >= 0.75" in user
        assert "sources_agree >= 3" in user

    @patch("prompt_manager.get_langfuse_client", return_value=None)
    def test_1h_extra_rules_in_system(self, _mock: MagicMock) -> None:
        system, _ = pm.get_decision_prompts("1h", _base_variables())
        assert "macro_risk_off" in system
        assert "veto" in system

    @patch("prompt_manager.get_langfuse_client", return_value=None)
    def test_15m_has_risk_off_rules(self, _mock: MagicMock) -> None:
        system, _ = pm.get_decision_prompts("15m", _base_variables())
        assert "VIX > 20" in system
        assert "suppress LONG" in system

    @patch("prompt_manager.get_langfuse_client", return_value=None)
    def test_variables_interpolated(self, _mock: MagicMock) -> None:
        _, user = pm.get_decision_prompts("15m", _base_variables())
        assert "VIX: 14" in user
        assert "Yield Curve: 18bps" in user
        assert "Risk-on" in user

    @patch("prompt_manager.get_langfuse_client", return_value=None)
    def test_prompt_version_is_yaml_fallback(self, _mock: MagicMock) -> None:
        pm.get_decision_prompts("15m", _base_variables())
        assert pm.get_prompt_version() == "yaml-fallback"

    @patch("prompt_manager.get_langfuse_client", return_value=None)
    def test_prompt_source_is_yaml_fallback(self, _mock: MagicMock) -> None:
        pm.get_decision_prompts("15m", _base_variables())
        assert pm.get_prompt_source() == "yaml-fallback"

    @patch("prompt_manager.get_langfuse_client", return_value=None)
    def test_custom_gate_override(self, _mock: MagicMock) -> None:
        """Variables dict can override default gates."""
        vars_ = {**_base_variables(), "ep_gate": "0.90"}
        _, user = pm.get_decision_prompts("15m", vars_)
        assert "edge_probability >= 0.90" in user


# ── Langfuse-First Tests ────────────────────────────────────────


class TestLangfuseFirst:
    """Tests for the Langfuse prompt management primary path."""

    def setup_method(self) -> None:
        _reset_module()

    @patch("prompt_manager.get_langfuse_client")
    def test_langfuse_prompt_used_when_available(self, mock_client: MagicMock) -> None:
        # Mock the Langfuse prompt objects
        sys_prompt = MagicMock()
        sys_prompt.compile.return_value = "LF-SYSTEM-PROMPT"
        sys_prompt.version = 42

        usr_prompt = MagicMock()
        usr_prompt.compile.return_value = "LF-USER-PROMPT"
        usr_prompt.version = 42

        lf = MagicMock()
        lf.get_prompt.side_effect = lambda name, **kw: sys_prompt if name == "decision-system" else usr_prompt
        mock_client.return_value = lf

        system, user = pm.get_decision_prompts("15m", _base_variables())
        assert system == "LF-SYSTEM-PROMPT"
        assert user == "LF-USER-PROMPT"
        assert pm.get_prompt_version() == "42"
        assert pm.get_prompt_source() == "langfuse"

    @patch("prompt_manager.get_langfuse_client")
    def test_langfuse_prompts_compiled_with_variables(self, mock_client: MagicMock) -> None:
        sys_prompt = MagicMock()
        sys_prompt.compile.return_value = "SYS"
        sys_prompt.version = 1

        usr_prompt = MagicMock()
        usr_prompt.compile.return_value = "USR"
        usr_prompt.version = 1

        lf = MagicMock()
        lf.get_prompt.side_effect = lambda name, **kw: sys_prompt if name == "decision-system" else usr_prompt
        mock_client.return_value = lf

        pm.get_decision_prompts("15m", _base_variables())

        # Verify compile was called with merged variables
        call_kwargs = sys_prompt.compile.call_args[1]
        assert call_kwargs["timeframe"] == "15m"
        assert call_kwargs["ep_gate"] == "0.70"

    @patch("prompt_manager.get_langfuse_client")
    def test_falls_back_on_langfuse_error(self, mock_client: MagicMock) -> None:
        lf = MagicMock()
        lf.get_prompt.side_effect = ConnectionError("network down")
        mock_client.return_value = lf

        system, user = pm.get_decision_prompts("15m", _base_variables())
        # Should still return valid prompts from fallback
        assert "quantitative trading signal evaluator" in system
        assert pm.get_prompt_source() == "yaml-fallback"

    @patch("prompt_manager.get_langfuse_client")
    def test_falls_back_on_not_found(self, mock_client: MagicMock) -> None:
        lf = MagicMock()
        lf.get_prompt.side_effect = Exception("Prompt not found")
        mock_client.return_value = lf

        system, user = pm.get_decision_prompts("1h", _base_variables())
        assert "edge_probability >= 0.75" in user
        assert pm.get_prompt_version() == "yaml-fallback"


# ── Compile Template Tests ───────────────────────────────────────


class TestCompileTemplate:
    """Tests for the internal _compile_template helper."""

    def test_basic_substitution(self) -> None:
        result = pm._compile_template("Hello {{name}}!", {"name": "World"})
        assert result == "Hello World!"

    def test_multiple_substitutions(self) -> None:
        template = "{{a}} + {{b}} = {{c}}"
        result = pm._compile_template(template, {"a": "1", "b": "2", "c": "3"})
        assert result == "1 + 2 = 3"

    def test_missing_variable_left_as_is(self) -> None:
        result = pm._compile_template("{{exists}} {{missing}}", {"exists": "yes"})
        assert result == "yes {{missing}}"

    def test_numeric_values_converted(self) -> None:
        result = pm._compile_template("count={{n}}", {"n": 42})
        assert result == "count=42"
