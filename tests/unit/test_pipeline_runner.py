"""Unit tests for pipeline_runner template engine and safe_eval."""

from __future__ import annotations

# pipeline_runner imports vault_env_loader at module level; stub it early
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.modules.setdefault("vault_env_loader", MagicMock())

from pipeline_runner import (  # noqa: E402
    MCP_ENDPOINTS,
    _exec_code_step,
    _exec_parallel_tool_calls,
    _exec_parallel_workflows,
    _mcp_call_async,
    _render_template,
    _safe_eval,
    mcp_call,
    run_workflow,
)

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


# ── MCP dispatch ──────────────────────────────────────────────────


class TestMcpEndpoints:
    def test_timesfm_mcp_present(self) -> None:
        assert "timesfm-mcp" in MCP_ENDPOINTS
        assert MCP_ENDPOINTS["timesfm-mcp"] == "http://timesfm-mcp:8012"

    @pytest.mark.asyncio
    async def test_unknown_mcp_returns_error(self) -> None:
        result = await _mcp_call_async("nonexistent-mcp", "ping", {})
        assert result == {"error": "Unknown MCP: nonexistent-mcp"}

    @pytest.mark.asyncio
    async def test_mcp_dispatch_resolves_endpoint(self) -> None:
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        result = await _mcp_call_async(
            "timesfm-mcp",
            "forecast",
            {"symbols": ["AAPL"]},
            client=mock_client,
        )

        assert result == {"ok": True}
        mock_client.post.assert_awaited_once_with(
            "http://timesfm-mcp:8012/tool/forecast",
            json={"symbols": ["AAPL"]},
        )

    @patch("pipeline_runner.asyncio.run")
    def test_mcp_call_sync_wrapper(self, mock_run: MagicMock) -> None:
        mock_run.return_value = {"data": 1}
        result = mcp_call("polygon-mcp", "quote", {"symbol": "AAPL"})
        assert result == {"data": 1}
        mock_run.assert_called_once()


class TestParallelToolCalls:
    @patch("pipeline_runner.asyncio.gather", new_callable=MagicMock)
    @patch("pipeline_runner._new_http_client")
    @patch("pipeline_runner._mcp_call_async", new_callable=MagicMock)
    def test_dispatches_all_tools(
        self,
        mock_mcp: MagicMock,
        mock_client_ctx: MagicMock,
        mock_gather: MagicMock,
    ) -> None:
        async def _fake_gather(*tasks: object, return_exceptions: bool = False) -> list[object]:
            assert return_exceptions is True
            assert len(tasks) == 2
            return [{"a": 1}, {"b": 2}]

        mock_gather.side_effect = _fake_gather
        mock_client_ctx.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_client_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        step = {
            "calls": [
                {"tool": "polygon-mcp", "method": "quote", "params": {}},
                {"tool": "timesfm-mcp", "method": "forecast", "params": {}},
            ]
        }
        results = _exec_parallel_tool_calls(step, {})
        assert results == [{"a": 1}, {"b": 2}]


class TestParallelWorkflows:
    @patch("pipeline_runner.run_workflow")
    @patch("pipeline_runner.concurrent.futures.ThreadPoolExecutor")
    def test_uses_thread_pool(
        self,
        mock_executor_cls: MagicMock,
        mock_run_workflow: MagicMock,
        tmp_path,
    ) -> None:
        workflows_dir = tmp_path / "wf"
        workflows_dir.mkdir()
        mock_run_workflow.side_effect = [{"ok": 1}, {"ok": 2}]

        mock_future_a = MagicMock()
        mock_future_a.result.return_value = ("a.yaml", {"ok": 1})
        mock_future_b = MagicMock()
        mock_future_b.result.return_value = ("b.yaml", {"ok": 2})
        mock_pool = MagicMock()
        mock_pool.__enter__.return_value = mock_pool
        mock_pool.__exit__.return_value = False
        mock_pool.submit.side_effect = [mock_future_a, mock_future_b]
        mock_executor_cls.return_value = mock_pool

        with patch(
            "pipeline_runner.concurrent.futures.as_completed", return_value=[mock_future_a, mock_future_b]
        ):
            result = _exec_parallel_workflows(
                {"workflows": ["a.yaml", "b.yaml"]},
                workflows_dir,
                {},
                {},
            )

        assert mock_executor_cls.called
        assert result["a.yaml"] == {"ok": 1}
        assert result["b.yaml"] == {"ok": 2}


class TestWorkflowOrchestration:
    def test_sequential_code_steps_chain(self, tmp_path) -> None:
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        wf_file = wf_dir / "chain.yaml"
        wf_file.write_text(
            """
name: chain-test
steps:
  - name: step-one
    type: code
    code: |
      result = 1
  - name: step-two
    type: code
    code: |
      result = steps['step-one'] + 2
  - name: step-three
    type: code
    code: |
      result = steps['step-two'] * 3
""".strip()
        )
        results = run_workflow(wf_file)
        assert results["step-one"] == 1
        assert results["step-two"] == 3
        assert results["step-three"] == 9

    @patch(
        "pipeline_runner._execute_step",
        side_effect=[RuntimeError("boom"), {"recovered": True}],
    )
    def test_abort_on_failure_stops_subsequent_steps(self, _mock_exec: MagicMock, tmp_path) -> None:
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        wf_file = wf_dir / "abort.yaml"
        wf_file.write_text(
            """
name: abort-test
error_handling:
  abort_on_failure: true
steps:
  - name: fail-step
    type: code
    code: "result = 1"
  - name: skipped-step
    type: code
    code: "result = 2"
  - name: on-failure-handler
    type: code
    run_on: failure
    code: "result = 'handled'"
""".strip()
        )
        results = run_workflow(wf_file)
        assert results["fail-step"] is None
        assert "skipped-step" not in results
        assert results.get("on-failure-handler") == {"recovered": True}

    @patch("pipeline_runner._execute_step", side_effect=[RuntimeError("boom"), {"ok": True}])
    def test_continue_on_failure_runs_later_steps(self, _mock_exec: MagicMock, tmp_path) -> None:
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        wf_file = wf_dir / "continue.yaml"
        wf_file.write_text(
            """
name: continue-test
error_handling:
  abort_on_failure: false
steps:
  - name: fail-step
    type: code
    code: "result = 1"
  - name: next-step
    type: code
    code: "result = 2"
""".strip()
        )
        results = run_workflow(wf_file)
        assert results["fail-step"] is None
        assert results["next-step"] == {"ok": True}


class TestMcpErrorHandling:
    @patch("pipeline_runner.mcp_call")
    def test_tool_call_skip_strategy(self, mock_mcp: MagicMock, tmp_path) -> None:
        mock_mcp.return_value = {"error": "timeout"}
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        wf_file = wf_dir / "mcp-skip.yaml"
        wf_file.write_text(
            """
name: mcp-skip-test
error_handling:
  on_mcp_error:
    rot-mcp:
      strategy: skip
steps:
  - name: call-rot
    type: tool_call
    tool: rot-mcp
    method: trending_tickers
    params: {}
""".strip()
        )
        results = run_workflow(wf_file)
        assert results["call-rot"] == {}

    def test_parallel_tool_calls_renders_dynamic_calls(self, tmp_path) -> None:
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        wf_file = wf_dir / "dynamic-calls.yaml"
        wf_file.write_text(
            """
name: dynamic-calls-test
steps:
  - name: build-calls
    type: code
    code: |
      result = {"calls": [{"tool": "spamshield-mcp", "method": "classify_text", "params": {"text": "hi"}}]}
  - name: run-calls
    type: parallel_tool_calls
    calls: "{{ steps['build-calls']['calls'] }}"
""".strip()
        )
        with patch(
            "pipeline_runner._mcp_call_async",
            new_callable=AsyncMock,
            return_value={"is_spam": False},
        ) as mock_mcp:
            results = run_workflow(wf_file)
        assert mock_mcp.called
        assert results["run-calls"] == [{"is_spam": False}]


# ── _exec_code_step import allowlist ────────────────────────────


class TestExecCodeStepImportAllowlist:
    """Workflow code blocks may only import whitelisted modules."""

    @pytest.mark.parametrize(
        "import_stmt",
        [
            "import gates.regime",
            "import decision_helpers",
            "import json",
        ],
    )
    def test_allowed_project_imports(self, import_stmt: str) -> None:
        code = f"{import_stmt}\nresult = True"
        assert _exec_code_step(code, {}, {}) is True

    def test_blocked_os_import_raises(self) -> None:
        with pytest.raises(ImportError, match="not allowed"):
            _exec_code_step("import os\nresult = True", {}, {})

    def test_blocked_getattr_escape(self) -> None:
        with pytest.raises((ImportError, NameError, AttributeError, RuntimeError)):
            _exec_code_step(
                "g = getattr\nresult = g((), '__class__')",
                {},
                {},
            )
