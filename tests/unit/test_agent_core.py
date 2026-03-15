"""Unit tests for AgentCore (EntryAgent): classification and routing."""

import json
from pathlib import Path

import pytest

from agent.core import AgentCore, _format_plan_summary
from agent.events import EventLogger, EventType
from agent.plan import Action, ActionType, Plan, Step, StepStatus
from agent.state import AgentState
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


def make_core(
    model: MockModel,
    tmp_path: Path,
    sink: OutputSink = None,
    max_turns: int = 20,
    dead_loop_window: int = 6,
    session_id: str = "test-session",
) -> AgentCore:
    return AgentCore(
        model=model,
        registry=make_registry(),
        loader=SkillLoader(),
        event_logger=make_logger(tmp_path, session_id),
        sink=sink or OutputSink(),
        max_turns=max_turns,
        dead_loop_window=dead_loop_window,
    )


def classify_simple() -> Action:
    """Classification response: simple task."""
    return Action(type=ActionType.FINAL_ANSWER, params={"content": "simple"})


def classify_complex() -> Action:
    """Classification response: complex task."""
    return Action(type=ActionType.FINAL_ANSWER, params={"content": "complex"})


def decompose_action(goal: str, steps: list[dict]) -> Action:
    return Action(type=ActionType.UPDATE_PLAN, params={"plan": {"goal": goal, "steps": steps}})


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
# _format_plan_summary re-export
# ---------------------------------------------------------------------------

class TestFormatPlanSummary:
    def test_no_plan(self):
        assert _format_plan_summary(None) == "(no plan yet)"

    def test_with_steps(self):
        plan = Plan(goal="do the thing", steps=[
            Step(id="s1", description="first step", status=StepStatus.DONE),
            Step(id="s2", description="second step", status=StepStatus.IN_PROGRESS),
            Step(id="s3", description="third step", status=StepStatus.PENDING),
        ])
        summary = _format_plan_summary(plan)
        assert "Goal: do the thing" in summary
        assert "[done       ] s1: first step" in summary
        assert "[in_progress] s2: second step" in summary
        assert "[pending    ] s3: third step" in summary

    def test_empty_steps(self):
        plan = Plan(goal="goal", steps=[])
        summary = _format_plan_summary(plan)
        assert "Goal: goal" in summary
        assert "Steps:" in summary


# ---------------------------------------------------------------------------
# Classification: simple path
# ---------------------------------------------------------------------------

class TestSimpleRoute:
    def test_simple_classification_returns_direct_answer(self, tmp_path):
        """
        MockModel sequence:
          1. classify → "simple"
          2. direct_answer → FINAL_ANSWER "quicksort is a divide-and-conquer..."
        """
        model = MockModel([
            classify_simple(),
            final_answer("quicksort is a divide-and-conquer algorithm"),
        ])
        core = make_core(model, tmp_path)
        result = core.run("What is quicksort?")
        assert result == "quicksort is a divide-and-conquer algorithm"
        assert model.call_count == 2

    def test_simple_route_emits_route_decision_event(self, tmp_path):
        model = MockModel([classify_simple(), final_answer("answer")])
        core = make_core(model, tmp_path)
        core.run("hello")
        events = read_events(tmp_path)
        route_events = events_of_type(events, "route_decision")
        assert len(route_events) == 1
        assert route_events[0]["data"]["complexity"] == "simple"

    def test_simple_route_emits_final_answer_event(self, tmp_path):
        model = MockModel([classify_simple(), final_answer("the answer")])
        core = make_core(model, tmp_path)
        core.run("hello")
        events = read_events(tmp_path)
        fa_events = events_of_type(events, "final_answer")
        assert len(fa_events) == 1
        assert fa_events[0]["data"]["content"] == "the answer"

    def test_simple_route_calls_on_text_chunk(self, tmp_path):
        chunks = []

        class TrackingSink(OutputSink):
            def on_text_chunk(self, chunk, done):
                chunks.append((chunk, done))

        model = MockModel([classify_simple(), final_answer("my answer")])
        core = make_core(model, tmp_path, sink=TrackingSink())
        core.run("something simple")
        assert chunks == [("my answer", True)]

    def test_simple_classification_does_not_invoke_orchestrator(self, tmp_path):
        """For simple tasks only 2 model calls happen: classify + direct_answer."""
        model = MockModel([classify_simple(), final_answer("direct")])
        core = make_core(model, tmp_path)
        core.run("simple question")
        assert model.call_count == 2


# ---------------------------------------------------------------------------
# Classification: complex path
# ---------------------------------------------------------------------------

class TestComplexRoute:
    def _complex_actions(self) -> list[Action]:
        """Full mock sequence for a 1-step complex task."""
        return [
            classify_complex(),
            decompose_action("write quicksort", [
                {"id": "1", "description": "write the code", "status": "pending"},
            ]),
            final_answer("code written"),                   # ReactAgent
            final_answer("Here is the quicksort code..."),  # synthesize
        ]

    def test_complex_route_returns_synthesized_answer(self, tmp_path):
        model = MockModel(self._complex_actions())
        core = make_core(model, tmp_path)
        result = core.run("Write quicksort and save it")
        assert result == "Here is the quicksort code..."

    def test_complex_route_emits_route_decision_event(self, tmp_path):
        model = MockModel(self._complex_actions())
        core = make_core(model, tmp_path)
        core.run("write quicksort")
        events = read_events(tmp_path)
        route_events = events_of_type(events, "route_decision")
        assert route_events[0]["data"]["complexity"] == "complex"

    def test_complex_route_model_call_count(self, tmp_path):
        """classify + decompose + react(1 step) + synthesize = 4 calls."""
        model = MockModel(self._complex_actions())
        core = make_core(model, tmp_path)
        core.run("write quicksort")
        assert model.call_count == 4

    def test_complex_two_steps_model_call_count(self, tmp_path):
        """classify + decompose + react(step1) + react(step2) + synthesize = 5 calls."""
        model = MockModel([
            classify_complex(),
            decompose_action("build app", [
                {"id": "1", "description": "write code", "status": "pending"},
                {"id": "2", "description": "write tests", "status": "pending"},
            ]),
            final_answer("code done"),
            final_answer("tests done"),
            final_answer("app built"),
        ])
        core = make_core(model, tmp_path)
        result = core.run("Build app")
        assert result == "app built"
        assert model.call_count == 5


# ---------------------------------------------------------------------------
# Classification fallback: defaults to complex
# ---------------------------------------------------------------------------

class TestClassificationFallback:
    def test_unknown_classification_defaults_to_complex(self, tmp_path):
        """If model returns something other than 'simple', default to complex."""
        model = MockModel([
            # Returns "medium" — not "simple" → defaults to complex
            final_answer("medium"),
            decompose_action("goal", [
                {"id": "1", "description": "do it", "status": "pending"},
            ]),
            final_answer("step done"),
            final_answer("result"),
        ])
        core = make_core(model, tmp_path)
        result = core.run("some task")
        assert result == "result"
        events = read_events(tmp_path)
        route_events = events_of_type(events, "route_decision")
        assert route_events[0]["data"]["complexity"] == "complex"

    def test_classification_failure_defaults_to_complex(self, tmp_path):
        """If model raises on classification, route to complex (safe default)."""
        from model.base import ModelResponseError

        call_count = 0

        class FailFirstModel(MockModel):
            def next_action(self, messages, response_format=None):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise ModelResponseError("parse failure")
                return super().next_action(messages)

        model = FailFirstModel([
            decompose_action("goal", [
                {"id": "1", "description": "step", "status": "pending"},
            ]),
            final_answer("done"),
            final_answer("result"),
        ])
        core = make_core(model, tmp_path)
        result = core.run("some task")
        assert result == "result"


# ---------------------------------------------------------------------------
# Sink callbacks
# ---------------------------------------------------------------------------

class TestSinkCallbacks:
    def test_on_route_decision_called_simple(self, tmp_path):
        decisions = []

        class TrackingSink(OutputSink):
            def on_route_decision(self, complexity):
                decisions.append(complexity)

        model = MockModel([classify_simple(), final_answer("answer")])
        core = make_core(model, tmp_path, sink=TrackingSink())
        core.run("simple task")
        assert decisions == ["simple"]

    def test_on_route_decision_called_complex(self, tmp_path):
        decisions = []

        class TrackingSink(OutputSink):
            def on_route_decision(self, complexity):
                decisions.append(complexity)

        model = MockModel([
            classify_complex(),
            decompose_action("g", [{"id": "1", "description": "s", "status": "pending"}]),
            final_answer("step done"),
            final_answer("done"),
        ])
        core = make_core(model, tmp_path, sink=TrackingSink())
        core.run("complex task")
        assert decisions == ["complex"]


# ---------------------------------------------------------------------------
# history_messages pass-through
# ---------------------------------------------------------------------------

class TestHistoryMessages:
    def test_history_messages_appear_in_classify_call(self, tmp_path):
        history = [
            {"role": "user", "content": "prior question"},
            {"role": "assistant", "content": "prior answer"},
        ]
        model = MockModel([classify_simple(), final_answer("answer")])
        core = make_core(model, tmp_path)
        core.run("new question", history_messages=history)
        # The classify call includes history_messages
        classify_msgs = model.call_history[0]
        contents = [m["content"] for m in classify_msgs]
        assert any("prior question" in c for c in contents)


# ---------------------------------------------------------------------------
# initial_state (crash recovery compatibility)
# ---------------------------------------------------------------------------

class TestInitialStateCompat:
    def test_initial_state_plan_passed_to_orchestrator(self, tmp_path):
        """
        If initial_state has a plan with step 1 DONE, the orchestrator should
        skip step 1 and only run step 2.
        """
        plan = Plan(
            goal="recover goal",
            steps=[
                Step(id="1", description="already done", status=StepStatus.DONE),
                Step(id="2", description="needs to run", status=StepStatus.PENDING),
            ],
        )
        initial_state = AgentState(
            session_id="s1",
            user_input="recover goal",
            plan=plan,
        )

        model = MockModel([
            classify_complex(),            # EntryAgent classifies
            # No decompose — plan already in OrchestratorState
            final_answer("step 2 done"),   # ReactAgent step 2
            final_answer("recovered"),     # synthesize
        ])
        core = make_core(model, tmp_path)
        result = core.run("recover goal", initial_state=initial_state)
        assert result == "recovered"
        assert model.call_count == 3  # classify + react(step2) + synthesize
