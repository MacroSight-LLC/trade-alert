"""Unit tests for pipeline_runner template engine and safe_eval."""

from __future__ import annotations

# pipeline_runner imports vault_env_loader at module level; stub it early
import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("vault_env_loader", MagicMock())

from pipeline_runner import _render_template, _safe_eval  # noqa: E402

# ── _safe_eval ──────────────────────────────────────────────────


class TestSafeEval:
    """AST-based expression evaluator safety and correctness."""

    def test_constant(self) -> None:
        assert _safe_eval("42", {}) == 42

    def test_string_constant(self) -> None:
        assert _safe_eval("'hello'", {}) == "hello"

    def test_name_lookup(self) -> None:
        assert _safe_eval("x", {"x": 10}) == 10

    def test_subscript(self) -> None:
        assert _safe_eval("d['key']", {"d": {"key": 99}}) == 99

    def test_nested_subscript(self) -> None:
        ns = {"steps": {"a": {"b": [1, 2, 3]}}}
        assert _safe_eval("steps['a']['b'][1]", ns) == 2

    def test_attribute_access(self) -> None:
        ns = {"items": [1, 2, 3]}
        # list.__class__.__name__ would be blocked, but len is allowed
        result = _safe_eval("len(items)", ns)
        assert result == 3

    def test_dunder_access_blocked(self) -> None:
        with pytest.raises(ValueError, match="Dunder"):
            _safe_eval("x.__class__", {"x": 1})

    def test_unknown_name_raises(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            _safe_eval("undefined_var", {})

    def test_comparison(self) -> None:
        assert _safe_eval("x > 5", {"x": 10}) is True
        assert _safe_eval("x < 5", {"x": 10}) is False

    def test_boolean_and(self) -> None:
        assert _safe_eval("x > 0 and y > 0", {"x": 1, "y": 2}) is True
        assert _safe_eval("x > 0 and y > 0", {"x": 1, "y": -1}) is False

    def test_boolean_or(self) -> None:
        assert _safe_eval("x > 0 or y > 0", {"x": -1, "y": 2}) is True

    def test_arithmetic(self) -> None:
        assert _safe_eval("a + b", {"a": 3, "b": 4}) == 7
        assert _safe_eval("a * b", {"a": 3, "b": 4}) == 12

    def test_unary_neg(self) -> None:
        assert _safe_eval("-x", {"x": 5}) == -5

    def test_unary_not(self) -> None:
        assert _safe_eval("not x", {"x": False}) is True

    def test_if_expression(self) -> None:
        assert _safe_eval("'yes' if x else 'no'", {"x": True}) == "yes"
        assert _safe_eval("'yes' if x else 'no'", {"x": False}) == "no"

    def test_list_literal(self) -> None:
        assert _safe_eval("[1, 2, 3]", {}) == [1, 2, 3]

    def test_dict_literal(self) -> None:
        assert _safe_eval("{'a': 1}", {}) == {"a": 1}

    def test_tuple_literal(self) -> None:
        assert _safe_eval("(1, 2)", {}) == (1, 2)

    def test_whitelisted_func_len(self) -> None:
        assert _safe_eval("len(items)", {"items": [1, 2]}) == 2

    def test_whitelisted_func_str(self) -> None:
        assert _safe_eval("str(42)", {}) == "42"

    def test_whitelisted_func_max(self) -> None:
        assert _safe_eval("max(1, 5, 3)", {}) == 5

    def test_non_whitelisted_func_blocked(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            _safe_eval("open('/etc/passwd')", {})

    def test_slice(self) -> None:
        assert _safe_eval("items[1:3]", {"items": [0, 1, 2, 3, 4]}) == [1, 2]

    def test_in_operator(self) -> None:
        assert _safe_eval("'a' in d", {"d": {"a": 1}}) is True

    def test_not_in_operator(self) -> None:
        assert _safe_eval("'z' not in d", {"d": {"a": 1}}) is True

    def test_is_none(self) -> None:
        assert _safe_eval("x is None", {"x": None}) is True


# ── _render_template ──────────────────────────────────────────────


class TestRenderTemplate:
    """Jinja-style {{ expr }} template rendering."""

    def test_no_template(self) -> None:
        assert _render_template("plain text", {}) == "plain text"

    def test_single_expression_returns_raw(self) -> None:
        steps = {"a": {"count": 42}}
        result = _render_template("{{ steps['a']['count'] }}", steps)
        assert result == 42
        assert isinstance(result, int)

    def test_mixed_text_and_expression(self) -> None:
        steps = {"name": "test"}
        result = _render_template("Hello {{ steps['name'] }}!", steps)
        assert result == "Hello test!"

    def test_multiple_expressions(self) -> None:
        steps = {"a": 1, "b": 2}
        result = _render_template("{{ steps['a'] }} + {{ steps['b'] }}", steps)
        assert result == "1 + 2"

    def test_non_string_passthrough(self) -> None:
        assert _render_template(42, {}) == 42
        assert _render_template(None, {}) is None

    def test_extra_vars(self) -> None:
        result = _render_template("{{ x + y }}", {}, extra_vars={"x": 3, "y": 4})
        assert result == 7

    def test_empty_string(self) -> None:
        assert _render_template("", {}) == ""

    def test_nested_dict_access(self) -> None:
        steps = {"fetch": {"results": [{"symbol": "AAPL"}]}}
        result = _render_template("{{ steps['fetch']['results'][0]['symbol'] }}", steps)
        assert result == "AAPL"
