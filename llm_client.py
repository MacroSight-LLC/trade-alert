"""LLM completion helper with Langfuse metadata and retry/fallback."""

from __future__ import annotations

import logging
import os
import random
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reasoning.prompt_builder import ReasoningPrompt

logger = logging.getLogger(__name__)

try:
    import litellm as _litellm

    _litellm.success_callback = ["langfuse"]
    _litellm.failure_callback = ["langfuse"]
except ImportError:
    pass


def _resolve_messages(prompt: str | ReasoningPrompt) -> tuple[str, str]:
    """Extract system and user messages from a flat prompt or ReasoningPrompt."""
    from reasoning.prompt_builder import ReasoningPrompt as _ReasoningPrompt

    if isinstance(prompt, _ReasoningPrompt):
        return prompt.system, prompt.user

    system_msg = ""
    user_msg = prompt
    if prompt.startswith("SYSTEM:"):
        parts = prompt.split("\n\nUSER:\n", 1)
        if len(parts) == 2:
            system_msg = parts[0].removeprefix("SYSTEM:").strip()
            user_msg = parts[1].strip()
    return system_msg, user_msg


def llm_call(
    prompt: str | ReasoningPrompt,
    model: str,
    *,
    trace_id: str | None = None,
    step_name: str = "llm-call",
    max_retries: int = 3,
) -> str:
    """Call the LLM via litellm.completion with Langfuse tracing."""
    import litellm

    fallback_model = os.environ.get("DECISION_FALLBACK_MODEL", "").strip()

    system_msg, user_msg = _resolve_messages(prompt)

    messages: list[dict[str, str]] = []
    if system_msg:
        messages.append({"role": "system", "content": system_msg})
    messages.append({"role": "user", "content": user_msg})

    def _resolve_litellm_model(name: str) -> str:
        if "/" not in name:
            return f"anthropic/{name}"
        return name

    def _run_model(model_name: str, *, used_fallback: bool) -> str:
        litellm_model = _resolve_litellm_model(model_name)
        lf_metadata: dict[str, Any] = {"generation_name": step_name}
        if trace_id:
            lf_metadata["existing_trace_id"] = trace_id
            lf_metadata["update_trace_keys"] = []
        if used_fallback:
            lf_metadata["used_fallback"] = True
        try:
            from prompt_manager import get_gate_defaults, get_prompt_source, get_prompt_version

            lf_metadata["prompt_version"] = get_prompt_version()
            lf_metadata["prompt_source"] = get_prompt_source()
            lf_metadata["gates"] = get_gate_defaults()
        except (ImportError, AttributeError, TypeError):
            pass

        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                response = litellm.completion(
                    model=litellm_model,
                    messages=messages,
                    max_tokens=4096,
                    temperature=0.2,
                    timeout=120,
                    metadata=lf_metadata,
                )
                content = response.choices[0].message.content
                return content if content is not None else ""
            except (
                litellm.exceptions.RateLimitError,
                litellm.exceptions.ServiceUnavailableError,
                litellm.exceptions.APIConnectionError,
                litellm.exceptions.Timeout,
            ) as exc:
                last_exc = exc
                if attempt < max_retries:
                    backoff = (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        "LLM call attempt %d/%d failed (%s), retrying in %.1fs",
                        attempt,
                        max_retries,
                        type(exc).__name__,
                        backoff,
                    )
                    time.sleep(backoff)
                else:
                    logger.error("LLM call failed after %d attempts: %s", max_retries, exc)
        raise last_exc  # type: ignore[misc]

    try:
        return _run_model(model, used_fallback=False)
    except Exception as primary_exc:
        if not fallback_model or fallback_model == model:
            raise
        logger.warning(
            "Primary model %s failed — falling back to %s: %s",
            model,
            fallback_model,
            primary_exc,
        )
        if trace_id:
            try:
                from pipeline_tracing import tag_trace

                tag_trace(trace_id, ["used_fallback=True"])
            except ImportError:
                pass
        return _run_model(fallback_model, used_fallback=True)
