"""Unit tests for ModelAdapter, parse_action_response, RetryAdapter, and MockModel."""
import pytest

from agent.plan import Action, ActionType
from model.base import (
    ModelAdapter,
    ModelResponseError,
    RetryAdapter,
    parse_action_response,
)
from model.mock import MockModel


# ─── Helpers ──────────────────────────────────────────────────────────────────

def final_answer(content: str = "done") -> Action:
    return Action(type=ActionType.FINAL_ANSWER, params={"content": content})


def load_skill(name: str) -> Action:
    return Action(type=ActionType.LOAD_SKILL, params={"skill_name": name})


# ─── MockModel ────────────────────────────────────────────────────────────────

class TestMockModel:
    def test_isinstance_model_adapter(self):
        assert isinstance(MockModel([]), ModelAdapter)

    def test_returns_actions_in_order(self):
        actions = [load_skill("a"), load_skill("b"), final_answer()]
        model = MockModel(actions)
        msgs: list[dict] = []
        assert model.next_action(msgs).params["skill_name"] == "a"
        assert model.next_action(msgs).params["skill_name"] == "b"
        assert model.next_action(msgs).type == ActionType.FINAL_ANSWER

    def test_exhausted_returns_final_answer(self):
        model = MockModel([load_skill("x")])
        msgs: list[dict] = []
        model.next_action(msgs)  # consume the only action
        result = model.next_action(msgs)
        assert result.type == ActionType.FINAL_ANSWER
        assert result.params["content"] == "[MockModel: action sequence exhausted]"

    def test_empty_sequence_returns_final_answer(self):
        model = MockModel([])
        result = model.next_action([])
        assert result.type == ActionType.FINAL_ANSWER
        assert "[MockModel: action sequence exhausted]" in result.params["content"]

    def test_accepts_dict_actions(self):
        model = MockModel([
            {"type": "load_skill", "params": {"skill_name": "my-skill"}},
            {"type": "final_answer", "params": {"content": "ok"}},
        ])
        first = model.next_action([])
        assert isinstance(first, Action)
        assert first.type == ActionType.LOAD_SKILL
        assert first.params["skill_name"] == "my-skill"

    def test_accepts_mixed_action_and_dict(self):
        model = MockModel([
            load_skill("first"),
            {"type": "final_answer", "params": {"content": "done"}},
        ])
        assert model.next_action([]).type == ActionType.LOAD_SKILL
        assert model.next_action([]).type == ActionType.FINAL_ANSWER

    def test_call_count_increments(self):
        model = MockModel([load_skill("a"), load_skill("b"), final_answer()])
        assert model.call_count == 0
        model.next_action([])
        model.next_action([])
        model.next_action([])
        assert model.call_count == 3

    def test_call_history_records_messages(self):
        model = MockModel([final_answer()])
        msgs = [{"role": "user", "content": "hello"}]
        model.next_action(msgs)
        assert model.call_history[0] == msgs

    def test_reset_clears_state(self):
        model = MockModel([load_skill("a"), final_answer()])
        model.next_action([])
        model.next_action([])
        assert model.call_count == 2
        model.reset()
        assert model.call_count == 0
        # After reset, actions should restart from beginning
        result = model.next_action([])
        assert result.type == ActionType.LOAD_SKILL


# ─── parse_action_response ────────────────────────────────────────────────────

class TestParseActionResponse:
    def test_clean_json(self):
        raw = '{"type": "final_answer", "params": {"content": "hello"}}'
        action = parse_action_response(raw)
        assert action.type == ActionType.FINAL_ANSWER
        assert action.params["content"] == "hello"

    def test_json_without_params(self):
        raw = '{"type": "load_skill"}'
        action = parse_action_response(raw)
        assert action.type == ActionType.LOAD_SKILL
        assert action.params == {}

    def test_extracts_json_from_code_fence(self):
        raw = (
            "Here is my response:\n"
            "```json\n"
            '{"type": "update_plan", "params": {"plan": {}}}\n'
            "```\n"
            "That's the action."
        )
        action = parse_action_response(raw)
        assert action.type == ActionType.UPDATE_PLAN

    def test_extracts_json_from_prose(self):
        raw = 'I will load the skill. {"type": "load_skill", "params": {"skill_name": "foo"}} Done.'
        action = parse_action_response(raw)
        assert action.type == ActionType.LOAD_SKILL
        assert action.params["skill_name"] == "foo"

    def test_raises_on_plain_text(self):
        with pytest.raises(ModelResponseError):
            parse_action_response("I don't know what to do.")

    def test_raises_on_missing_type(self):
        with pytest.raises(ModelResponseError):
            parse_action_response('{"params": {"content": "hi"}}')

    def test_raises_on_invalid_type_value(self):
        with pytest.raises(ModelResponseError):
            parse_action_response('{"type": "not_a_real_action", "params": {}}')

    def test_raises_on_empty_string(self):
        with pytest.raises(ModelResponseError):
            parse_action_response("")

    def test_all_action_types_parseable(self):
        for action_type in ActionType:
            raw = f'{{"type": "{action_type.value}", "params": {{}}}}'
            action = parse_action_response(raw)
            assert action.type == action_type


# ─── RetryAdapter ─────────────────────────────────────────────────────────────

class _FailThenSucceedModel(ModelAdapter):
    """Fails `fail_count` times with ModelResponseError, then returns `success_action`."""

    def __init__(self, fail_count: int, success_action: Action) -> None:
        self._fail_count = fail_count
        self._success_action = success_action
        self._calls = 0
        self.received_messages: list[list[dict]] = []

    def next_action(self, messages: list[dict]) -> Action:
        self._calls += 1
        self.received_messages.append(messages)
        if self._calls <= self._fail_count:
            raise ModelResponseError(f"Simulated failure #{self._calls}")
        return self._success_action


class _AlwaysFailModel(ModelAdapter):
    def next_action(self, messages: list[dict]) -> Action:
        raise ModelResponseError("Always fails")


class TestRetryAdapter:
    def test_succeeds_on_first_try(self):
        inner = _FailThenSucceedModel(0, final_answer("ok"))
        adapter = RetryAdapter(inner, max_retries=2)
        result = adapter.next_action([])
        assert result.type == ActionType.FINAL_ANSWER

    def test_retries_after_one_failure(self):
        inner = _FailThenSucceedModel(1, final_answer("recovered"))
        adapter = RetryAdapter(inner, max_retries=2)
        result = adapter.next_action([])
        assert result.type == ActionType.FINAL_ANSWER
        assert inner._calls == 2

    def test_correction_message_appended_on_retry(self):
        inner = _FailThenSucceedModel(1, final_answer("ok"))
        adapter = RetryAdapter(inner, max_retries=2)
        adapter.next_action([{"role": "user", "content": "start"}])
        # Second call should have correction message appended
        second_call_msgs = inner.received_messages[1]
        assert len(second_call_msgs) == 2
        assert second_call_msgs[1]["role"] == "user"
        assert "JSON" in second_call_msgs[1]["content"]

    def test_raises_after_max_retries_exhausted(self):
        adapter = RetryAdapter(_AlwaysFailModel(), max_retries=2)
        with pytest.raises(ModelResponseError):
            adapter.next_action([])

    def test_total_calls_equals_max_retries_plus_one(self):
        inner = _FailThenSucceedModel(99, final_answer())  # always fails within retry range
        adapter = RetryAdapter(inner, max_retries=2)
        with pytest.raises(ModelResponseError):
            adapter.next_action([])
        assert inner._calls == 3  # initial + 2 retries

    def test_original_messages_not_mutated(self):
        inner = _FailThenSucceedModel(1, final_answer())
        adapter = RetryAdapter(inner, max_retries=2)
        original = [{"role": "user", "content": "hi"}]
        adapter.next_action(list(original))
        assert original == [{"role": "user", "content": "hi"}]
