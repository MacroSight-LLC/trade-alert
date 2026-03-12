"""Unit tests for safe template evaluation in pipeline_runner.py.

Verifies that the AST-based evaluator blocks code injection attempts
while allowing all legitimate workflow template expressions.
"""

from __future__ import annotations

import pytest

from pipeline_runner import _render_template, _safe_eval

# ── Legitimate expressions (must work) ──────────────────────────


class TestLegitimateExpressions:
    """Expressions actually used in workflow YAML files."""

    def test_simple_subscript(self) -> None:
        steps = {"read-universes": {"equities": ["AAPL", "NVDA", "TSLA"]}}
        result = _render_template("{{ steps['read-universes']['equities'] }}", steps)
        assert result == ["AAPL", "NVDA", "TSLA"]

    def test_subscript_with_slice(self) -> None:
        steps = {"read-universes": {"equities": ["AAPL", "NVDA", "TSLA", "GOOG", "AMZN"]}}
        result = _render_template("{{ steps['read-universes']['equities'][:3] }}", steps)
        assert result == ["AAPL", "NVDA", "TSLA"]

    def test_not_operator(self) -> None:
        steps = {"merge-snapshots": {"skip": False}}
        result = _render_template('{{ not steps["merge-snapshots"]["skip"] }}', steps)
        assert result is True

    def test_comparison_gt(self) -> None:
        steps = {"validate-and-filter": {"count": 5}}
        result = _render_template('{{ steps["validate-and-filter"]["count"] > 0 }}', steps)
        assert result is True

    def test_comparison_gt_false(self) -> None:
        steps = {"validate-and-filter": {"count": 0}}
        result = _render_template('{{ steps["validate-and-filter"]["count"] > 0 }}', steps)
        assert result is False

    def test_string_value(self) -> None:
        steps = {"build-prompt": {"prompt": "Hello LLM"}}
        result = _render_template('{{ steps["build-prompt"]["prompt"] }}', steps)
        assert result == "Hello LLM"

    def test_mixed_interpolation(self) -> None:
        steps = {"info": {"name": "AAPL"}}
        result = _render_template("Symbol is {{ steps['info']['name'] }} today", steps)
        assert result == "Symbol is AAPL today"

    def test_non_template_passthrough(self) -> None:
        result = _render_template("plain text", {})
        assert result == "plain text"

    def test_non_string_passthrough(self) -> None:
        result = _render_template(42, {})  # type: ignore[arg-type]
        assert result == 42

    def test_builtin_len(self) -> None:
        steps = {"data": {"items": [1, 2, 3]}}
        result = _render_template("{{ len(steps['data']['items']) }}", steps)
        assert result == 3

    def test_extra_vars(self) -> None:
        result = _render_template("{{ timeframe }}", {}, extra_vars={"timeframe": "15m"})
        assert result == "15m"


# ── Injection attempts (must be blocked) ─────────────────────────


class TestInjectionBlocked:
    """Attempts to exploit the template engine must raise ValueError."""

    def test_dunder_class_access(self) -> None:
        with pytest.raises(ValueError, match="Dunder attribute"):
            _safe_eval("().__class__.__bases__[0].__subclasses__()", {"steps": {}})

    def test_dunder_import(self) -> None:
        with pytest.raises(ValueError, match="Dunder attribute"):
            _safe_eval("''.__class__.__mro__[1].__subclasses__()", {"steps": {}})

    def test_import_builtin(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            _safe_eval("__import__('os')", {"steps": {}})

    def test_getattr_dunder(self) -> None:
        with pytest.raises(ValueError, match="Dunder attribute"):
            _safe_eval("steps.__class__", {"steps": {}})

    def test_arbitrary_function_call(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            _safe_eval("open('/etc/passwd')", {"steps": {}})

    def test_exec_call(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            _safe_eval("exec('import os')", {"steps": {}})

    def test_eval_call(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            _safe_eval("eval('1+1')", {"steps": {}})

    def test_lambda_not_allowed(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            _safe_eval("(lambda: 1)()", {"steps": {}})

    def test_comprehension_not_allowed(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            _safe_eval("[x for x in range(10)]", {"steps": {}})

    def test_nested_dunder_via_mcp_data(self) -> None:
        """Simulate a malicious MCP response injected into steps."""
        steps = {"collector": {"payload": "().__class__"}}
        with pytest.raises(ValueError, match="Dunder attribute"):
            _safe_eval("steps['collector']['payload'].__class__.__bases__", {"steps": steps})

    def test_render_template_blocks_injection(self) -> None:
        with pytest.raises(ValueError, match="Dunder attribute"):
            _render_template("{{ ().__class__.__bases__ }}", {})

    def test_unknown_name_blocked(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            _safe_eval("os", {"steps": {}})
