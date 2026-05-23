"""Parse LLM decision JSON payloads with defensive repair.

Extracted from validate_and_filter for testability (SSOT audit #18).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_API_ERROR_MARKERS = (
    "InternalServerError",
    "overloaded_error",
    "RateLimitError",
    "ServiceUnavailableError",
    "APIConnectionError",
    "APIStatusError",
    "AnthropicError",
)


def _extract_json_array_text(payload: Any) -> str:
    if isinstance(payload, str):
        text = payload.strip()
    elif isinstance(payload, list):
        return json.dumps(payload)
    elif isinstance(payload, dict):
        content = payload.get("content")
        if isinstance(content, str):
            text = content.strip()
        else:
            return json.dumps(payload)
    else:
        text = str(payload or "").strip()

    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    if not text.startswith("["):
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end > start:
            text = text[start : end + 1]

    return text


def _json_loads_with_repairs(text: str) -> tuple[Any, bool]:
    """Parse JSON with light deterministic repairs for common LLM artifacts."""
    try:
        return json.loads(text), False
    except json.JSONDecodeError:
        pass

    repaired = re.sub(r",(\s*[\]}])", r"\1", text)
    return json.loads(repaired), True


def parse_llm_alerts(
    llm_response: Any,
    *,
    add_score_fn: Any | None = None,
    trace_id: str | None = None,
) -> tuple[list[Any] | None, bool]:
    """Parse an LLM decision response into a list of raw alert dicts.

    Returns:
        Tuple of (parsed list or None on failure, whether JSON repair was used).
    """
    if llm_response is None or llm_response == "":
        logger.error("LLM response is None/empty — all retries exhausted (API overload or timeout)")
        if add_score_fn and trace_id:
            add_score_fn(trace_id, "llm_api_error", 1.0, comment="LLM returned None — all retries exhausted")
        return None, False

    llm_resp_str = str(llm_response)
    if any(m in llm_resp_str for m in _API_ERROR_MARKERS):
        logger.error("LLM API error detected (not a prompt compliance issue): %s", llm_resp_str[:300])
        if add_score_fn and trace_id:
            add_score_fn(
                trace_id, "llm_api_error", 1.0, comment="LLM backend error — not a JSON compliance failure"
            )
        return None, False

    parse_used_repair = False
    try:
        llm_json_text = _extract_json_array_text(llm_response)
        raw, parse_used_repair = _json_loads_with_repairs(llm_json_text)
        if isinstance(raw, dict):
            for key in ("alerts", "result", "results", "data"):
                candidate = raw.get(key)
                if isinstance(candidate, list):
                    raw = candidate
                    break
        if not isinstance(raw, list):
            raise ValueError(f"Expected list, got {type(raw).__name__}")
    except Exception as exc:
        logger.error("Decision engine JSON parse error: %s", exc)
        logger.error("Raw response: %s", llm_resp_str[:500])
        if add_score_fn and trace_id:
            add_score_fn(trace_id, "llm_json_valid", 0.0, comment="JSON parse failed")
        return None, False

    if add_score_fn and trace_id:
        add_score_fn(trace_id, "llm_json_valid", 1.0, comment="valid JSON array")
        add_score_fn(
            trace_id,
            "llm_json_repaired",
            1.0 if parse_used_repair else 0.0,
            comment="1 if lightweight parser repair was required",
        )

    return raw, parse_used_repair
