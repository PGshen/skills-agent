"""Unit tests for OrchestratorAgent: planner + dispatcher."""

import json
from pathlib import Path

import pytest

from agent.events import EventLogger, EventType
from agent.multi_agent import TaskResult
from agent.orchestrator import OrchestratorAgent, OrchestratorState
from agent.plan import Action, ActionType, Plan, Step, StepStatus
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


def make_logger(tmp_path: Path, session_id: str = "test-orch") -> EventLogger:
    return EventLogger(str(tmp_path / "events.jsonl"), session_id)


def make_orchestrator(
    model: MockModel,
    tmp_path: Path,
    sink: OutputSink = None,
    max_turns_per_subtask: int = 5,
) -> OrchestratorAgent:
    return OrchestratorAgent(
        model=model,
        registry=make_registry(),
        loader=SkillLoader(),
        event_logger=make_logger(tmp_path),
        sink=sink or OutputSink(),
        max_turns_per_subtask=max_turns_per_subtask,
    )


def decompose_action(goal: str, steps: list[dict]) -> Action:
    """Build an UPDATE_PLAN action for _decompose()."""
    return Action(type=ActionType.UPDATE_PLAN, params={"plan": {
        "goal": goal,
        "steps": steps,
    }})


def final_answer(content: str) -> Action:
    return Action(type=ActionType.FINAL_ANSWER, params={"content": content})


def read_events(tmp_path: Path) -> list[dict]:
    f = tmp_path / "events.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]


def events_of_type(events: list[dict], t: str) -> list[dict]:
    return [e for e in events if e["type"] == t]


# ---------------------------------------------------------------------------
# Decompose → execute → synthesize: single step
# ---------------------------------------------------------------------------

class TestSingleStep:
    def test_single_step_returns_synthesized_answer(self, tmp_path):
        """
        MockModel sequence:
          1. decompose → UPDATE_PLAN (1 step)
          2. ReactAgent turn: FINAL_ANSWER "step done"
          3. synthesize → FINAL_ANSWER "all good"
        """
        model = MockModel([
            decompose_action("write quicksort", [
                {"id": "1", "description": "write the function", "status": "pending"},
            ]),
            final_answer("step done"),    # ReactAgent's answer
            final_answer("all good"),     # synthesize
        ])
        orch = make_orchestrator(model, tmp_path)
        result = orch.run("write quicksort")
        assert result == "all good"

    def test_single_step_model_called_three_times(self, tmp_path):
        model = MockModel([
            decompose_action("goal", [{"id": "1", "description": "do it", "status": "pending"}]),
            final_answer("step result"),
            final_answer("final"),
        ])
        orch = make_orchestrator(model, tmp_path)
        orch.run("goal")
        assert model.call_count == 3

    def test_plan_updated_event_emitted(self, tmp_path):
        model = MockModel([
            decompose_action("goal", [{"id": "1", "description": "step", "status": "pending"}]),
            final_answer("done"),
            final_answer("summary"),
        ])
        orch = make_orchestrator(model, tmp_path)
        orch.run("goal")
        events = read_events(tmp_path)
        plan_events = events_of_type(events, "plan_updated")
        assert len(plan_events) >= 1
        assert plan_events[0]["data"]["goal"] == "goal"


# ---------------------------------------------------------------------------
# Multi-step flow
# ---------------------------------------------------------------------------

class TestMultiStep:
    def test_two_steps_both_executed(self, tmp_path):
        """
        Sequence:
          1. decompose → 2 steps
          2. ReactAgent step 1: FINAL_ANSWER "wrote file"
          3. ReactAgent step 2: FINAL_ANSWER "ran tests"
          4. synthesize → FINAL_ANSWER "all complete"
        """
        model = MockModel([
            decompose_action("build feature", [
                {"id": "1", "description": "write code", "status": "pending"},
                {"id": "2", "description": "write tests", "status": "pending"},
            ]),
            final_answer("wrote code"),
            final_answer("tests passed"),
            final_answer("feature built successfully"),
        ])
        orch = make_orchestrator(model, tmp_path)
        result = orch.run("build feature")
        assert result == "feature built successfully"
        assert model.call_count == 4

    def test_subtask_start_done_events_emitted(self, tmp_path):
        model = MockModel([
            decompose_action("goal", [
                {"id": "1", "description": "step one", "status": "pending"},
                {"id": "2", "description": "step two", "status": "pending"},
            ]),
            final_answer("step 1 done"),
            final_answer("step 2 done"),
            final_answer("done"),
        ])
        orch = make_orchestrator(model, tmp_path)
        orch.run("goal")
        events = read_events(tmp_path)
        assert len(events_of_type(events, "subtask_start")) == 2
        assert len(events_of_type(events, "subtask_done")) == 2

    def test_step_context_includes_prior_results(self, tmp_path):
        """
        ReactAgent for step 2 should see step 1's result in context (system prompt).
        """
        model = MockModel([
            decompose_action("goal", [
                {"id": "1", "description": "step one", "status": "pending"},
                {"id": "2", "description": "step two", "status": "pending"},
            ]),
            final_answer("step one output"),   # ReactAgent step 1
            final_answer("step two done"),     # ReactAgent step 2
            final_answer("complete"),          # synthesize
        ])
        orch = make_orchestrator(model, tmp_path)
        orch.run("goal")
        # call 0: decompose, call 1: step1 react, call 2: step2 react, call 3: synthesize
        step2_system = model.call_history[2][0]
        assert step2_system["role"] == "system"
        assert "step one output" in step2_system["content"]

    def test_subtask_sink_callbacks_called(self, tmp_path):
        starts, dones = [], []

        class TrackingSink(OutputSink):
            def on_subtask_start(self, step_id, description):
                starts.append((step_id, description))
            def on_subtask_done(self, step_id, success, summary):
                dones.append((step_id, success))

        model = MockModel([
            decompose_action("goal", [
                {"id": "a", "description": "step A", "status": "pending"},
                {"id": "b", "description": "step B", "status": "pending"},
            ]),
            final_answer("A done"),
            final_answer("B done"),
            final_answer("summary"),
        ])
        orch = make_orchestrator(model, tmp_path, sink=TrackingSink())
        orch.run("goal")
        assert len(starts) == 2
        assert starts[0] == ("a", "step A")
        assert starts[1] == ("b", "step B")
        assert dones[0] == ("a", True)
        assert dones[1] == ("b", True)


# ---------------------------------------------------------------------------
# Failed step handling
# ---------------------------------------------------------------------------

class TestFailedStep:
    def test_failed_step_marked_as_failed_in_plan(self, tmp_path):
        """A FAILED: response from ReactAgent marks the step FAILED, execution continues."""
        model = MockModel([
            decompose_action("goal", [
                {"id": "1", "description": "risky step", "status": "pending"},
                {"id": "2", "description": "safe step", "status": "pending"},
            ]),
            final_answer("FAILED: permission denied"),  # step 1 fails
            final_answer("step 2 done"),               # step 2 still runs
            final_answer("partial success"),           # synthesize
        ])
        orch = make_orchestrator(model, tmp_path)
        result = orch.run("goal")
        assert result == "partial success"

    def test_subtask_done_event_has_success_false_for_failed_step(self, tmp_path):
        model = MockModel([
            decompose_action("goal", [
                {"id": "1", "description": "step", "status": "pending"},
            ]),
            final_answer("FAILED: something went wrong"),
            final_answer("answer"),
        ])
        orch = make_orchestrator(model, tmp_path)
        orch.run("goal")
        events = read_events(tmp_path)
        done_events = events_of_type(events, "subtask_done")
        assert len(done_events) == 1
        assert done_events[0]["data"]["success"] is False

    def test_sink_on_subtask_done_called_with_false(self, tmp_path):
        dones = []

        class TrackingSink(OutputSink):
            def on_subtask_done(self, step_id, success, summary):
                dones.append(success)

        model = MockModel([
            decompose_action("goal", [
                {"id": "1", "description": "fail step", "status": "pending"},
            ]),
            final_answer("FAILED: error"),
            final_answer("done"),
        ])
        orch = make_orchestrator(model, tmp_path, sink=TrackingSink())
        orch.run("goal")
        assert dones == [False]


# ---------------------------------------------------------------------------
# Decompose fallback
# ---------------------------------------------------------------------------

class TestDecomposeFallback:
    def test_invalid_decompose_falls_back_to_single_step(self, tmp_path):
        """If decompose returns garbage, orchestrator falls back to a single-step plan."""
        model = MockModel([
            # Decompose returns wrong action type → fallback
            final_answer("not a plan"),
            # ReactAgent for the single fallback step
            final_answer("step done"),
            # synthesize
            final_answer("all done"),
        ])
        orch = make_orchestrator(model, tmp_path)
        result = orch.run("do something")
        assert result == "all done"


# ---------------------------------------------------------------------------
# Synthesize fallback
# ---------------------------------------------------------------------------

class TestSynthesizeFallback:
    def test_synthesize_fallback_concatenates_outputs(self, tmp_path):
        """If synthesize returns a non-FINAL_ANSWER, fallback concatenates step results."""
        model = MockModel([
            decompose_action("goal", [
                {"id": "1", "description": "step", "status": "pending"},
            ]),
            final_answer("step one output"),
            # synthesize returns wrong type → fallback
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "x"}),
        ])
        orch = make_orchestrator(model, tmp_path)
        result = orch.run("goal")
        # Fallback: concatenated step results
        assert "step one output" in result


# ---------------------------------------------------------------------------
# Crash recovery: initial_state
# ---------------------------------------------------------------------------

class TestCrashRecovery:
    def test_initial_state_with_completed_steps_skips_to_current(self, tmp_path):
        """
        If OrchestratorState has a plan with step 1 DONE and step 2 PENDING,
        only step 2 should be dispatched.
        """
        existing_plan = Plan(
            goal="original goal",
            steps=[
                Step(id="1", description="already done", status=StepStatus.DONE),
                Step(id="2", description="needs to run", status=StepStatus.PENDING),
            ],
        )
        initial_state = OrchestratorState(
            session_id="s1",
            user_input="original goal",
            plan=existing_plan,
        )

        model = MockModel([
            # No decompose call — plan already exists
            final_answer("step 2 result"),
            final_answer("resumed summary"),
        ])
        orch = make_orchestrator(model, tmp_path)
        result = orch.run("original goal", initial_state=initial_state)
        assert result == "resumed summary"
        # Only 2 model calls: ReactAgent(step2) + synthesize
        assert model.call_count == 2


# ---------------------------------------------------------------------------
# Plan goal preservation
# ---------------------------------------------------------------------------

class TestPlanGoal:
    def test_orchestrator_state_stores_goal_from_decompose(self, tmp_path):
        model = MockModel([
            decompose_action("the real goal", [
                {"id": "1", "description": "do it", "status": "pending"},
            ]),
            final_answer("done"),
            final_answer("summary"),
        ])
        orch = make_orchestrator(model, tmp_path)
        orch.run("the real goal")
        events = read_events(tmp_path)
        plan_event = events_of_type(events, "plan_updated")[0]
        assert plan_event["data"]["goal"] == "the real goal"
