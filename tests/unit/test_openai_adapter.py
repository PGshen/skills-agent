"""Unit tests for OpenAIAdapter (mocked OpenAI client, no real API calls)."""
from unittest.mock import MagicMock, patch

import pytest

from agent.plan import Action, ActionType
from model.base import ModelAdapter, ModelResponseError
from model.openai import OpenAIAdapter


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_completion(content: str) -> MagicMock:
    """Build a fake non-streaming ChatCompletion response."""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _make_stream_chunks(text: str) -> list[MagicMock]:
    """Build fake streaming chunks, one character per chunk."""
    chunks = []
    for ch in text:
        delta = MagicMock()
        delta.content = ch
        choice = MagicMock()
        choice.delta = delta
        chunk = MagicMock()
        chunk.choices = [choice]
        chunks.append(chunk)
    return chunks


def _make_adapter(sink=None) -> tuple[OpenAIAdapter, MagicMock]:
    """Return (adapter, mock_client) with OpenAI client patched out."""
    with patch("model.openai.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        adapter = OpenAIAdapter(api_key="test-key", sink=sink)
        return adapter, mock_client


# ─── Construction ─────────────────────────────────────────────────────────────

class TestConstruction:
    def test_isinstance_model_adapter(self):
        with patch("model.openai.OpenAI"):
            adapter = OpenAIAdapter(api_key="x")
        assert isinstance(adapter, ModelAdapter)

    def test_default_model_is_gpt4o(self):
        with patch("model.openai.OpenAI"):
            adapter = OpenAIAdapter(api_key="x")
        assert adapter._model == "gpt-4o"

    def test_default_max_tokens(self):
        with patch("model.openai.OpenAI"):
            adapter = OpenAIAdapter(api_key="x")
        assert adapter._max_tokens == 4096

    def test_default_sink_is_null_sink(self):
        from output.sink import NullSink
        with patch("model.openai.OpenAI"):
            adapter = OpenAIAdapter(api_key="x")
        assert isinstance(adapter._sink, NullSink)

    def test_custom_model_and_tokens(self):
        with patch("model.openai.OpenAI"):
            adapter = OpenAIAdapter(api_key="x", model="gpt-3.5-turbo", max_tokens=512)
        assert adapter._model == "gpt-3.5-turbo"
        assert adapter._max_tokens == 512


# ─── next_action (non-streaming) ──────────────────────────────────────────────

class TestNextAction:
    def test_parses_valid_json_response(self):
        adapter, mock_client = _make_adapter()
        raw = '{"type": "load_skill", "params": {"skill_name": "foo"}}'
        mock_client.chat.completions.create.return_value = _make_completion(raw)

        action = adapter.next_action([{"role": "user", "content": "hi"}])

        assert action.type == ActionType.LOAD_SKILL
        assert action.params["skill_name"] == "foo"

    def test_passes_messages_to_api(self):
        adapter, mock_client = _make_adapter()
        raw = '{"type": "final_answer", "params": {"content": "done"}}'
        mock_client.chat.completions.create.return_value = _make_completion(raw)
        msgs = [{"role": "user", "content": "hello"}]

        adapter.next_action(msgs)

        call_kwargs = mock_client.chat.completions.create.call_args
        assert call_kwargs.kwargs["messages"] == msgs
        assert call_kwargs.kwargs["model"] == "gpt-4o"
        assert call_kwargs.kwargs["max_tokens"] == 4096

    def test_raises_on_invalid_json(self):
        adapter, mock_client = _make_adapter()
        mock_client.chat.completions.create.return_value = _make_completion("not json at all")

        with pytest.raises(ModelResponseError):
            adapter.next_action([])

    def test_raises_on_missing_type_field(self):
        adapter, mock_client = _make_adapter()
        mock_client.chat.completions.create.return_value = _make_completion(
            '{"params": {"content": "hi"}}'
        )

        with pytest.raises(ModelResponseError):
            adapter.next_action([])

    def test_non_streaming_never_calls_sink(self):
        """next_action() does not call on_text_chunk — AgentCore._execute_action() does."""
        sink = MagicMock()
        adapter, mock_client = _make_adapter(sink=sink)
        for raw in [
            '{"type": "final_answer", "params": {"content": "hello"}}',
            '{"type": "final_answer", "params": {}}',
            '{"type": "load_skill", "params": {"skill_name": "bar"}}',
        ]:
            mock_client.chat.completions.create.return_value = _make_completion(raw)
            adapter.next_action([])

        sink.on_text_chunk.assert_not_called()

    def test_json_wrapped_in_prose_still_parses(self):
        adapter, mock_client = _make_adapter()
        raw = 'Here is my action: {"type": "update_plan", "params": {"plan": {}}} Done.'
        mock_client.chat.completions.create.return_value = _make_completion(raw)

        action = adapter.next_action([])

        assert action.type == ActionType.UPDATE_PLAN

    def test_all_action_types_parseable(self):
        adapter, mock_client = _make_adapter()
        for action_type in ActionType:
            raw = f'{{"type": "{action_type.value}", "params": {{}}}}'
            mock_client.chat.completions.create.return_value = _make_completion(raw)
            action = adapter.next_action([])
            assert action.type == action_type


# ─── next_action_streaming ────────────────────────────────────────────────────

class TestNextActionStreaming:
    def _setup_stream(self, mock_client, json_text: str) -> None:
        chunks = _make_stream_chunks(json_text)
        mock_client.chat.completions.create.return_value = iter(chunks)

    def test_returns_correct_action_type(self):
        adapter, mock_client = _make_adapter()
        self._setup_stream(
            mock_client,
            '{"type": "final_answer", "params": {"content": "streaming works"}}',
        )

        action = adapter.next_action_streaming([])

        assert action.type == ActionType.FINAL_ANSWER

    def test_streaming_calls_sink_multiple_times(self):
        sink = MagicMock()
        adapter, mock_client = _make_adapter(sink=sink)
        self._setup_stream(
            mock_client,
            '{"type": "final_answer", "params": {"content": "abc"}}',
        )

        adapter.next_action_streaming([])

        calls = sink.on_text_chunk.call_args_list
        # Should have at least one done=False delta and one done=True
        done_false_calls = [c for c in calls if c.kwargs.get("done") is False or (len(c.args) > 1 and c.args[1] is False)]
        done_true_calls = [c for c in calls if c.kwargs.get("done") is True or (len(c.args) > 1 and c.args[1] is True)]
        assert len(done_false_calls) > 0
        assert len(done_true_calls) == 1

    def test_streaming_delta_concat_equals_full_content(self):
        sink = MagicMock()
        adapter, mock_client = _make_adapter(sink=sink)
        content = "hello world"
        self._setup_stream(
            mock_client,
            f'{{"type": "final_answer", "params": {{"content": "{content}"}}}}',
        )

        adapter.next_action_streaming([])

        # Collect all delta chunks (done=False), concatenate
        calls = sink.on_text_chunk.call_args_list
        deltas = ""
        for c in calls:
            val = c.args[0] if c.args else c.kwargs.get("chunk", "")
            done = c.args[1] if len(c.args) > 1 else c.kwargs.get("done")
            if not done:
                deltas += val
        assert content in deltas

    def test_streaming_passes_stream_true_to_api(self):
        adapter, mock_client = _make_adapter()
        self._setup_stream(
            mock_client,
            '{"type": "load_skill", "params": {"skill_name": "x"}}',
        )

        adapter.next_action_streaming([])

        call_kwargs = mock_client.chat.completions.create.call_args
        assert call_kwargs.kwargs.get("stream") is True

    def test_streaming_non_final_answer_action(self):
        adapter, mock_client = _make_adapter()
        self._setup_stream(
            mock_client,
            '{"type": "load_skill", "params": {"skill_name": "my-skill"}}',
        )

        action = adapter.next_action_streaming([])

        assert action.type == ActionType.LOAD_SKILL
        assert action.params["skill_name"] == "my-skill"

    def test_streaming_done_true_fires_with_empty_chunk(self):
        """on_text_chunk("", done=True) is the final call — not the full value again."""
        sink = MagicMock()
        adapter, mock_client = _make_adapter(sink=sink)
        self._setup_stream(
            mock_client,
            '{"type": "final_answer", "params": {"content": "hello"}}',
        )

        adapter.next_action_streaming([])

        calls = sink.on_text_chunk.call_args_list
        done_true_calls = [
            c for c in calls
            if (c.args[1] if len(c.args) > 1 else c.kwargs.get("done")) is True
        ]
        assert len(done_true_calls) == 1
        done_call = done_true_calls[0]
        chunk_val = done_call.args[0] if done_call.args else done_call.kwargs.get("chunk", "")
        assert chunk_val == "", f"Expected empty string on done=True, got {chunk_val!r}"

    def test_streaming_empty_delta_chunks_ignored(self):
        """Chunks with empty/None content should not break the parser."""
        adapter, mock_client = _make_adapter()
        json_text = '{"type": "final_answer", "params": {"content": "ok"}}'
        real_chunks = _make_stream_chunks(json_text)

        # Insert empty-content chunks in the middle
        empty_delta = MagicMock()
        empty_delta.content = ""
        empty_choice = MagicMock()
        empty_choice.delta = empty_delta
        empty_chunk = MagicMock()
        empty_chunk.choices = [empty_choice]

        none_delta = MagicMock()
        none_delta.content = None
        none_choice = MagicMock()
        none_choice.delta = none_delta
        none_chunk = MagicMock()
        none_chunk.choices = [none_choice]

        mixed = [real_chunks[0], empty_chunk, none_chunk] + real_chunks[1:]
        mock_client.chat.completions.create.return_value = iter(mixed)

        action = adapter.next_action_streaming([])
        assert action.type == ActionType.FINAL_ANSWER


# ─── RetryAdapter integration ─────────────────────────────────────────────────

class TestRetryAdapterIntegration:
    def test_retry_adapter_wraps_openai_adapter(self):
        from model.base import RetryAdapter

        with patch("model.openai.OpenAI") as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            inner = OpenAIAdapter(api_key="x")

        # First call returns bad JSON, second returns valid
        mock_client.chat.completions.create.side_effect = [
            _make_completion("not valid json"),
            _make_completion('{"type": "final_answer", "params": {"content": "ok"}}'),
        ]

        adapter = RetryAdapter(inner, max_retries=1)
        action = adapter.next_action([])

        assert action.type == ActionType.FINAL_ANSWER
        assert mock_client.chat.completions.create.call_count == 2

    def test_retry_exhausted_raises_model_response_error(self):
        from model.base import RetryAdapter

        with patch("model.openai.OpenAI") as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            inner = OpenAIAdapter(api_key="x")

        mock_client.chat.completions.create.return_value = _make_completion("bad")

        adapter = RetryAdapter(inner, max_retries=2)
        with pytest.raises(ModelResponseError):
            adapter.next_action([])

        assert mock_client.chat.completions.create.call_count == 3  # 1 + 2 retries
