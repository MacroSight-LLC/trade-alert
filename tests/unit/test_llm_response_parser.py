"""Unit tests for llm_response_parser.py — valid and malformed JSON."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from llm_response_parser import parse_llm_alerts


def _sample_alert() -> dict:
    return {
        "symbol": "AAPL",
        "direction": "LONG",
        "edge_probability": 0.82,
        "confidence": 0.85,
        "timeframe": "15m",
        "thesis": "Breakout",
        "entry": {"level": 185.0, "stop": 182.0, "target": 195.0},
        "timeframe_rationale": "15m setup",
        "sentiment_context": "bullish",
        "unusual_activity": [],
        "macro_regime": "risk-on",
        "sources_agree": 4,
    }


class TestValidJson:
    def test_plain_array(self) -> None:
        payload = json.dumps([_sample_alert()])
        parsed, repaired = parse_llm_alerts(payload)
        assert parsed is not None
        assert len(parsed) == 1
        assert parsed[0]["symbol"] == "AAPL"
        assert repaired is False

    def test_fenced_markdown(self) -> None:
        payload = f"```json\n{json.dumps([_sample_alert()])}\n```"
        parsed, repaired = parse_llm_alerts(payload)
        assert parsed is not None
        assert len(parsed) == 1

    def test_prose_wrapped_array(self) -> None:
        payload = f"Here are alerts:\n{json.dumps([_sample_alert()])}\nThanks"
        parsed, _ = parse_llm_alerts(payload)
        assert parsed is not None
        assert len(parsed) == 1

    def test_dict_alerts_wrapper(self) -> None:
        parsed, _ = parse_llm_alerts({"alerts": [_sample_alert()]})
        assert parsed is not None
        assert len(parsed) == 1

    def test_list_payload_direct(self) -> None:
        parsed, _ = parse_llm_alerts([_sample_alert()])
        assert parsed is not None
        assert len(parsed) == 1

    def test_langfuse_scores_on_success(self) -> None:
        scores: list[tuple[str, float, str]] = []

        def _add_score(_tid: str, name: str, val: float, comment: str = "") -> None:
            scores.append((name, val, comment))

        parse_llm_alerts(json.dumps([_sample_alert()]), add_score_fn=_add_score, trace_id="t1")
        names = {s[0] for s in scores}
        assert "llm_json_valid" in names
        assert "llm_json_repaired" in names
        valid = next(v for n, v, _ in scores if n == "llm_json_valid")
        assert valid == 1.0


class TestMalformedJson:
    def test_trailing_comma_repaired(self) -> None:
        broken = json.dumps([_sample_alert()]).replace("}", ",}", 1)
        parsed, repaired = parse_llm_alerts(broken)
        assert parsed is not None
        assert len(parsed) == 1
        assert repaired is True

    def test_not_a_list_returns_none(self) -> None:
        parsed, repaired = parse_llm_alerts('{"not": "a list"}')
        assert parsed is None
        assert repaired is False

    def test_invalid_json_returns_none(self) -> None:
        parsed, _ = parse_llm_alerts("{ totally broken json")
        assert parsed is None

    def test_empty_response_returns_none(self) -> None:
        parsed, _ = parse_llm_alerts(None)
        assert parsed is None
        parsed, _ = parse_llm_alerts("")
        assert parsed is None

    def test_api_error_marker_returns_none(self) -> None:
        parsed, _ = parse_llm_alerts("litellm.InternalServerError: overloaded_error")
        assert parsed is None

    def test_parse_failure_langfuse_score(self) -> None:
        add_score = MagicMock()
        parse_llm_alerts("not json", add_score_fn=add_score, trace_id="t2")
        add_score.assert_any_call("t2", "llm_json_valid", 0.0, comment="JSON parse failed")

    def test_api_error_langfuse_score(self) -> None:
        add_score = MagicMock()
        parse_llm_alerts(None, add_score_fn=add_score, trace_id="t3")
        add_score.assert_any_call(
            "t3", "llm_api_error", 1.0, comment="LLM returned None — all retries exhausted"
        )
