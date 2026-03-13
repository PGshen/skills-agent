"""Integration tests for OpenAIAdapter — requires OPENAI_API_KEY to be set."""
import pytest

from agent.plan import ActionType
from model.base import RetryAdapter
from model.openai import OpenAIAdapter


MESSAGES = [
    {
        "role": "system",
        "content": (
            "You are an agent. Always respond with a single JSON object matching this schema:\n"
            '{"type": "<action_type>", "params": {...}}\n'
            "Valid action types: load_skill, load_resource, run_script, update_plan, final_answer.\n"
            "Never include any text outside the JSON object."
        ),
    },
    {
        "role": "user",
        "content": 'Reply with a final_answer action. Set params.content to "hello from integration test".',
    },
]


@pytest.mark.integration
class TestOpenAIAdapterIntegration:
    def test_next_action_returns_final_answer(self):
        adapter = OpenAIAdapter()
        action = adapter.next_action(MESSAGES)
        assert action.type == ActionType.FINAL_ANSWER
        assert "hello from integration test" in action.params.get("content", "")

    def test_next_action_sink_called_on_final_answer(self):
        chunks = []

        class _CaptureSink:
            def on_text_chunk(self, chunk, done):
                chunks.append((chunk, done))

        adapter = OpenAIAdapter(sink=_CaptureSink())
        adapter.next_action(MESSAGES)

        assert len(chunks) == 1
        text, done = chunks[0]
        assert done is True
        assert "hello from integration test" in text

    def test_next_action_streaming_returns_final_answer(self):
        adapter = OpenAIAdapter()
        action = adapter.next_action_streaming(MESSAGES)
        assert action.type == ActionType.FINAL_ANSWER
        assert "hello from integration test" in action.params.get("content", "")

    def test_next_action_streaming_sink_receives_deltas(self):
        chunks = []

        class _CaptureSink:
            def on_text_chunk(self, chunk, done):
                chunks.append((chunk, done))

        adapter = OpenAIAdapter(sink=_CaptureSink())
        adapter.next_action_streaming(MESSAGES)

        assert len(chunks) > 1, "Expected multiple delta callbacks during streaming"
        assert any(done for _, done in chunks), "Expected at least one done=True callback"
        full = "".join(c for c, d in chunks if not d)
        assert "hello from integration test" in full

    def test_retry_adapter_integration(self):
        """RetryAdapter should succeed on first try with a well-prompted model."""
        inner = OpenAIAdapter()
        adapter = RetryAdapter(inner, max_retries=1)
        action = adapter.next_action(MESSAGES)
        assert action.type == ActionType.FINAL_ANSWER
