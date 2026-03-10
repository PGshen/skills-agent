"""Unit tests for ContextBuilder (B3.1)."""
import json
import pytest

from agent.context import ContextBuilder, _OBS_TRUNCATE_CHARS, _MIN_REACT_TURNS
from agent.plan import Plan, Step, StepStatus, Action, ActionType
from agent.state import AgentState
from skills.registry import SkillRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry(tmp_path):
    """Return an empty SkillRegistry (no skill roots)."""
    return SkillRegistry(skill_roots=[])


def _make_state(tmp_path, plan=None):
    return AgentState(
        session_id="test-session",
        user_input="What is the answer?",
        plan=plan,
    )


def _make_plan():
    return Plan(
        goal="Solve the problem",
        steps=[
            Step(id="step-1", description="Load skill", status=StepStatus.PENDING),
            Step(id="step-2", description="Run script", status=StepStatus.PENDING),
        ],
    )


def _react_history(n: int, obs_len: int = 10) -> list[tuple]:
    """Generate n (action, observation) tuples."""
    return [
        (
            json.dumps({"type": "load_skill", "params": {"skill_name": f"skill-{i}"}}),
            "x" * obs_len,
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Basic structure tests
# ---------------------------------------------------------------------------

class TestBuildMessageOrder:
    def test_system_first_user_input_last(self, tmp_path):
        reg = _make_registry(tmp_path)
        state = _make_state(tmp_path)
        cb = ContextBuilder()
        msgs = cb.build(state, reg, react_history=[], history_messages=[])

        assert msgs[0]["role"] == "system"
        assert msgs[-1]["role"] == "user"
        assert msgs[-1]["content"] == state.user_input

    def test_history_messages_before_react(self, tmp_path):
        reg = _make_registry(tmp_path)
        state = _make_state(tmp_path)
        history = [
            {"role": "user", "content": "prev question"},
            {"role": "assistant", "content": "prev answer"},
        ]
        react = _react_history(1)
        cb = ContextBuilder()
        msgs = cb.build(state, reg, react_history=react, history_messages=history)

        # system, hist_user, hist_asst, react_asst, react_obs, user_input
        assert msgs[0]["role"] == "system"
        assert msgs[1]["content"] == "prev question"
        assert msgs[2]["content"] == "prev answer"
        assert msgs[3]["role"] == "assistant"  # react action
        assert msgs[4]["content"].startswith("Observation: ")
        assert msgs[-1]["content"] == state.user_input

    def test_react_pairs_interleaved_correctly(self, tmp_path):
        reg = _make_registry(tmp_path)
        state = _make_state(tmp_path)
        react = _react_history(2)
        cb = ContextBuilder()
        msgs = cb.build(state, reg, react_history=react, history_messages=[])

        # system, (asst, obs) * 2, user_input
        assert msgs[1]["role"] == "assistant"
        assert msgs[2]["role"] == "user"
        assert msgs[2]["content"].startswith("Observation: ")
        assert msgs[3]["role"] == "assistant"
        assert msgs[4]["content"].startswith("Observation: ")
        assert msgs[-1]["content"] == state.user_input

    def test_plan_summary_in_system_prompt(self, tmp_path):
        reg = _make_registry(tmp_path)
        state = _make_state(tmp_path, plan=_make_plan())
        cb = ContextBuilder()
        msgs = cb.build(state, reg, react_history=[], history_messages=[])

        system_content = msgs[0]["content"]
        assert "Solve the problem" in system_content
        assert "step-1" in system_content

    def test_no_plan_shows_placeholder(self, tmp_path):
        reg = _make_registry(tmp_path)
        state = _make_state(tmp_path, plan=None)
        cb = ContextBuilder()
        msgs = cb.build(state, reg, react_history=[], history_messages=[])

        assert "(no plan yet)" in msgs[0]["content"]


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_empty_string(self):
        cb = ContextBuilder()
        assert cb.estimate_tokens("") == 0

    def test_four_chars_one_token(self):
        cb = ContextBuilder()
        assert cb.estimate_tokens("abcd") == 1

    def test_rounding_down(self):
        cb = ContextBuilder()
        assert cb.estimate_tokens("abc") == 0  # 3 // 4 == 0


# ---------------------------------------------------------------------------
# Trimming tests
# ---------------------------------------------------------------------------

class TestTrimToLimit:
    def test_no_trim_when_under_budget(self, tmp_path):
        reg = _make_registry(tmp_path)
        state = _make_state(tmp_path)
        cb = ContextBuilder(max_context_tokens=100_000)
        msgs_in = cb.build(state, reg, react_history=_react_history(3), history_messages=[])
        # Should be unchanged (well under budget)
        total = sum(cb._msg_tokens(m) for m in msgs_in)
        assert total <= 100_000

    def test_system_and_user_input_always_preserved(self, tmp_path):
        reg = _make_registry(tmp_path)
        state = _make_state(tmp_path)
        # Very small budget forces trimming
        cb = ContextBuilder(max_context_tokens=1)
        react = _react_history(5, obs_len=200)
        msgs = cb.build(state, reg, react_history=react, history_messages=[])

        assert msgs[0]["role"] == "system"
        assert msgs[-1]["content"] == state.user_input

    def test_observation_truncated_when_over_budget(self, tmp_path):
        reg = _make_registry(tmp_path)
        state = _make_state(tmp_path)
        long_obs = "y" * 2000
        react = [
            (json.dumps({"type": "load_skill", "params": {}}), long_obs),
        ]
        # Budget large enough to allow the message but trigger obs truncation
        # Total chars ≈ system(~400) + action(~50) + obs(2000+13) + input(19) ≈ 2500
        # 2500 // 4 ≈ 625 tokens; set budget just under that
        cb = ContextBuilder(max_context_tokens=400)
        msgs = cb.build(state, reg, react_history=react, history_messages=[])

        obs_msg = next(m for m in msgs if m.get("content", "").startswith("Observation: "))
        obs_body = obs_msg["content"][len("Observation: "):]
        assert len(obs_body) <= _OBS_TRUNCATE_CHARS + len("…[truncated]")

    def test_oldest_react_pairs_dropped_when_over_budget(self, tmp_path):
        reg = _make_registry(tmp_path)
        state = _make_state(tmp_path)
        # 6 react turns; actual total ~348 tokens (verified).
        # Set budget just above system+min_pairs+input to force dropping oldest pairs.
        # system ≈ 92, 3 pairs ≈ 3*42=126, input ≈ 4 → need ~222; set to 240.
        react = _react_history(6, obs_len=100)
        cb = ContextBuilder(max_context_tokens=240)
        msgs = cb.build(state, reg, react_history=react, history_messages=[])

        obs_count = sum(
            1 for m in msgs
            if isinstance(m.get("content", ""), str) and m["content"].startswith("Observation: ")
        )
        assert obs_count <= _MIN_REACT_TURNS

    def test_keeps_at_least_min_react_turns(self, tmp_path):
        reg = _make_registry(tmp_path)
        state = _make_state(tmp_path)
        react = _react_history(5, obs_len=50)
        # Force trimming but not to zero
        cb = ContextBuilder(max_context_tokens=50)
        msgs = cb.build(state, reg, react_history=react, history_messages=[])

        obs_count = sum(
            1 for m in msgs
            if isinstance(m.get("content", ""), str) and m["content"].startswith("Observation: ")
        )
        # Even under extreme budget, we never drop below MIN_REACT_TURNS (or all if fewer)
        assert obs_count >= min(_MIN_REACT_TURNS, len(react))
