"""Unit tests for llm_client.py — LiteLLM mock, model resolution, retry."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import llm_client


def _mock_completion_response(content: str) -> MagicMock:
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


class TestLlmCall:
    @patch("llm_client.time.sleep")
    @patch("litellm.completion")
    def test_claude_sonnet_resolves_to_anthropic_prefix(
        self, mock_completion: MagicMock, _mock_sleep: MagicMock
    ) -> None:
        mock_completion.return_value = _mock_completion_response('[{"symbol": "AAPL"}]')
        result = llm_client.llm_call("Return JSON", "claude-sonnet-4-5", trace_id="trace-1")
        assert result == '[{"symbol": "AAPL"}]'
        mock_completion.assert_called_once()
        assert mock_completion.call_args.kwargs["model"] == "anthropic/claude-sonnet-4-5"

    @patch("llm_client.time.sleep")
    @patch("litellm.completion")
    def test_prefixed_model_passed_through(self, mock_completion: MagicMock, _mock_sleep: MagicMock) -> None:
        mock_completion.return_value = _mock_completion_response("ok")
        llm_client.llm_call("hi", "openai/gpt-4o")
        assert mock_completion.call_args.kwargs["model"] == "openai/gpt-4o"

    @patch("llm_client.time.sleep")
    @patch("litellm.completion")
    def test_system_user_prompt_split(self, mock_completion: MagicMock, _mock_sleep: MagicMock) -> None:
        mock_completion.return_value = _mock_completion_response("done")
        prompt = "SYSTEM:You are helpful.\n\nUSER:\nAnalyze AAPL"
        llm_client.llm_call(prompt, "claude-sonnet-4-5")
        messages = mock_completion.call_args.kwargs["messages"]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "You are helpful."
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "Analyze AAPL"

    @patch("llm_client.time.sleep")
    @patch("litellm.completion")
    def test_rate_limit_retries_then_succeeds(
        self, mock_completion: MagicMock, mock_sleep: MagicMock
    ) -> None:
        import litellm

        mock_completion.side_effect = [
            litellm.exceptions.RateLimitError(
                message="rate limited",
                llm_provider="anthropic",
                model="claude-sonnet-4-5",
            ),
            _mock_completion_response("success after retry"),
        ]
        result = llm_client.llm_call("prompt", "claude-sonnet-4-5", max_retries=3)
        assert result == "success after retry"
        assert mock_completion.call_count == 2
        mock_sleep.assert_called_once()

    @patch("llm_client.time.sleep")
    @patch("litellm.completion")
    def test_rate_limit_exhausted_raises(self, mock_completion: MagicMock, mock_sleep: MagicMock) -> None:
        import litellm

        err = litellm.exceptions.RateLimitError(
            message="rate limited",
            llm_provider="anthropic",
            model="claude-sonnet-4-5",
        )
        mock_completion.side_effect = err
        with pytest.raises(litellm.exceptions.RateLimitError):
            llm_client.llm_call("prompt", "claude-sonnet-4-5", max_retries=2)
        assert mock_completion.call_count == 2
        assert mock_sleep.call_count == 1

    @patch("llm_client.time.sleep")
    @patch("litellm.completion")
    def test_fallback_model_on_primary_failure(
        self, mock_completion: MagicMock, _mock_sleep: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DECISION_FALLBACK_MODEL", "claude-haiku-4-5")
        mock_completion.side_effect = [
            ValueError("non-retryable primary failure"),
            _mock_completion_response("fallback ok"),
        ]
        result = llm_client.llm_call("prompt", "claude-sonnet-4-5", trace_id="t-fallback")
        assert result == "fallback ok"
        models = [c.kwargs["model"] for c in mock_completion.call_args_list]
        assert models == ["anthropic/claude-sonnet-4-5", "anthropic/claude-haiku-4-5"]

    @patch("llm_client.time.sleep")
    @patch("litellm.completion")
    def test_null_content_returns_empty_string(
        self, mock_completion: MagicMock, _mock_sleep: MagicMock
    ) -> None:
        resp = _mock_completion_response("")
        resp.choices[0].message.content = None
        mock_completion.return_value = resp
        assert llm_client.llm_call("prompt", "claude-sonnet-4-5") == ""
