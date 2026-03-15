"""Unit tests for the three context builders (context.py redesign)."""
import json
import pytest

from agent.context import (
    ClassifyContextBuilder,
    OrchestratorContextBuilder,
    ReactContextBuilder,
    estimate_tokens,
    _plan_summary,
)
from agent.multi_agent import SubTask, TaskResult
from agent.plan import Action, ActionType, Plan, Step, StepStatus


# ── Helpers ───────────────────────────────────────────────────────────────────

def _subtask(description="Do the thing", context="", goal="Overall goal"):
    return SubTask(step_id="1", description=description, context=context, goal=goal)


def _result(step_id="1", success=True, output="Done."):
    return TaskResult(step_id=step_id, success=success, output=output)


def _react_history(n: int, obs="observation text") -> list[tuple]:
    return [
        (json.dumps({"type": "read_file", "params": {"path": f"file-{i}.txt"}}), obs)
        for i in range(n)
    ]


def _action(action_type=ActionType.READ_FILE, path="foo.txt") -> Action:
    return Action(type=action_type, params={"path": path})


# ── estimate_tokens ───────────────────────────────────────────────────────────

class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_four_chars_one_token(self):
        assert estimate_tokens("abcd") == 1

    def test_rounds_down(self):
        assert estimate_tokens("abc") == 0


# ── ClassifyContextBuilder ────────────────────────────────────────────────────

class TestClassifyContextBuilder:
    def test_returns_single_user_message(self):
        msgs = ClassifyContextBuilder().build("What is quicksort?")
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_user_input_in_content(self):
        msgs = ClassifyContextBuilder().build("Write a script for me")
        assert "Write a script for me" in msgs[0]["content"]

    def test_contains_simple_and_complex_labels(self):
        msgs = ClassifyContextBuilder().build("anything")
        content = msgs[0]["content"]
        assert "simple" in content
        assert "complex" in content


# ── OrchestratorContextBuilder ────────────────────────────────────────────────

class TestOrchestratorBuildDecompose:
    def test_structure_system_history_user(self):
        history = [{"role": "user", "content": "prev"}, {"role": "assistant", "content": "ok"}]
        msgs = OrchestratorContextBuilder().build_decompose("Do X", history)

        assert msgs[0]["role"] == "system"
        assert msgs[1]["content"] == "prev"
        assert msgs[2]["content"] == "ok"
        assert msgs[-1]["role"] == "user"
        assert "Do X" in msgs[-1]["content"]

    def test_no_history(self):
        msgs = OrchestratorContextBuilder().build_decompose("Do X", [])
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert len(msgs) == 2

    def test_user_input_in_last_message(self):
        msgs = OrchestratorContextBuilder().build_decompose("Write quicksort", [])
        assert "Write quicksort" in msgs[-1]["content"]


class TestOrchestratorBuildSynthesize:
    def test_structure_system_history_user(self):
        history = [{"role": "user", "content": "hi"}]
        results = [_result("1", True, "Step 1 done"), _result("2", False, "Step 2 failed")]
        msgs = OrchestratorContextBuilder().build_synthesize("Do X", results, history)

        assert msgs[0]["role"] == "system"
        assert msgs[1]["content"] == "hi"
        assert msgs[-1]["role"] == "user"

    def test_step_results_in_prompt(self):
        results = [_result("1", True, "Wrote file.py"), _result("2", False, "Script error")]
        msgs = OrchestratorContextBuilder().build_synthesize("task", results, [])
        content = msgs[-1]["content"]
        assert "Wrote file.py" in content
        assert "Script error" in content
        assert "SUCCESS" in content
        assert "FAILED" in content

    def test_no_results_shows_placeholder(self):
        msgs = OrchestratorContextBuilder().build_synthesize("task", [], [])
        assert "no steps completed" in msgs[-1]["content"]

    def test_artifacts_included(self):
        r = TaskResult(step_id="1", success=True, output="Done", artifacts=["out.py"])
        msgs = OrchestratorContextBuilder().build_synthesize("task", [r], [])
        assert "out.py" in msgs[-1]["content"]


class TestOrchestratorBuildSubtaskContext:
    def test_empty_inputs_returns_empty_string(self):
        ctx = OrchestratorContextBuilder().build_subtask_context([], [])
        assert ctx == ""

    def test_session_context_block_present(self):
        history = [
            {"role": "user", "content": "Please use English"},
            {"role": "assistant", "content": "Sure"},
        ]
        ctx = OrchestratorContextBuilder().build_subtask_context([], history)
        assert "[Session context]" in ctx
        assert "Please use English" in ctx
        assert "Sure" in ctx

    def test_summary_wrapper_stripped(self):
        history = [
            {"role": "user", "content": "<summary>User prefers Python</summary>"},
        ]
        ctx = OrchestratorContextBuilder().build_subtask_context([], history)
        assert "<summary>" not in ctx
        assert "User prefers Python" in ctx

    def test_completed_steps_block_present(self):
        results = [_result("1", True, "Wrote quicksort.py"), _result("2", False, "Tests failed")]
        ctx = OrchestratorContextBuilder().build_subtask_context(results, [])
        assert "[Completed steps so far]" in ctx
        assert "✓" in ctx
        assert "✗" in ctx
        assert "Wrote quicksort.py" in ctx

    def test_both_blocks_present_when_both_provided(self):
        history = [{"role": "user", "content": "hello"}]
        results = [_result("1", True, "done")]
        ctx = OrchestratorContextBuilder().build_subtask_context(results, history)
        assert "[Session context]" in ctx
        assert "[Completed steps so far]" in ctx

    def test_assistant_role_in_session_context(self):
        history = [{"role": "assistant", "content": "Here is the result"}]
        ctx = OrchestratorContextBuilder().build_subtask_context([], history)
        assert "Assistant: Here is the result" in ctx


# ── ReactContextBuilder ───────────────────────────────────────────────────────

class TestReactContextBuilderStructure:
    def test_system_message_first(self):
        task = _subtask()
        msgs = ReactContextBuilder().build(task, [], [])
        assert msgs[0]["role"] == "system"

    def test_task_description_in_system(self):
        task = _subtask(description="Read the config file")
        msgs = ReactContextBuilder().build(task, [], [])
        assert "Read the config file" in msgs[0]["content"]

    def test_goal_in_system(self):
        task = _subtask(goal="Overall project goal")
        msgs = ReactContextBuilder().build(task, [], [])
        assert "Overall project goal" in msgs[0]["content"]

    def test_background_context_in_system(self):
        task = _subtask(context="[Session context]\nUser: hello")
        msgs = ReactContextBuilder().build(task, [], [])
        assert "hello" in msgs[0]["content"]

    def test_react_pairs_interleaved(self):
        task = _subtask()
        history = _react_history(2, obs="found nothing")
        msgs = ReactContextBuilder().build(task, history, [])
        # [system, asst, obs, asst, obs]
        assert msgs[1]["role"] == "assistant"
        assert msgs[2]["role"] == "user"
        assert msgs[2]["content"].startswith("Observation: ")
        assert msgs[3]["role"] == "assistant"
        assert msgs[4]["content"].startswith("Observation: ")

    def test_stall_injection_appended_last(self):
        task = _subtask()
        history = _react_history(1)
        msgs = ReactContextBuilder().build(task, history, [], stall_injection="Please continue.")
        assert msgs[-1]["role"] == "user"
        assert msgs[-1]["content"] == "Please continue."

    def test_no_stall_injection_when_none(self):
        task = _subtask()
        msgs = ReactContextBuilder().build(task, _react_history(1), [])
        assert not msgs[-1]["content"].startswith("Please continue")

    def test_action_object_serialised_to_json(self):
        task = _subtask()
        action = _action(ActionType.READ_FILE, "config.py")
        msgs = ReactContextBuilder().build(task, [(action, "file content")], [])
        action_content = msgs[1]["content"]
        parsed = json.loads(action_content)
        assert parsed["params"]["path"] == "config.py"

    def test_available_tools_filtered_in_system(self):
        task = _subtask()
        msgs = ReactContextBuilder().build(task, [], available_tools=["read_file"])
        system = msgs[0]["content"]
        assert "read_file" in system
        assert "write_file" not in system

    def test_no_tools_empty_tools_section(self):
        task = _subtask()
        msgs = ReactContextBuilder().build(task, [], available_tools=[])
        # System prompt still present, just no tool declarations
        assert msgs[0]["role"] == "system"


class TestReactContextBuilderDeclaredTools:
    def test_declared_tools_is_frozenset(self):
        assert isinstance(ReactContextBuilder.DECLARED_TOOLS, frozenset)

    def test_expected_tools_present(self):
        assert "read_file" in ReactContextBuilder.DECLARED_TOOLS
        assert "write_file" in ReactContextBuilder.DECLARED_TOOLS
        assert "web_search" in ReactContextBuilder.DECLARED_TOOLS

    def test_update_plan_not_in_declared_tools(self):
        assert "update_plan" not in ReactContextBuilder.DECLARED_TOOLS


class TestReactContextBuilderCompression:
    def test_no_compressor_over_budget_logs_warning_returns_full(self):
        """Without compressor, over-budget returns full history with a warning."""
        task = _subtask()
        history = _react_history(10, obs="x" * 500)
        # Very tiny budget to force compression path
        builder = ReactContextBuilder(max_context_tokens=1, compressor=None)
        msgs = builder.build(task, history, [])
        # Should still return messages (no crash)
        assert msgs[0]["role"] == "system"
        obs_count = sum(1 for m in msgs if m.get("content", "").startswith("Observation: "))
        assert obs_count == 10  # all preserved (no compressor to reduce)

    def test_compressor_called_when_over_budget(self):
        """When over budget and compressor is set, compress() is called."""
        task = _subtask()
        history = _react_history(6, obs="y" * 300)

        class _FakeCompressor:
            called = False
            def compress(self, pairs):
                _FakeCompressor.called = True
                return {"role": "user", "content": "[History summary] Did some reads."}

        builder = ReactContextBuilder(max_context_tokens=1, compressor=_FakeCompressor())
        msgs = builder.build(task, history, [])

        assert _FakeCompressor.called
        # Summary message replaces oldest pairs
        summary_msgs = [m for m in msgs if "[History summary]" in m.get("content", "")]
        assert len(summary_msgs) == 1

    def test_compressor_keeps_last_3_pairs_verbatim(self):
        """At least the last 3 react pairs survive compression."""
        task = _subtask()
        # 6 pairs; compressor keeps last 3 verbatim
        history = [
            (json.dumps({"type": "read_file", "params": {"path": f"f{i}.txt"}}), f"obs-{i}")
            for i in range(6)
        ]

        class _FakeCompressor:
            def compress(self, pairs):
                return {"role": "user", "content": "[History summary] summary"}

        builder = ReactContextBuilder(max_context_tokens=1, compressor=_FakeCompressor())
        msgs = builder.build(task, history, [])

        obs_messages = [m["content"] for m in msgs if m.get("content", "").startswith("Observation: ")]
        # Last 3 observations (obs-3, obs-4, obs-5) must be present
        assert "obs-3" in obs_messages[-3]
        assert "obs-4" in obs_messages[-2]
        assert "obs-5" in obs_messages[-1]

    def test_history_short_enough_no_compression(self):
        """When under budget, compressor is never called."""
        task = _subtask()

        class _NeverCalled:
            def compress(self, pairs):
                raise AssertionError("compress() should not be called")

        builder = ReactContextBuilder(
            max_context_tokens=100_000, compressor=_NeverCalled()
        )
        msgs = builder.build(task, _react_history(2), [])
        assert msgs[0]["role"] == "system"
