"""Restricted exec sandbox for YAML workflow code steps.

Workflow YAML on disk is trusted configuration; this sandbox limits accidental
damage and reduces escape surface for compromised workflow content.
"""

from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# Reserved keys that must not be supplied via workflow inputs/extra_vars.
_RESERVED_NS_KEYS: frozenset[str] = frozenset(
    {
        "__builtins__",
        "__import__",
        "steps",
        "mcp_call",
        "env_get",
        "get_redis",
        "inputs",
        "result",
    }
)

# stdlib + project modules workflow code may import.
_IMPORT_ALLOWLIST: frozenset[str] = {
    "json",
    "logging",
    "re",
    "time",
    "datetime",
    "math",
    "hashlib",
    "copy",
    "collections",
    "functools",
    "itertools",
    "pathlib",
    "textwrap",
    "uuid",
    "zoneinfo",
    "normalizers",
    "normalizers.events_normalizer",
    "normalizers.flow_normalizer",
    "normalizers.forecast_normalizer",
    "normalizers.macro_normalizer",
    "normalizers.market_normalizer",
    "normalizers.sentiment_normalizer",
    "normalizers.si_normalizer",
    "normalizers.ta_normalizer",
    "decision_helpers",
    "merger",
    "models",
    "validate_and_filter",
    "notifier_and_logger",
    "pipeline_tracing",
    "langfuse_client",
    "gates",
    "gates.regime",
    "gates.session",
    "gates.dedup",
    "gates.watch",
    "gates.rr_volume",
    "prompt_manager",
}

# Per-module symbols allowed via ``from module import symbol``.
_IMPORT_FROM_ALLOWLIST: dict[str, frozenset[str]] = {
    "decision_helpers": frozenset(
        {
            "merge_snapshots",
            "build_prompt",
            "validate_and_filter_step",
        }
    ),
    "notifier_and_logger": frozenset({"notify", "send_ops_message", "send_ops_embed"}),
    "pipeline_tracing": frozenset(
        {
            "create_pipeline_trace",
            "span_step",
            "end_pipeline_trace",
            "add_score",
            "tag_trace",
        }
    ),
    "langfuse_client": frozenset({"get_langfuse_client"}),
    "normalizers.sentiment_normalizer": frozenset({"normalize"}),
    "normalizers.ta_normalizer": frozenset({"normalize"}),
    "normalizers.market_normalizer": frozenset({"normalize"}),
    "normalizers.flow_normalizer": frozenset({"normalize"}),
    "normalizers.events_normalizer": frozenset({"normalize"}),
    "normalizers.forecast_normalizer": frozenset({"normalize"}),
    "normalizers.macro_normalizer": frozenset({"normalize"}),
    "normalizers.si_normalizer": frozenset({"normalize"}),
}

_ALLOWED_BUILTINS: frozenset[str] = frozenset(
    {
        "__name__",
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "float",
        "frozenset",
        "int",
        "isinstance",
        "issubclass",
        "len",
        "list",
        "max",
        "min",
        "range",
        "repr",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
        "True",
        "False",
        "None",
        "KeyError",
        "ValueError",
        "TypeError",
        "IndexError",
        "AttributeError",
        "Exception",
        "RuntimeError",
        "StopIteration",
        "NotImplementedError",
    }
)


def _safe_import(
    name: str,
    globals: dict[str, Any] | None = None,  # noqa: A002
    locals: dict[str, Any] | None = None,  # noqa: A002
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    """Import gate: allowlisted modules only; validate fromlist symbols."""
    if level != 0:
        msg = "Relative imports are not allowed in workflow code blocks"
        raise ImportError(msg)
    if name not in _IMPORT_ALLOWLIST:
        msg = f"Import of {name!r} is not allowed in workflow code blocks"
        raise ImportError(msg)
    module = importlib.import_module(name)
    if not fromlist:
        return module
    allowed = _IMPORT_FROM_ALLOWLIST.get(name)
    if allowed is None:
        msg = f"from {name} import ... is not allowed (import the module only)"
        raise ImportError(msg)
    for symbol in fromlist:
        if symbol not in allowed:
            msg = f"Import of {name}.{symbol} is not allowed in workflow code blocks"
            raise ImportError(msg)
    if len(fromlist) == 1:
        return getattr(module, fromlist[0])
    return tuple(getattr(module, sym) for sym in fromlist)


def _build_restricted_builtins() -> dict[str, Any]:
    import builtins as builtins_mod

    restricted = {k: getattr(builtins_mod, k) for k in _ALLOWED_BUILTINS if hasattr(builtins_mod, k)}
    restricted["__import__"] = _safe_import
    return restricted


class SandboxExecutor:
    """Execute workflow inline Python with a restricted namespace."""

    def __init__(
        self,
        *,
        env_get: Callable[[str, str | None], str | None] | None = None,
        get_redis: Callable[[], Any] | None = None,
    ) -> None:
        self._env_get = env_get or (lambda key, default=None: os.getenv(key, default))
        self._get_redis = get_redis

    def execute(
        self,
        code: str,
        steps: dict[str, Any],
        extra_vars: dict[str, Any] | None = None,
    ) -> Any:
        """Run code block; return value of ``result`` variable if set."""
        if extra_vars:
            for key in extra_vars:
                if key in _RESERVED_NS_KEYS or key.startswith("__"):
                    msg = f"Reserved or dunder variable {key!r} cannot be supplied via inputs"
                    raise ValueError(msg)

        local_ns: dict[str, Any] = {
            "steps": steps,
            "inputs": extra_vars or {},
            "env_get": self._env_get,
        }
        if self._get_redis is not None:
            local_ns["get_redis"] = self._get_redis

        if extra_vars:
            local_ns.update(extra_vars)

        local_ns["__builtins__"] = _build_restricted_builtins()

        try:
            exec(compile(code, "<workflow-code>", "exec"), local_ns)  # noqa: S102
        except (SystemExit, KeyboardInterrupt) as exc:
            msg = f"Workflow code step attempted process control: {exc!r}"
            raise RuntimeError(msg) from exc

        return local_ns.get("result")


_default_executor = SandboxExecutor()


def exec_code_step(
    code: str,
    steps: dict[str, Any],
    extra_vars: dict[str, Any] | None = None,
) -> Any:
    """Module-level entry used by pipeline_runner."""
    if _default_executor._get_redis is None:
        from redis_client import get_redis

        _default_executor._get_redis = get_redis
    return _default_executor.execute(code, steps, extra_vars)
