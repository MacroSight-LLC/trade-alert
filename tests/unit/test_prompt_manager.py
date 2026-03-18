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
        assert "discount" in system.lower()

    @patch("prompt_manager.get_langfuse_client", return_value=None)
    def test_15m_has_risk_off_rules(self, _mock: MagicMock) -> None:
        system, _ = pm.get_decision_prompts("15m", _base_variables())
        assert "VIX > 25" in system
        assert "CAUTIOUS with LONG" in system

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


# ── Data Freshness & Performance Context ─────────────────────────


class TestNewTemplateVariables:
    """Tests for data_freshness, performance_context, few_shot_examples."""

    def setup_method(self) -> None:
        _reset_module()

    @patch("prompt_manager.get_langfuse_client", return_value=None)
    def test_data_freshness_default_is_live(self, _mock: MagicMock) -> None:
        _, user = pm.get_decision_prompts("15m", _base_variables())
        assert "LIVE" in user

    @patch("prompt_manager.get_langfuse_client", return_value=None)
    def test_data_freshness_override(self, _mock: MagicMock) -> None:
        vars_ = {**_base_variables(), "data_freshness": "CACHED"}
        _, user = pm.get_decision_prompts("15m", vars_)
        assert "CACHED" in user

    @patch("prompt_manager.get_langfuse_client", return_value=None)
    def test_performance_context_in_system(self, _mock: MagicMock) -> None:
        vars_ = {
            **_base_variables(),
            "performance_context": "Win rate: 65% over last 7 days",
        }
        system, _ = pm.get_decision_prompts("15m", vars_)
        assert "Win rate: 65%" in system

    @patch("prompt_manager.get_langfuse_client", return_value=None)
    def test_performance_context_default(self, _mock: MagicMock) -> None:
        system, _ = pm.get_decision_prompts("15m", _base_variables())
        assert "No recent outcome data" in system

    @patch("prompt_manager.get_langfuse_client", return_value=None)
    def test_few_shot_examples_in_user(self, _mock: MagicMock) -> None:
        vars_ = {
            **_base_variables(),
            "few_shot_examples": "REFERENCE EXAMPLES: ...",
        }
        _, user = pm.get_decision_prompts("15m", vars_)
        assert "REFERENCE EXAMPLES:" in user

    @patch("prompt_manager.get_langfuse_client", return_value=None)
    def test_1h_has_macro_emphasis_rules(self, _mock: MagicMock) -> None:
        system, _ = pm.get_decision_prompts("1h", _base_variables())
        assert "MACRO AWARENESS" in system
        assert "fundamental" in system.lower()


# ── format_winrate_context ───────────────────────────────────────


class TestFormatWinrateContext:
    """Tests for the winrate context formatter."""

    def test_formats_winrate_data(self) -> None:
        import db

        with patch.object(db, "get_recent_winrate_summary") as mock_summary:
            mock_summary.return_value = {
                "total_resolved": 20,
                "wins": 13,
                "losses": 7,
                "winrate": 0.65,
                "avg_ep": 0.78,
                "ep_calibration": [
                    {"bucket": 0.8, "total": 10, "wins": 6, "actual_winrate": 0.60},
                ],
            }
            result = pm.format_winrate_context()
        assert "65" in result
        assert "20" in result

    def test_few_resolved_returns_default(self) -> None:
        import db

        with patch.object(db, "get_recent_winrate_summary") as mock_summary:
            mock_summary.return_value = {
                "total_resolved": 3,
                "wins": 2,
                "losses": 1,
                "winrate": 0.67,
                "avg_ep": 0.80,
                "ep_calibration": [],
            }
            result = pm.format_winrate_context()
        assert "No recent outcome data" in result

    def test_exception_returns_safe_default(self) -> None:
        import db

        with patch.object(db, "get_recent_winrate_summary", side_effect=RuntimeError("DB down")):
            result = pm.format_winrate_context()
        assert isinstance(result, str)
        assert "No recent outcome data" in result


# ── get_quality_escalation_rules ─────────────────────────────────


class TestGetQualityEscalationRules:
    """Tests for dynamic quality-based prompt escalation."""

    @patch("langfuse_client.get_langfuse_client", return_value=None)
    def test_no_langfuse_returns_empty(self, _mock: MagicMock) -> None:
        assert pm.get_quality_escalation_rules("15m") == ""

    @patch("langfuse_client.get_langfuse_client")
    def test_strict_escalation_below_050(self, mock_get_client: MagicMock) -> None:
        mock_score = MagicMock()
        mock_score.name = "batch_avg_quality"
        mock_score.value = 0.40

        mock_trace = MagicMock()
        mock_trace.scores = [mock_score]

        mock_response = MagicMock()
        mock_response.data = [mock_trace] * 5

        lf = MagicMock()
        lf.fetch_traces.return_value = mock_response
        mock_get_client.return_value = lf

        result = pm.get_quality_escalation_rules("15m")
        assert "STRICT" in result
        assert "at MOST 2" in result

    @patch("langfuse_client.get_langfuse_client")
    def test_moderate_escalation_below_065(self, mock_get_client: MagicMock) -> None:
        mock_score = MagicMock()
        mock_score.name = "batch_avg_quality"
        mock_score.value = 0.55

        mock_trace = MagicMock()
        mock_trace.scores = [mock_score]

        mock_response = MagicMock()
        mock_response.data = [mock_trace] * 5

        lf = MagicMock()
        lf.fetch_traces.return_value = mock_response
        mock_get_client.return_value = lf

        result = pm.get_quality_escalation_rules("15m")
        assert "MODERATE" in result

    @patch("langfuse_client.get_langfuse_client")
    def test_no_escalation_above_065(self, mock_get_client: MagicMock) -> None:
        mock_score = MagicMock()
        mock_score.name = "batch_avg_quality"
        mock_score.value = 0.80

        mock_trace = MagicMock()
        mock_trace.scores = [mock_score]

        mock_response = MagicMock()
        mock_response.data = [mock_trace] * 5

        lf = MagicMock()
        lf.fetch_traces.return_value = mock_response
        mock_get_client.return_value = lf

        result = pm.get_quality_escalation_rules("15m")
        assert result == ""

    @patch("langfuse_client.get_langfuse_client")
    def test_insufficient_data_returns_empty(self, mock_get_client: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.data = []

        lf = MagicMock()
        lf.fetch_traces.return_value = mock_response
        mock_get_client.return_value = lf

        result = pm.get_quality_escalation_rules("15m")
        assert result == ""
