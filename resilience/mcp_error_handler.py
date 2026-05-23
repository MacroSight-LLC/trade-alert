"""MCP error strategy dispatcher — implements on_mcp_error YAML config."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

STRATEGIES = frozenset({"skip", "fail_open", "fallback_to_cache", "continue_partial"})


def _resolve_strategy_config(
    tool: str,
    on_mcp_error: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Return (strategy, merged config dict) for a tool error."""
    tool_cfg = on_mcp_error.get(tool)
    if isinstance(tool_cfg, str):
        return tool_cfg, {}
    if isinstance(tool_cfg, dict):
        strategy = tool_cfg.get("strategy") or on_mcp_error.get("strategy") or "continue_partial"
        return strategy, tool_cfg

    workflow_strategy = on_mcp_error.get("strategy")
    if workflow_strategy:
        return workflow_strategy, on_mcp_error

    return "continue_partial", {}


def apply_mcp_error_strategy(
    tool: str,
    method: str,
    raw_result: Any,
    error_cfg: dict[str, Any],
    *,
    get_redis: Callable[[], Any] | None = None,
) -> Any:
    """Apply on_mcp_error strategy from workflow error_handling config."""
    if not isinstance(raw_result, dict) or "error" not in raw_result:
        return raw_result

    on_mcp_error: dict[str, Any] = error_cfg.get("on_mcp_error") or {}
    strategy, cfg = _resolve_strategy_config(tool, on_mcp_error)
    if strategy not in STRATEGIES:
        strategy = "continue_partial"

    log_msg = cfg.get("message")
    error_detail = raw_result.get("error", "unknown error")
    suffix = f" | {log_msg}" if log_msg else ""

    if strategy in ("skip", "fail_open"):
        logger.warning(
            "MCP %s.%s error — strategy=%s: %s%s",
            tool,
            method,
            strategy,
            error_detail,
            suffix,
        )
        return {}

    if strategy == "fallback_to_cache":
        fallback_key = (
            cfg.get("fallback_key")
            or cfg.get("fallback_equities_key")
            or on_mcp_error.get("fallback_key")
            or on_mcp_error.get("fallback_equities_key")
        )
        hardcoded = cfg.get("hardcoded_fallback") or on_mcp_error.get("hardcoded_fallback")

        if get_redis is not None and fallback_key:
            try:
                r = get_redis()
                cached = r.get(fallback_key)
                if cached:
                    logger.info(
                        "MCP %s.%s error — strategy=fallback_to_cache: using %s",
                        tool,
                        method,
                        fallback_key,
                    )
                    if isinstance(cached, (bytes, bytearray)):
                        cached = cached.decode("utf-8")
                    if isinstance(cached, str):
                        try:
                            return json.loads(cached)
                        except json.JSONDecodeError:
                            return {"value": cached}
                    return cached
            except Exception as exc:
                logger.warning("fallback_to_cache failed for %s: %s", fallback_key, exc)

        if hardcoded is not None:
            logger.info(
                "MCP %s.%s error — strategy=fallback_to_cache: using hardcoded_fallback",
                tool,
                method,
            )
            return hardcoded

        logger.warning(
            "MCP %s.%s error — strategy=fallback_to_cache: no cache available, returning {}",
            tool,
            method,
        )
        return {}

    logger.warning(
        "MCP %s.%s error — strategy=continue_partial: %s%s",
        tool,
        method,
        error_detail,
        suffix,
    )
    return raw_result
