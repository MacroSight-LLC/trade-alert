"""YAML workflow runner for trade-alert pipelines.

Interprets the step-based YAML DSL used by all workflow files
(collectors, decisions, orchestrators, notifier, outcome-tracker).
Called by cron or manually via CLI.

Supported step types:
    code, tool_call, parallel_tool_calls, llm,
    parallel, workflow, conditional

Usage:
    python pipeline_runner.py workflows/orchestrator-15m.yaml
    python pipeline_runner.py workflows/outcome-tracker.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, cast

import httpx
import yaml

import vault_env_loader  # noqa: F401  — seeds os.environ from Vault
from llm_client import llm_call as _llm_call
from log_config import configure_logging
from metrics import (
    MCP_CALL_DURATION,
    MCP_CIRCUIT_BREAKER_TRIPS,
    PIPELINE_LAST_RUN,
    PIPELINE_RUNS,
)
from resilience.circuit_breaker import CircuitBreakerRegistry
from workflow_sandbox import exec_code_step as _exec_code_step
from workflow_template import render_params as _render_params
from workflow_template import render_template as _render_template
from mcp.registry import get_endpoint, get_registry, register_workflow_overrides
from workflow_template import safe_eval as _safe_eval  # noqa: F401 — re-export for tests

configure_logging()
logger = logging.getLogger("pipeline_runner")

# Backward-compatible re-export (same dict as mcp.registry singleton).
MCP_ENDPOINTS: dict[str, str] = get_registry().endpoints

MCP_TIMEOUT = float(os.environ.get("MCP_TIMEOUT", "150"))
if MCP_TIMEOUT <= 0:
    logger.warning("MCP_TIMEOUT=%s invalid, falling back to 150s", MCP_TIMEOUT)
    MCP_TIMEOUT = 150.0


def _new_http_client() -> httpx.AsyncClient:
    """Create an async HTTP client with optimized connection pooling."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=MCP_TIMEOUT, write=5.0, pool=5.0),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )


# ── MCP call helper ──────────────────────────────────────────────────

# Simple in-memory circuit breaker per MCP endpoint (SSOT §0.1).
_CIRCUIT_FAILURE_THRESHOLD: int = 3
_CIRCUIT_OPEN_DURATION: float = 300.0  # seconds

_mcp_circuit_breaker = CircuitBreakerRegistry(
    failure_threshold=_CIRCUIT_FAILURE_THRESHOLD,
    open_duration=_CIRCUIT_OPEN_DURATION,
)


async def _mcp_call_async(
    tool: str, method: str, params: dict[str, Any], client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """Call an MCP server tool endpoint with circuit-breaker protection."""
    if _mcp_circuit_breaker.is_open(tool):
        logger.warning(
            "Circuit open for %s — failing fast (resets in %.0fs)",
            tool,
            _mcp_circuit_breaker.seconds_until_close(tool),
        )
        return {"error": f"Circuit open for {tool}", "circuit_open": True}

    base = get_endpoint(tool)
    if not base:
        logger.error("Unknown MCP tool: %s", tool)
        return {"error": f"Unknown MCP: {tool}"}

    url = f"{base}/tool/{method}"
    _mcp_start = time.monotonic()
    try:
        if client:
            resp = await client.post(url, json=params)
        else:
            async with _new_http_client() as c:
                resp = await c.post(url, json=params)
        resp.raise_for_status()
        MCP_CALL_DURATION.labels(tool=tool, method=method).observe(time.monotonic() - _mcp_start)
        _mcp_circuit_breaker.record_success(tool)
        return cast(dict[str, Any], resp.json())
    except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as exc:
        MCP_CALL_DURATION.labels(tool=tool, method=method).observe(time.monotonic() - _mcp_start)
        opened = _mcp_circuit_breaker.record_failure(tool)
        if opened:
            MCP_CIRCUIT_BREAKER_TRIPS.labels(endpoint=tool).inc()
            logger.error(
                "Circuit OPEN for %s after %d consecutive failures (%.0fs cooldown): %s",
                tool,
                _CIRCUIT_FAILURE_THRESHOLD,
                _CIRCUIT_OPEN_DURATION,
                exc,
            )
        else:
            logger.warning("MCP call failed for %s: %s", tool, exc)
        return {"error": str(exc)}


def mcp_call(tool: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Synchronous wrapper for MCP calls (available inside code blocks)."""
    return asyncio.run(_mcp_call_async(tool, method, params or {}))


# ── Step executors ───────────────────────────────────────────────────


def _get_redis_client() -> Any:
    """Lazy Redis client for MCP fallback_to_cache strategies."""
    from redis_client import get_redis

    return get_redis()


def _exec_tool_call(
    step: dict[str, Any],
    steps: dict[str, Any],
    extra_vars: dict[str, Any] | None = None,
    error_cfg: dict[str, Any] | None = None,
) -> Any:
    """Execute a single MCP tool_call step."""
    tool = step["tool"]
    method = step["method"]
    params = _render_params(step.get("params", {}), steps, extra_vars)
    result = mcp_call(tool, method, params)
    if error_cfg:
        from resilience.mcp_error_handler import apply_mcp_error_strategy

        result = apply_mcp_error_strategy(
            tool,
            method,
            result,
            error_cfg,
            get_redis=_get_redis_client,
        )
    return result


def _exec_parallel_tool_calls(
    step: dict[str, Any],
    steps: dict[str, Any],
    extra_vars: dict[str, Any] | None = None,
    error_cfg: dict[str, Any] | None = None,
) -> list[Any]:
    """Execute multiple MCP calls concurrently with connection pooling."""
    calls = _render_params(step["calls"], steps, extra_vars)
    if not calls:
        return []
    if not isinstance(calls, list):
        raise TypeError(f"parallel_tool_calls 'calls' must be a list, got {type(calls).__name__}")
    results: list[Any] = [None] * len(calls)

    async def _run() -> list[Any]:
        async with _new_http_client() as client:
            tasks = []
            for call in calls:
                tool = call["tool"]
                method = call["method"]
                params = _render_params(call.get("params", {}), steps, extra_vars)
                tasks.append(_mcp_call_async(tool, method, params, client=client))
            return cast(list[Any], await asyncio.gather(*tasks, return_exceptions=True))

    raw = asyncio.run(_run())
    for i, r in enumerate(raw):
        if isinstance(r, Exception):
            call_info = calls[i] if i < len(calls) else {}
            logger.warning(
                "Parallel tool call %d failed: tool=%s method=%s error=%s",
                i,
                call_info.get("tool", "?"),
                call_info.get("method", "?"),
                r,
            )
            results[i] = {"error": str(r)}
        else:
            results[i] = r

    if error_cfg:
        from resilience.mcp_error_handler import apply_mcp_error_strategy

        for i, r in enumerate(results):
            if isinstance(r, dict) and "error" in r:
                call_info = calls[i] if i < len(calls) else {}
                results[i] = apply_mcp_error_strategy(
                    call_info.get("tool", "?"),
                    call_info.get("method", "?"),
                    r,
                    error_cfg,
                    get_redis=_get_redis_client,
                )
    return results


def _exec_llm_step(
    step: dict[str, Any],
    steps: dict[str, Any],
    model: str,
    extra_vars: dict[str, Any] | None = None,
    *,
    trace_id: str | None = None,
    step_name: str = "llm-call",
) -> str:
    """Execute an LLM step with Langfuse generation tracking."""
    prompt = _render_template(step["prompt"], steps, extra_vars)
    return _llm_call(
        str(prompt),
        model,
        trace_id=trace_id,
        step_name=step_name,
    )


# ── Workflow runner ──────────────────────────────────────────────────


def run_workflow(
    workflow_path: Path,
    inputs: dict[str, Any] | None = None,
    parent_steps: dict[str, Any] | None = None,
    *,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Execute a YAML workflow file and return all step results.

    Args:
        workflow_path: Path to the YAML workflow file.
        inputs: Named variables passed from a parent workflow's
            ``type: workflow`` step with ``inputs:``.
        parent_steps: Step results from the calling workflow (for
            template expressions in inputs).
        trace_id: Langfuse trace ID — propagated to all child steps
            so LLM generations and spans are linked to the root trace.

    Returns:
        Dict mapping step names to their results.
    """
    with open(workflow_path) as fh:
        wf = yaml.safe_load(fh)

    name = wf.get("name", workflow_path.stem)
    model = os.getenv("LLM_MODEL") or wf.get("llm_model", "claude-sonnet-4-5")
    error_cfg = wf.get("error_handling", {})
    max_attempts = error_cfg.get("retry", {}).get("max_attempts", 1)
    backoff = error_cfg.get("retry", {}).get("backoff_seconds", 0)

    # Register workflow-level MCP endpoint overrides.
    # Environment variables (e.g. TRADINGVIEW_MCP_URL) take precedence over
    # YAML-hardcoded Docker hostnames so the pipeline works from the host.
    register_workflow_overrides(wf.get("mcp_servers", []))

    wf_steps: list[dict[str, Any]] = wf.get("steps", [])
    step_results: dict[str, Any] = {}
    step_timings: dict[str, float] = {}
    extra_vars: dict[str, Any] = dict(inputs or {})
    workflow_failed = False

    logger.info("▶ Starting workflow: %s (%s)", name, workflow_path.name)

    abort_main_loop = False
    for step_def in wf_steps:
        if abort_main_loop:
            break
        step_name = step_def.get("name", "unnamed")
        step_type = step_def.get("type", "code")
        run_on = step_def.get("run_on")

        # Failure handlers run once after the main loop (never inline).
        if run_on == "failure":
            continue

        logger.info("  ├─ step: %s (type=%s)", step_name, step_type)

        for attempt in range(1, max_attempts + 1):
            try:
                t0 = time.time()
                # Resolve trace_id: from init-trace step or passed from parent
                active_trace_id = trace_id
                if active_trace_id is None:
                    try:
                        active_trace_id = step_results.get("init-trace", {}).get("trace_id")
                    except (AttributeError, TypeError):
                        pass
                result = _execute_step(
                    step_def,
                    step_results,
                    model,
                    workflow_path.parent,
                    extra_vars,
                    trace_id=active_trace_id,
                    error_cfg=error_cfg,
                )
                step_results[step_name] = result
                step_elapsed = time.time() - t0
                step_timings[step_name] = step_elapsed
                logger.info("  │  ✓ %s completed (%.1fs)", step_name, step_elapsed)
                break
            except Exception as exc:
                logger.error(
                    "  │  FAIL step=%s attempt=%d/%d: %s\n%s",
                    step_name,
                    attempt,
                    max_attempts,
                    exc,
                    traceback.format_exc(),
                )
                if attempt < max_attempts:
                    time.sleep(backoff)
                else:
                    step_results[step_name] = None
                    workflow_failed = True
                    if error_cfg.get("abort_on_failure", False):
                        abort_main_loop = True
                        break

    # Run failure handlers if workflow failed
    if workflow_failed:
        for step_def in wf_steps:
            if step_def.get("run_on") == "failure":
                step_name = step_def.get("name", "unnamed")
                logger.info("  ├─ step (on-failure): %s", step_name)
                try:
                    active_trace_id = trace_id
                    if active_trace_id is None:
                        try:
                            active_trace_id = step_results.get("init-trace", {}).get("trace_id")
                        except (AttributeError, TypeError):
                            pass
                    result = _execute_step(
                        step_def,
                        step_results,
                        model,
                        workflow_path.parent,
                        extra_vars,
                        trace_id=active_trace_id,
                        error_cfg=error_cfg,
                    )
                    step_results[step_name] = result
                except Exception as exc:
                    logger.error("  │  on-failure step %s also failed: %s", step_name, exc)

    logger.info("■ Finished workflow: %s (failed=%s)", name, workflow_failed)

    # Prometheus metrics
    _wf_label = workflow_path.stem
    PIPELINE_RUNS.labels(workflow=_wf_label, status="failure" if workflow_failed else "success").inc()
    PIPELINE_LAST_RUN.labels(workflow=_wf_label).set_to_current_time()
    try:
        from redis_client import get_redis

        get_redis().set("pipeline:last_run_ts", str(time.time()))
    except Exception:  # noqa: BLE001
        pass

    # Latency breakdown summary
    if step_timings:
        sorted_steps = sorted(step_timings.items(), key=lambda x: x[1], reverse=True)
        total = sum(step_timings.values())
        parts = " | ".join(f"{n}={t:.1f}s" for n, t in sorted_steps[:5])
        logger.info("  ⏱ Latency breakdown (top 5): %s | total=%.1fs", parts, total)

    return step_results


def _execute_step(
    step_def: dict[str, Any],
    steps: dict[str, Any],
    model: str,
    workflows_dir: Path,
    extra_vars: dict[str, Any],
    *,
    trace_id: str | None = None,
    error_cfg: dict[str, Any] | None = None,
) -> Any:
    """Dispatch and execute a single step by type."""
    step_type = step_def.get("type", "code")
    step_name = step_def.get("name", "unnamed")

    if step_type == "code":
        # Inject trace_id so code blocks can access it for Langfuse scoring
        code_extra = dict(extra_vars) if extra_vars else {}
        if trace_id:
            steps_with_trace = {**steps, "__trace_id__": trace_id}
        else:
            steps_with_trace = steps
        return _exec_code_step(step_def["code"], steps_with_trace, code_extra)

    if step_type == "tool_call":
        return _exec_tool_call(step_def, steps, extra_vars, error_cfg=error_cfg)

    if step_type == "parallel_tool_calls":
        return _exec_parallel_tool_calls(step_def, steps, extra_vars, error_cfg=error_cfg)

    if step_type == "llm":
        return _exec_llm_step(
            step_def,
            steps,
            model,
            extra_vars,
            trace_id=trace_id,
            step_name=step_name,
        )

    if step_type == "workflow":
        workflow_file = _render_params(step_def["workflow"], steps, extra_vars)
        sub_path = workflows_dir / str(workflow_file)
        # Render inputs from parent step context
        sub_inputs: dict[str, Any] = {}
        for k, v in step_def.get("inputs", {}).items():
            sub_inputs[k] = _render_params(v, steps, extra_vars)
        return run_workflow(
            sub_path,
            inputs=sub_inputs,
            parent_steps=steps,
            trace_id=trace_id,
        )

    if step_type == "parallel":
        return _exec_parallel_workflows(
            step_def,
            workflows_dir,
            steps,
            extra_vars,
            trace_id=trace_id,
        )

    if step_type == "conditional":
        return _exec_conditional(
            step_def,
            steps,
            model,
            workflows_dir,
            extra_vars,
            trace_id=trace_id,
            step_name=step_name,
            error_cfg=error_cfg,
        )

    logger.warning("Unknown step type: %s — skipping", step_type)
    return None


def _exec_parallel_workflows(
    step_def: dict[str, Any],
    workflows_dir: Path,
    steps: dict[str, Any],
    extra_vars: dict[str, Any],
    *,
    trace_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Run multiple sub-workflows concurrently via thread pool."""
    workflow_files = step_def.get("workflows", [])
    if not workflow_files:
        return {}
    abort_on_failure = step_def.get("abort_on_failure", False)
    results: dict[str, dict[str, Any]] = {}

    def _run_one(wf_file: str) -> tuple[str, dict[str, Any] | None]:
        path = workflows_dir / wf_file
        try:
            return wf_file, run_workflow(path, trace_id=trace_id)
        except Exception as exc:
            logger.error("Parallel workflow %s failed: %s\n%s", wf_file, exc, traceback.format_exc())
            if abort_on_failure:
                raise
            return wf_file, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(workflow_files)) as pool:
        futures = {pool.submit(_run_one, wf): wf for wf in workflow_files}
        for future in concurrent.futures.as_completed(futures):
            wf_file, wf_result = future.result()
            results[wf_file] = wf_result  # type: ignore[assignment]

    return results


def _exec_conditional(
    step_def: dict[str, Any],
    steps: dict[str, Any],
    model: str,
    workflows_dir: Path,
    extra_vars: dict[str, Any],
    *,
    trace_id: str | None = None,
    step_name: str = "conditional",
    error_cfg: dict[str, Any] | None = None,
) -> Any:
    """Evaluate a conditional and execute the matching branch."""
    condition = _render_template(step_def["condition"], steps, extra_vars)
    branch = step_def.get("if_true") if condition else step_def.get("if_false")
    if branch is None:
        return None
    # Propagate step_name into nested branch so LLM steps get the parent name
    if "name" not in branch:
        branch = {**branch, "name": step_name}
    return _execute_step(
        branch,
        steps,
        model,
        workflows_dir,
        extra_vars,
        trace_id=trace_id,
        error_cfg=error_cfg,
    )


# ── CLI entrypoint ───────────────────────────────────────────────────


def main() -> None:
    """CLI entrypoint for the pipeline runner."""
    parser = argparse.ArgumentParser(description="Trade-alert YAML workflow runner")
    parser.add_argument("workflow", type=Path, help="Path to YAML workflow file")
    args = parser.parse_args()

    wf_path = Path(args.workflow)
    if not wf_path.exists():
        logger.error("Workflow file not found: %s", wf_path)
        sys.exit(1)

    t0 = time.time()
    try:
        run_workflow(wf_path)
    except Exception as exc:
        logger.error("Workflow failed: %s\n%s", exc, traceback.format_exc())
        sys.exit(1)
    finally:
        elapsed = time.time() - t0
        logger.info("Total pipeline time: %.1fs", elapsed)


if __name__ == "__main__":
    main()
