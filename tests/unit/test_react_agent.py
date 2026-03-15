"""Unit tests for ReactAgent: atomic task executor."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.events import EventLogger, EventType
from agent.multi_agent import SubTask, TaskResult
from agent.plan import Action, ActionType
from agent.react_agent import ReactAgent, ReactState
from model.mock import MockModel
from output.sink import OutputSink
from skills.loader import SkillLoader
from skills.registry import SkillRegistry

FIXTURE_SKILLS = Path(__file__).parent.parent / "fixtures" / "skills"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_registry() -> SkillRegistry:
    return SkillRegistry([{"source": "project", "path": str(FIXTURE_SKILLS), "priority": 0}])


def make_logger(tmp_path: Path, session_id: str = "test-session") -> EventLogger:
    return EventLogger(str(tmp_path / "events.jsonl"), session_id)


def make_subtask(description: str = "do something", step_id: str = "1") -> SubTask:
    return SubTask(step_id=step_id, description=description, goal="overall goal")


def make_agent(
    model: MockModel,
    tmp_path: Path,
    sink: OutputSink = None,
    max_turns: int = 10,
    dead_loop_window: int = 4,
) -> ReactAgent:
    return ReactAgent(
        model=model,
        registry=make_registry(),
        loader=SkillLoader(),
        event_logger=make_logger(tmp_path),
        sink=sink or OutputSink(),
        max_turns=max_turns,
        dead_loop_window=dead_loop_window,
    )


def read_events(tmp_path: Path) -> list[dict]:
    f = tmp_path / "events.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]


def events_of_type(events: list[dict], t: str) -> list[dict]:
    return [e for e in events if e["type"] == t]


# ---------------------------------------------------------------------------
# ReactState
# ---------------------------------------------------------------------------

class TestReactState:
    def test_is_done_initial(self):
        task = make_subtask()
        state = ReactState(session_id="s", task=task)
        assert state.is_done() is False

    def test_is_done_completed(self):
        task = make_subtask()
        state = ReactState(session_id="s", task=task, status="completed")
        assert state.is_done() is True

    def test_is_done_dead_loop(self):
        task = make_subtask()
        state = ReactState(session_id="s", task=task, dead_loop_triggered=True)
        assert state.is_done() is True


# ---------------------------------------------------------------------------
# Happy path: final_answer
# ---------------------------------------------------------------------------

class TestFinalAnswer:
    def test_returns_task_result_success(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.FINAL_ANSWER, params={"content": "task done"}),
        ])
        agent = make_agent(model, tmp_path)
        result = agent.run(make_subtask())
        assert isinstance(result, TaskResult)
        assert result.success is True
        assert result.output == "task done"

    def test_failed_prefix_sets_success_false(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.FINAL_ANSWER, params={"content": "FAILED: cannot do it"}),
        ])
        agent = make_agent(model, tmp_path)
        result = agent.run(make_subtask())
        assert result.success is False

    def test_step_id_preserved_in_result(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path)
        result = agent.run(make_subtask(step_id="step-42"))
        assert result.step_id == "step-42"

    def test_model_called_once_for_immediate_answer(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path)
        agent.run(make_subtask())
        assert model.call_count == 1


# ---------------------------------------------------------------------------
# update_plan is rejected
# ---------------------------------------------------------------------------

class TestUpdatePlanRejected:
    def test_update_plan_returns_error_observation(self, tmp_path):
        """ReactAgent must reject update_plan and return an error, not crash."""
        model = MockModel([
            Action(type=ActionType.UPDATE_PLAN, params={"plan": {"goal": "g", "steps": []}}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path)
        result = agent.run(make_subtask())
        # Model gets observation with the error, then produces final_answer
        assert model.call_count == 2
        assert result.success is True

    def test_update_plan_error_in_observation(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.UPDATE_PLAN, params={"plan": {"goal": "g", "steps": []}}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "ok"}),
        ])
        agent = make_agent(model, tmp_path)
        agent.run(make_subtask())
        # Second call should have the error observation
        second_msgs = model.call_history[1]
        obs = next(m for m in second_msgs if "Observation:" in m.get("content", ""))
        assert "update_plan is not available" in obs["content"]


# ---------------------------------------------------------------------------
# Skill loading
# ---------------------------------------------------------------------------

class TestSkillLoading:
    def test_load_skill_then_final_answer(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "loaded and done"}),
        ])
        agent = make_agent(model, tmp_path)
        result = agent.run(make_subtask())
        assert result.success is True
        assert model.call_count == 2

    def test_load_skill_observation_contains_skill_body(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path)
        agent.run(make_subtask())
        second_msgs = model.call_history[1]
        obs = next(m for m in second_msgs if "Observation:" in m.get("content", ""))
        assert "example-skill" in obs["content"]

    def test_load_nonexistent_skill_returns_error(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "no-such-skill"}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "gave up"}),
        ])
        agent = make_agent(model, tmp_path)
        agent.run(make_subtask())
        second_msgs = model.call_history[1]
        obs = next(m for m in second_msgs if "Observation:" in m.get("content", ""))
        assert "not found" in obs["content"]

    def test_load_resource_nonexistent_skill_returns_error(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.LOAD_RESOURCE, params={
                "skill_name": "no-such-skill",
                "resource": "reference/api.md",
            }),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path)
        agent.run(make_subtask())
        second_msgs = model.call_history[1]
        obs = next(m for m in second_msgs if "Observation:" in m.get("content", ""))
        assert "not found" in obs["content"]

    def test_skill_logged_in_events(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path)
        agent.run(make_subtask())
        events = read_events(tmp_path)
        loaded = events_of_type(events, "skill_loaded")
        assert len(loaded) == 1
        assert loaded[0]["data"]["skill"] == "example-skill"


# ---------------------------------------------------------------------------
# Dead loop detection
# ---------------------------------------------------------------------------

class TestDeadLoopDetection:
    def test_repeated_action_triggers_dead_loop(self, tmp_path):
        repeated = Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"})
        model = MockModel([repeated, repeated, repeated])
        agent = make_agent(model, tmp_path, dead_loop_window=4)
        result = agent.run(make_subtask())
        assert result.success is False
        assert "dead_loop" in result.output
        events = read_events(tmp_path)
        assert events_of_type(events, "dead_loop_detected")

    def test_dead_loop_triggers_on_second_occurrence(self, tmp_path):
        same = Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"})
        model = MockModel([same, same])
        agent = make_agent(model, tmp_path, dead_loop_window=4)
        agent.run(make_subtask())
        assert model.call_count == 2

    def test_different_actions_no_dead_loop(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "advanced-skill"}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path)
        result = agent.run(make_subtask())
        assert result.success is True
        assert not events_of_type(read_events(tmp_path), "dead_loop_detected")


# ---------------------------------------------------------------------------
# Max turns
# ---------------------------------------------------------------------------

class TestMaxTurns:
    def test_max_turns_returns_failed_result(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "advanced-skill"}),
        ])
        # max_turns=2, dead_loop_window=10 so no dead loop fires
        agent = make_agent(model, tmp_path, max_turns=2, dead_loop_window=10)
        result = agent.run(make_subtask())
        assert result.success is False
        assert "max_turns" in result.output

    def test_max_turns_exhausted_model_call_count(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "advanced-skill"}),
        ])
        agent = make_agent(model, tmp_path, max_turns=2, dead_loop_window=10)
        agent.run(make_subtask())
        assert model.call_count == 2


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

class TestContextAssembly:
    def test_system_prompt_contains_task_description(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path)
        agent.run(SubTask(step_id="1", description="write the report", goal="full goal"))
        system_msg = model.call_history[0][0]
        assert system_msg["role"] == "system"
        assert "write the report" in system_msg["content"]

    def test_system_prompt_contains_goal(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path)
        agent.run(SubTask(step_id="1", description="step", goal="the overall project goal"))
        system_msg = model.call_history[0][0]
        assert "the overall project goal" in system_msg["content"]

    def test_system_prompt_contains_context_when_provided(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path)
        agent.run(SubTask(
            step_id="2",
            description="write tests",
            goal="build the feature",
            context="Step 1 wrote the main module at src/main.py",
        ))
        system_msg = model.call_history[0][0]
        assert "Step 1 wrote the main module" in system_msg["content"]

    def test_system_prompt_has_no_update_plan_declaration(self, tmp_path):
        """update_plan must not appear in system prompt (model must not know it exists)."""
        model = MockModel([
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path)
        agent.run(make_subtask())
        system_msg = model.call_history[0][0]
        assert '"update_plan"' not in system_msg["content"]

    def test_react_history_injected_in_subsequent_calls(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path)
        agent.run(make_subtask())
        second_msgs = model.call_history[1]
        obs_msgs = [m for m in second_msgs
                    if m["role"] == "user" and "Observation:" in m["content"]]
        assert len(obs_msgs) == 1

    def test_available_skills_not_in_system_prompt(self, tmp_path):
        """Skill registry (allowed_tools etc.) must not leak into executor prompt."""
        model = MockModel([
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path)
        agent.run(make_subtask())
        system_msg = model.call_history[0][0]
        assert "allowed_tools" not in system_msg["content"]
        assert "allowed-tools" not in system_msg["content"]


# ---------------------------------------------------------------------------
# RUN_SCRIPT without ToolsRuntime
# ---------------------------------------------------------------------------

class TestRunScriptNoTools:
    def test_run_script_error_when_no_tools(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.RUN_SCRIPT, params={
                "skill_name": "example-skill", "script": "run.sh", "args": [],
            }),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "gave up"}),
        ])
        agent = make_agent(model, tmp_path)  # tools=None
        agent.run(make_subtask())
        obs = next(
            m for m in model.call_history[1]
            if "Observation:" in m.get("content", "")
        )
        assert "Error: script execution not enabled" in obs["content"]

    def test_run_script_skill_not_loaded_returns_error(self, tmp_path):
        """run_script on a skill that was never loaded returns ToolNotAllowed."""
        mock_tools = MagicMock()
        mock_tools.available_tools.return_value = ["run_script"]
        model = MockModel([
            Action(type=ActionType.RUN_SCRIPT, params={
                "skill_name": "example-skill", "script": "run.sh", "args": [],
            }),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "gave up"}),
        ])
        agent = ReactAgent(
            model=model,
            registry=make_registry(),
            loader=SkillLoader(),
            event_logger=make_logger(tmp_path),
            tools=mock_tools,
        )
        agent.run(make_subtask())
        obs = next(
            m for m in model.call_history[1]
            if "Observation:" in m.get("content", "")
        )
        assert "ToolNotAllowed" in obs["content"]
        assert "not loaded" in obs["content"]


# ---------------------------------------------------------------------------
# OutputSink integration
# ---------------------------------------------------------------------------

class TestOutputSinkIntegration:
    def test_on_text_chunk_called_with_final_answer(self, tmp_path):
        """ReactAgent emits on_text_chunk for non-streaming models (streaming models
        emit during the model call itself via next_action_streaming)."""
        chunks = []

        class TrackingSink(OutputSink):
            def on_text_chunk(self, chunk, done):
                chunks.append((chunk, done))

        model = MockModel([
            Action(type=ActionType.FINAL_ANSWER, params={"content": "hello world"}),
        ])
        agent = make_agent(model, tmp_path, sink=TrackingSink())
        result = agent.run(make_subtask())
        assert len(chunks) == 1
        assert chunks[0] == ("hello world", True)
        assert result.output == "hello world"

    def test_on_progress_called_for_load_skill(self, tmp_path):
        progress = []

        class TrackingSink(OutputSink):
            def on_progress(self, action, detail=""):
                progress.append((action, detail))

        model = MockModel([
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path, sink=TrackingSink())
        agent.run(make_subtask())
        assert any(a == "load_skill" and "example-skill" in d for a, d in progress)

    def test_on_thinking_start_called_each_turn(self, tmp_path):
        turns = []

        class TrackingSink(OutputSink):
            def on_thinking_start(self, turn):
                turns.append(turn)

        model = MockModel([
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path, sink=TrackingSink())
        agent.run(make_subtask())
        assert turns == [1, 2]


# ---------------------------------------------------------------------------
# Event logging
# ---------------------------------------------------------------------------

class TestEventLogging:
    def test_session_start_and_end_emitted(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path)
        agent.run(make_subtask(step_id="s1"))
        events = read_events(tmp_path)
        types = [e["type"] for e in events]
        assert "session_start" in types
        assert "session_end" in types

    def test_final_answer_event_emitted(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.FINAL_ANSWER, params={"content": "the answer"}),
        ])
        agent = make_agent(model, tmp_path)
        agent.run(make_subtask())
        events = read_events(tmp_path)
        fa_events = events_of_type(events, "final_answer")
        assert len(fa_events) == 1
        assert fa_events[0]["data"]["content"] == "the answer"

    def test_action_requested_and_completed_emitted(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path)
        agent.run(make_subtask())
        events = read_events(tmp_path)
        assert len(events_of_type(events, "action_requested")) == 2
        assert len(events_of_type(events, "action_completed")) == 2

    def test_session_end_success_on_final_answer(self, tmp_path):
        model = MockModel([
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ])
        agent = make_agent(model, tmp_path)
        result = agent.run(make_subtask())
        events = read_events(tmp_path)
        end = events_of_type(events, "session_end")[0]
        assert end["data"]["success"] is True

    def test_session_end_success_false_on_dead_loop(self, tmp_path):
        same = Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"})
        model = MockModel([same, same])
        agent = make_agent(model, tmp_path, dead_loop_window=4)
        result = agent.run(make_subtask())
        assert result.success is False


# ---------------------------------------------------------------------------
# MockModel exhaustion fallback
# ---------------------------------------------------------------------------

class TestMockModelExhaustion:
    def test_exhausted_model_returns_final_answer(self, tmp_path):
        """When MockModel runs out of actions, it returns FINAL_ANSWER automatically."""
        model = MockModel([
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
        ])
        agent = make_agent(model, tmp_path, max_turns=5, dead_loop_window=10)
        result = agent.run(make_subtask())
        # Second call gets the auto-FINAL_ANSWER from MockModel
        assert result.success is True
        assert "MockModel" in result.output
