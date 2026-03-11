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
import json
import logging
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import httpx
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("pipeline_runner")

# MCP server endpoint mapping — matches docker-compose.prod.yml
MCP_ENDPOINTS: dict[str, str] = {
    "tradingview-mcp": os.getenv("TRADINGVIEW_MCP_URL", "http://tradingview-mcp:8001"),
    "polygon-mcp": os.getenv("POLYGON_MCP_URL", "http://polygon-mcp:8002"),
    "discord-mcp": os.getenv("DISCORD_MCP_URL", "http://discord-mcp:8003"),
    "finnhub-mcp": os.getenv("FINNHUB_MCP_URL", "http://finnhub-mcp:8004"),
    "rot-mcp": os.getenv("ROT_MCP_URL", "http://rot-mcp:8005"),
    "crypto-orderbook-mcp": os.getenv("CRYPTO_ORDERBOOK_MCP_URL", "http://crypto-orderbook-mcp:8006"),
    "coingecko-mcp": os.getenv("COINGECKO_MCP_URL", "http://coingecko-mcp:8007"),
    "trading-mcp": os.getenv("TRADING_MCP_URL", "http://trading-mcp:8008"),
    "fred-mcp": os.getenv("FRED_MCP_URL", "http://fred-mcp:8009"),
    "spamshield-mcp": os.getenv("SPAMSHIELD_MCP_URL", "http://spamshield-mcp:8010"),
}

# Also accept workflow-level mcp_servers overrides
_workflow_mcp_endpoints: dict[str, str] = {}

MCP_TIMEOUT = 15.0


# ── Template evaluation ──────────────────────────────────────────────

_TEMPLATE_RE = re.compile(r"\{\{(.+?)\}\}", re.DOTALL)


def _render_template(template: str, steps: dict[str, Any], extra_vars: dict[str, Any] | None = None) -> Any:
    """Evaluate ``{{ expr }}`` Jinja-style template expressions.

    If the entire string is a single expression, returns the raw Python
    value (not stringified).  Mixed text+expression strings are returned
    as concatenated strings.
    """
    if not isinstance(template, str):
        return template

    matches = list(_TEMPLATE_RE.finditer(template))
    if not matches:
        return template

    ns: dict[str, Any] = {"steps": steps}
    if extra_vars:
        ns.update(extra_vars)

    # Single expression spanning the full string → return raw value
    if len(matches) == 1 and matches[0].start() == 0 and matches[0].end() == len(template.strip()):
        return eval(matches[0].group(1).strip(), {"__builtins__": {}}, ns)  # noqa: S307

    # Multiple/mixed → string interpolation
    result = template
    for m in reversed(matches):
        val = eval(m.group(1).strip(), {"__builtins__": {}}, ns)  # noqa: S307
        result = result[: m.start()] + str(val) + result[m.end() :]
    return result


def _render_params(params: Any, steps: dict[str, Any], extra_vars: dict[str, Any] | None = None) -> Any:
    """Recursively render template expressions in params dicts/lists."""
    if isinstance(params, str):
        return _render_template(params, steps, extra_vars)
    if isinstance(params, dict):
        return {k: _render_params(v, steps, extra_vars) for k, v in params.items()}
    if isinstance(params, list):
        return [_render_params(v, steps, extra_vars) for v in params]
    return params


# ── MCP call helper ──────────────────────────────────────────────────

async def _mcp_call_async(tool: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Call an MCP server tool endpoint."""
    base = _workflow_mcp_endpoints.get(tool) or MCP_ENDPOINTS.get(tool)
    if not base:
        logger.error("Unknown MCP tool: %s", tool)
        return {"error": f"Unknown MCP: {tool}"}

    url = f"{base}/tool/{method}"
    async with httpx.AsyncClient(timeout=MCP_TIMEOUT) as client:
        resp = await client.post(url, json=params)
        resp.raise_for_status()
        return resp.json()


def mcp_call(tool: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Synchronous wrapper for MCP calls (available inside code blocks)."""
    return asyncio.get_event_loop().run_until_complete(
        _mcp_call_async(tool, method, params or {})
    )


# ── LLM call helper ─────────────────────────────────────────────────

def _llm_call(prompt: str, model: str) -> str:
    """Call the LLM via litellm.completion."""
    import litellm

    # Parse SYSTEM: ... USER: ... format if present
    system_msg = ""
    user_msg = prompt
    if prompt.startswith("SYSTEM:"):
        parts = prompt.split("\n\nUSER:\n", 1)
        if len(parts) == 2:
            system_msg = parts[0].removeprefix("SYSTEM:").strip()
            user_msg = parts[1].strip()

    messages: list[dict[str, str]] = []
    if system_msg:
        messages.append({"role": "system", "content": system_msg})
    messages.append({"role": "user", "content": user_msg})

    response = litellm.completion(
        model=f"anthropic/{model}" if "/" not in model else model,
        messages=messages,
        max_tokens=4096,
        temperature=0.2,
    )
    return response.choices[0].message.content


# ── Step executors ───────────────────────────────────────────────────

def _exec_code_step(
    code: str,
    steps: dict[str, Any],
    extra_vars: dict[str, Any] | None = None,
) -> Any:
    """Execute an inline Python code block.

    The code block can set ``result`` to return a value. It has access
    to ``steps`` (prior results) and ``mcp_call`` function.
    """
    local_ns: dict[str, Any] = {
        "steps": steps,
        "mcp_call": mcp_call,
    }
    if extra_vars:
        local_ns.update(extra_vars)

    exec(compile(code, "<workflow-code>", "exec"), local_ns)  # noqa: S102
    return local_ns.get("result")


def _exec_tool_call(
    step: dict[str, Any],
    steps: dict[str, Any],
    extra_vars: dict[str, Any] | None = None,
) -> Any:
    """Execute a single MCP tool_call step."""
    tool = step["tool"]
    method = step["method"]
    params = _render_params(step.get("params", {}), steps, extra_vars)
    return mcp_call(tool, method, params)


def _exec_parallel_tool_calls(
    step: dict[str, Any],
    steps: dict[str, Any],
    extra_vars: dict[str, Any] | None = None,
) -> list[Any]:
    """Execute multiple MCP calls concurrently."""
    calls = step["calls"]
    results: list[Any] = [None] * len(calls)

    async def _run() -> list[Any]:
        tasks = []
        for call in calls:
            tool = call["tool"]
            method = call["method"]
            params = _render_params(call.get("params", {}), steps, extra_vars)
            tasks.append(_mcp_call_async(tool, method, params))
        return await asyncio.gather(*tasks, return_exceptions=True)

    raw = asyncio.get_event_loop().run_until_complete(_run())
    for i, r in enumerate(raw):
        if isinstance(r, Exception):
            logger.warning("Parallel tool call %d failed: %s", i, r)
            results[i] = {"error": str(r)}
        else:
            results[i] = r
    return results


def _exec_llm_step(
    step: dict[str, Any],
    steps: dict[str, Any],
    model: str,
    extra_vars: dict[str, Any] | None = None,
) -> str:
    """Execute an LLM step."""
    prompt = _render_template(step["prompt"], steps, extra_vars)
    return _llm_call(str(prompt), model)


# ── Workflow runner ──────────────────────────────────────────────────

def run_workflow(
    workflow_path: Path,
    inputs: dict[str, Any] | None = None,
    parent_steps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a YAML workflow file and return all step results.

    Args:
        workflow_path: Path to the YAML workflow file.
        inputs: Named variables passed from a parent workflow's
            ``type: workflow`` step with ``inputs:``.
        parent_steps: Step results from the calling workflow (for
            template expressions in inputs).

    Returns:
        Dict mapping step names to their results.
    """
    with open(workflow_path) as fh:
        wf = yaml.safe_load(fh)

    name = wf.get("name", workflow_path.stem)
    model = wf.get("llm_model", "claude-sonnet-4-20250514")
    error_cfg = wf.get("error_handling", {})
    max_attempts = error_cfg.get("retry", {}).get("max_attempts", 1)
    backoff = error_cfg.get("retry", {}).get("backoff_seconds", 0)

    # Register workflow-level MCP endpoint overrides
    for srv in wf.get("mcp_servers", []):
        srv_name = srv["name"]
        if "endpoint" in srv:
            _workflow_mcp_endpoints[srv_name] = srv["endpoint"]

    wf_steps: list[dict[str, Any]] = wf.get("steps", [])
    step_results: dict[str, Any] = {}
    extra_vars: dict[str, Any] = dict(inputs or {})
    workflow_failed = False

    logger.info("▶ Starting workflow: %s (%s)", name, workflow_path.name)

    for step_def in wf_steps:
        step_name = step_def.get("name", "unnamed")
        step_type = step_def.get("type", "code")
        run_on = step_def.get("run_on")

        # Skip failure-only steps unless the workflow failed
        if run_on == "failure" and not workflow_failed:
            continue
        # Skip normal steps if this is a failure-only pass
        if run_on != "failure" and workflow_failed:
            continue

        logger.info("  ├─ step: %s (type=%s)", step_name, step_type)

        for attempt in range(1, max_attempts + 1):
            try:
                result = _execute_step(
                    step_def, step_results, model, workflow_path.parent, extra_vars,
                )
                step_results[step_name] = result
                break
            except Exception:
                logger.error(
                    "  │  FAIL step=%s attempt=%d/%d\n%s",
                    step_name, attempt, max_attempts, traceback.format_exc(),
                )
                if attempt < max_attempts:
                    time.sleep(backoff)
                else:
                    step_results[step_name] = None
                    if error_cfg.get("abort_on_failure", False):
                        workflow_failed = True
                        break

    # Run failure handlers if workflow failed
    if workflow_failed:
        for step_def in wf_steps:
            if step_def.get("run_on") == "failure":
                step_name = step_def.get("name", "unnamed")
                logger.info("  ├─ step (on-failure): %s", step_name)
                try:
                    result = _execute_step(
                        step_def, step_results, model, workflow_path.parent, extra_vars,
                    )
                    step_results[step_name] = result
                except Exception:
                    logger.error("  │  on-failure step %s also failed", step_name)

    logger.info("■ Finished workflow: %s (failed=%s)", name, workflow_failed)
    return step_results


def _execute_step(
    step_def: dict[str, Any],
    steps: dict[str, Any],
    model: str,
    workflows_dir: Path,
    extra_vars: dict[str, Any],
) -> Any:
    """Dispatch and execute a single step by type."""
    step_type = step_def.get("type", "code")

    if step_type == "code":
        return _exec_code_step(step_def["code"], steps, extra_vars)

    if step_type == "tool_call":
        return _exec_tool_call(step_def, steps, extra_vars)

    if step_type == "parallel_tool_calls":
        return _exec_parallel_tool_calls(step_def, steps, extra_vars)

    if step_type == "llm":
        return _exec_llm_step(step_def, steps, model, extra_vars)

    if step_type == "workflow":
        sub_path = workflows_dir / step_def["workflow"]
        # Render inputs from parent step context
        sub_inputs: dict[str, Any] = {}
        for k, v in step_def.get("inputs", {}).items():
            sub_inputs[k] = _render_params(v, steps, extra_vars)
        return run_workflow(sub_path, inputs=sub_inputs, parent_steps=steps)

    if step_type == "parallel":
        return _exec_parallel_workflows(step_def, workflows_dir, steps, extra_vars)

    if step_type == "conditional":
        return _exec_conditional(step_def, steps, model, workflows_dir, extra_vars)

    logger.warning("Unknown step type: %s — skipping", step_type)
    return None


def _exec_parallel_workflows(
    step_def: dict[str, Any],
    workflows_dir: Path,
    steps: dict[str, Any],
    extra_vars: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Run multiple sub-workflows concurrently via thread pool."""
    workflow_files = step_def.get("workflows", [])
    abort_on_failure = step_def.get("abort_on_failure", False)
    results: dict[str, dict[str, Any]] = {}

    def _run_one(wf_file: str) -> tuple[str, dict[str, Any] | None]:
        path = workflows_dir / wf_file
        try:
            return wf_file, run_workflow(path)
        except Exception:
            logger.error("Parallel workflow %s failed:\n%s", wf_file, traceback.format_exc())
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
) -> Any:
    """Evaluate a conditional and execute the matching branch."""
    condition = _render_template(step_def["condition"], steps, extra_vars)
    branch = step_def.get("if_true") if condition else step_def.get("if_false")
    if branch is None:
        return None
    # Branch is a nested step definition
    return _execute_step(branch, steps, model, workflows_dir, extra_vars)


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

    try:
        run_workflow(wf_path)
    except Exception:
        logger.error("Workflow failed:\n%s", traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
