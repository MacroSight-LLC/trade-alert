"""Langfuse prompt fetch with TTL cache and compile."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from langfuse_client import register_langfuse_failure
from prompt_renderer import _check_unresolved_placeholders

logger = logging.getLogger(__name__)

# Module-level cache for last prompt source
_last_source: str = "not-loaded"
_last_version: str = "yaml-fallback"

# TTL cache for Langfuse prompt objects (avoids repeated API calls)
_prompt_cache: dict[str, tuple[float, Any, Any]] = {}  # key → (ts, sys_obj, usr_obj)
_PROMPT_CACHE_TTL: float = 300.0  # seconds
_prompt_cache_lock = threading.Lock()


def get_prompt_version() -> str:
    """Return the version tag of the last loaded prompts."""
    return _last_version


def get_prompt_source() -> str:
    """Return ``"langfuse"`` or ``"yaml-fallback"``."""
    return _last_source


def set_yaml_fallback_source() -> None:
    """Mark the last prompt load as YAML fallback."""
    global _last_source, _last_version  # noqa: PLW0603
    _last_source = "yaml-fallback"
    _last_version = "yaml-fallback"


def _apply_market_reference_prefix(user: str, merged: dict[str, Any]) -> str:
    if str(merged.get("market_reference_context", "")).strip():
        return (
            "Current market reference prices (use ONLY these for entry/stop/target):\n"
            f"{merged['market_reference_context']}\n\n{user}"
        )
    return user


def _finalize_langfuse_prompts(
    sys_obj: Any,
    usr_obj: Any,
    merged: dict[str, Any],
    warnings: list[str],
) -> tuple[str, str]:
    """Compile Langfuse prompt objects and validate placeholders."""
    global _last_source, _last_version  # noqa: PLW0603

    system = sys_obj.compile(**merged)
    user = usr_obj.compile(**merged)
    user = _apply_market_reference_prefix(user, merged)
    _last_source = "langfuse"
    _last_version = str(getattr(sys_obj, "version", "unknown"))
    if warnings:
        system = "\n".join(warnings) + "\n\n" + system
    _check_unresolved_placeholders(system, "system")
    _check_unresolved_placeholders(user, "user")
    return system, user


def fetch_prompts_from_langfuse(
    timeframe: str,
    merged: dict[str, Any],
    warnings: list[str],
    langfuse_client: Any,
) -> tuple[str, str] | None:
    """Fetch and compile prompts from Langfuse, using TTL cache when valid.

    Returns:
        ``(system, user)`` on success, or ``None`` when caller should fall back.
    """
    if langfuse_client is None:
        return None

    cache_key = timeframe
    with _prompt_cache_lock:
        cached = _prompt_cache.get(cache_key)
    if cached:
        ts, sys_obj, usr_obj = cached
        if (time.monotonic() - ts) < _PROMPT_CACHE_TTL:
            try:
                return _finalize_langfuse_prompts(sys_obj, usr_obj, merged, warnings)
            except (KeyError, TypeError, ValueError, RuntimeError):
                pass  # stale/broken cache entry — refetch below

    try:
        sys_prompt_obj = langfuse_client.get_prompt("decision-system", label="production")
        usr_prompt_obj = langfuse_client.get_prompt("decision-user", label="production")
        with _prompt_cache_lock:
            _prompt_cache[cache_key] = (time.monotonic(), sys_prompt_obj, usr_prompt_obj)
        system, user = _finalize_langfuse_prompts(sys_prompt_obj, usr_prompt_obj, merged, warnings)
        logger.info("Prompts loaded from Langfuse (version=%s)", _last_version)
        return system, user
    except Exception as exc:  # noqa: BLE001 - missing prompts or auth issues should fall back cleanly
        register_langfuse_failure(exc)
        logger.warning("Langfuse prompt fetch failed — using YAML fallback: %s", exc)
        return None
