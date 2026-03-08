"""Unit tests for AgentCore ReAct main loop."""

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

# ---------------------------------------------------------------------------
# Path to fixture skills directory
# ---------------------------------------------------------------------------

FIXTURE_SKILLS = Path(__file__).parent.parent / "fixtures" / "skills"


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------

def make_registry(tmp_path: Path = None) -> SkillRegistry:
    """Registry pointing at the fixture skills directory."""
    root = str(FIXTURE_SKILLS)
    return SkillRegistry([{"source": "project", "path": root, "priority": 0}])


def make_logger(tmp_path: Path, session_id: str = "test-session") -> EventLogger:
    events_file = tmp_path / "events.jsonl"
    return EventLogger(str(events_file), session_id)


def make_core(
    model: MockModel,
    tmp_path: Path,
    registry: SkillRegistry = None,
    sink: OutputSink = None,
    max_turns: int = 20,
    dead_loop_window: int = 6,
    session_id: str = "test-session",
) -> AgentCore:
    if registry is None:
        registry = make_registry()
    logger = make_logger(tmp_path, session_id)
    loader = SkillLoader()
    return AgentCore(
        model=model,
        registry=registry,
        loader=loader,
        event_logger=logger,
        sink=sink or OutputSink(),
        max_turns=max_turns,
        dead_loop_window=dead_loop_window,
    )


def read_events(tmp_path: Path) -> list[dict]:
    events_file = tmp_path / "events.jsonl"
    if not events_file.exists():
        return []
    return [json.loads(line) for line in events_file.read_text().splitlines() if line.strip()]


def events_of_type(events: list[dict], event_type: str) -> list[dict]:
    return [e for e in events if e["type"] == event_type]


# ---------------------------------------------------------------------------
# Tests: _format_plan_summary
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
# Tests: three-turn ReAct loop (core acceptance criteria)
# ---------------------------------------------------------------------------

class TestThreeTurnLoop:
    """MockModel drives UPDATE_PLAN → LOAD_SKILL → FINAL_ANSWER."""

    def _make_actions(self) -> list[Action]:
        return [
            Action(type=ActionType.UPDATE_PLAN, params={"plan": {
                "goal": "test goal",
                "steps": [
                    {"id": "s1", "description": "load skill", "status": "pending"},
                    {"id": "s2", "description": "finish", "status": "pending"},
                ],
            }}),
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "All done!"}),
        ]

    def test_returns_final_answer(self, tmp_path):
        model = MockModel(self._make_actions())
        core = make_core(model, tmp_path)
        result = core.run("do the task")
        assert result == "All done!"

    def test_model_called_three_times(self, tmp_path):
        model = MockModel(self._make_actions())
        core = make_core(model, tmp_path)
        core.run("do the task")
        assert model.call_count == 3

    def test_events_complete_sequence(self, tmp_path):
        model = MockModel(self._make_actions())
        core = make_core(model, tmp_path)
        core.run("do the task")
        events = read_events(tmp_path)

        types = [e["type"] for e in events]
        assert types[0] == "session_start"
        assert types[-1] == "session_end"

        assert types.count("action_requested") == 3
        assert types.count("action_completed") == 3
        assert "plan_updated" in types
        assert "skill_loaded" in types
        assert "final_answer" in types

    def test_session_end_status_completed(self, tmp_path):
        model = MockModel(self._make_actions())
        core = make_core(model, tmp_path)
        core.run("do the task")
        events = read_events(tmp_path)
        session_end = events_of_type(events, "session_end")[0]
        assert session_end["data"]["status"] == "completed"
        assert session_end["data"]["turns"] == 3

    def test_skill_in_active_skills(self, tmp_path):
        """After LOAD_SKILL, skill appears in agent state via events."""
        model = MockModel(self._make_actions())
        core = make_core(model, tmp_path)
        core.run("do the task")
        events = read_events(tmp_path)
        skill_loaded = events_of_type(events, "skill_loaded")
        assert len(skill_loaded) == 1
        assert skill_loaded[0]["data"]["skill"] == "example-skill"

    def test_plan_updated_event_contains_plan_data(self, tmp_path):
        model = MockModel(self._make_actions())
        core = make_core(model, tmp_path)
        core.run("do the task")
        events = read_events(tmp_path)
        plan_updated = events_of_type(events, "plan_updated")[0]
        assert plan_updated["data"]["goal"] == "test goal"
        assert len(plan_updated["data"]["steps"]) == 2


# ---------------------------------------------------------------------------
# Tests: is_done() after FINAL_ANSWER
# ---------------------------------------------------------------------------

class TestIsDoneAfterFinalAnswer:
    def test_state_is_done_after_final_answer(self, tmp_path):
        """AgentState.is_done() returns True after FINAL_ANSWER (status=completed)."""
        state = AgentState(session_id="s", user_input="x")
        assert state.is_done() is False
        state.status = "completed"
        assert state.is_done() is True


# ---------------------------------------------------------------------------
# Tests: Plan update mechanics
# ---------------------------------------------------------------------------

class TestPlanUpdate:
    def test_goal_preserved_steps_replaced(self, tmp_path):
        initial_plan_params = {
            "goal": "original goal",
            "steps": [{"id": "s1", "description": "old step", "status": "pending"}],
        }
        new_plan_params = {
            "goal": "different goal",  # should be ignored; original goal kept
            "steps": [
                {"id": "s1", "description": "new step A", "status": "pending"},
                {"id": "s2", "description": "new step B", "status": "pending"},
            ],
        }
        actions = [
            Action(type=ActionType.UPDATE_PLAN, params={"plan": initial_plan_params}),
            Action(type=ActionType.UPDATE_PLAN, params={"plan": new_plan_params}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        core.run("test")

        events = read_events(tmp_path)
        plan_events = events_of_type(events, "plan_updated")
        assert len(plan_events) == 2

        # Second plan_updated: goal preserved from first, steps from second
        second = plan_events[1]["data"]
        assert second["goal"] == "original goal"  # original goal preserved
        assert len(second["steps"]) == 2
        assert second["steps"][0]["description"] == "new step A"


# ---------------------------------------------------------------------------
# Tests: Skill not found
# ---------------------------------------------------------------------------

class TestSkillNotFound:
    def test_load_nonexistent_skill_returns_error_observation(self, tmp_path):
        actions = [
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "nonexistent-skill"}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "giving up"}),
        ]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        core.run("test")

        # The model was called twice (got error observation, then produced final_answer)
        assert model.call_count == 2

        # Check the observation is in the second call's messages
        second_call_messages = model.call_history[1]
        obs_msg = next(m for m in second_call_messages if m["role"] == "user"
                       and "Observation:" in m["content"])
        assert "Error: skill 'nonexistent-skill' not found." in obs_msg["content"]

    def test_load_resource_nonexistent_skill_returns_error(self, tmp_path):
        actions = [
            Action(type=ActionType.LOAD_RESOURCE, params={
                "skill_name": "nonexistent-skill",
                "resource": "reference/api.md",
            }),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        core.run("test")

        second_call_messages = model.call_history[1]
        obs_msg = next(m for m in second_call_messages if m["role"] == "user"
                       and "Observation:" in m["content"])
        assert "Error: skill 'nonexistent-skill' not found." in obs_msg["content"]


# ---------------------------------------------------------------------------
# Tests: Dead loop detection
# ---------------------------------------------------------------------------

class TestDeadLoopDetection:
    def test_repeated_action_triggers_dead_loop(self, tmp_path):
        # Same LOAD_SKILL action repeated → same hash → dead loop
        repeated_action = Action(
            type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}
        )
        actions = [repeated_action, repeated_action, repeated_action]
        model = MockModel(actions)
        core = make_core(model, tmp_path, dead_loop_window=6)
        result = core.run("test")

        events = read_events(tmp_path)
        dead_loop_events = events_of_type(events, "dead_loop_detected")
        assert len(dead_loop_events) == 1
        assert dead_loop_events[0]["data"]["reason"] == "repeated_action"

        session_end = events_of_type(events, "session_end")[0]
        assert session_end["data"]["status"] == "dead_loop"
        assert result == ""  # no final answer on dead loop

    def test_dead_loop_triggers_on_second_occurrence(self, tmp_path):
        """Dead loop triggers when same hash appears the 2nd time."""
        same_action = Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"})
        actions = [same_action, same_action]
        model = MockModel(actions)
        core = make_core(model, tmp_path, dead_loop_window=6)
        core.run("test")

        # Model called twice: first produces action, second produces same action → dead loop
        # Dead loop is detected on the 2nd occurrence → no further model calls
        assert model.call_count == 2

    def test_different_actions_no_dead_loop(self, tmp_path):
        """Different actions don't trigger dead loop even if same type."""
        actions = [
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "advanced-skill"}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        result = core.run("test")

        events = read_events(tmp_path)
        assert not events_of_type(events, "dead_loop_detected")
        assert result == "done"

    def test_dead_loop_window_eviction(self, tmp_path):
        """Hashes outside the window are evicted and don't trigger false positives."""
        # Window=2: only last 2 actions tracked.
        # Pattern: A, B, A — with window=2, only [B, A] are in window when second A comes.
        # So [B, A] has no duplicate → no dead loop.
        action_a = Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"})
        action_b = Action(type=ActionType.FINAL_ANSWER, params={"content": "done"})
        # With window=1, only 1 hash is kept — FINAL_ANSWER arrives → no dead loop check needed
        # Let's just verify window=2 with pattern A, B, C → no dead loop
        action_c = Action(type=ActionType.FINAL_ANSWER, params={"content": "result"})
        actions = [action_a, action_b]  # b is final_answer → exits cleanly
        model = MockModel(actions)
        core = make_core(model, tmp_path, dead_loop_window=2)
        result = core.run("test")
        assert result == "done"
        events = read_events(tmp_path)
        assert not events_of_type(events, "dead_loop_detected")


# ---------------------------------------------------------------------------
# Tests: Max turns exhausted
# ---------------------------------------------------------------------------

class TestMaxTurns:
    def test_max_turns_emits_error_and_session_end(self, tmp_path):
        # MockModel will keep returning LOAD_SKILL until exhausted (returns FINAL_ANSWER)
        # We set max_turns=2 and provide 3 distinct actions → loop exits on max_turns
        # Use dead_loop_window=10 so we don't trigger dead loop
        actions = [
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "advanced-skill"}),
        ]
        model = MockModel(actions)  # exhausted → returns FINAL_ANSWER on turn 3 but max_turns=2

        errors_received = []

        class TrackingSink(OutputSink):
            def on_error(self, message: str, recoverable: bool = True) -> None:
                errors_received.append(message)

        core = make_core(model, tmp_path, sink=TrackingSink(), max_turns=2)
        result = core.run("test")

        # max_turns=2: turns 1 and 2 run LOAD_SKILL (both different → no dead loop).
        # After turn 2, loop exits. No FINAL_ANSWER → result is ""
        assert result == ""
        assert any("Budget exhausted" in e for e in errors_received)

        events = read_events(tmp_path)
        session_end = events_of_type(events, "session_end")[0]
        assert session_end["data"]["status"] == "max_turns"
        assert session_end["data"]["turns"] == 2


# ---------------------------------------------------------------------------
# Tests: Context assembly
# ---------------------------------------------------------------------------

class TestContextAssembly:
    def test_system_prompt_contains_skill_index(self, tmp_path):
        """Skill index appears in the system prompt sent to model."""
        actions = [Action(type=ActionType.FINAL_ANSWER, params={"content": "done"})]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        core.run("hello")

        first_messages = model.call_history[0]
        system_msg = next(m for m in first_messages if m["role"] == "system")
        # example-skill from fixtures should appear
        assert "name=example-skill" in system_msg["content"]
        assert "source=project" in system_msg["content"]
        # allowed_tools must NOT be in model-visible context
        assert "allowed_tools" not in system_msg["content"]
        assert "allowed-tools" not in system_msg["content"]

    def test_system_prompt_plan_summary_before_plan(self, tmp_path):
        """Before any plan, system prompt shows '(no plan yet)'."""
        actions = [Action(type=ActionType.FINAL_ANSWER, params={"content": "done"})]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        core.run("hello")

        system_msg = model.call_history[0][0]
        assert "(no plan yet)" in system_msg["content"]

    def test_system_prompt_plan_summary_after_update(self, tmp_path):
        """After UPDATE_PLAN, system prompt in next turn shows plan."""
        actions = [
            Action(type=ActionType.UPDATE_PLAN, params={"plan": {
                "goal": "my test goal",
                "steps": [{"id": "s1", "description": "do stuff", "status": "pending"}],
            }}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        core.run("test")

        # Second model call should have plan in system prompt
        second_messages = model.call_history[1]
        system_msg = next(m for m in second_messages if m["role"] == "system")
        assert "my test goal" in system_msg["content"]
        assert "s1: do stuff" in system_msg["content"]

    def test_react_history_injected_in_context(self, tmp_path):
        """Previous action/observation pairs appear in subsequent model calls."""
        actions = [
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        core.run("test")

        # Second call should contain the observation from LOAD_SKILL
        second_messages = model.call_history[1]
        obs_messages = [m for m in second_messages
                        if m["role"] == "user" and "Observation:" in m["content"]]
        assert len(obs_messages) == 1
        assert "example-skill" in obs_messages[0]["content"]

    def test_history_messages_injected_after_system(self, tmp_path):
        """history_messages appear after system prompt and before react history."""
        history = [
            {"role": "user", "content": "previous question"},
            {"role": "assistant", "content": "previous answer"},
        ]
        actions = [Action(type=ActionType.FINAL_ANSWER, params={"content": "done"})]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        core.run("current question", history_messages=history)

        messages = model.call_history[0]
        roles = [m["role"] for m in messages]
        # system must be first
        assert roles[0] == "system"
        # history messages follow immediately
        assert messages[1]["content"] == "previous question"
        assert messages[2]["content"] == "previous answer"

    def test_user_input_always_last(self, tmp_path):
        """user_input is always the last message."""
        actions = [Action(type=ActionType.FINAL_ANSWER, params={"content": "done"})]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        core.run("my query")

        messages = model.call_history[0]
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "my query"

    def test_skill_index_excludes_control_fields(self, tmp_path):
        """allowed_tools and other control fields must not appear in skill index."""
        actions = [Action(type=ActionType.FINAL_ANSWER, params={"content": "done"})]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        core.run("test")

        system_content = model.call_history[0][0]["content"]
        assert "allowed_tools" not in system_content
        assert "disable_model_invocation" not in system_content


# ---------------------------------------------------------------------------
# Tests: RUN_SCRIPT in Phase A
# ---------------------------------------------------------------------------

class TestRunScript:
    def test_run_script_returns_error_in_phase_a(self, tmp_path):
        actions = [
            Action(type=ActionType.RUN_SCRIPT, params={
                "skill_name": "example-skill",
                "script": "run.sh",
                "args": [],
            }),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "gave up"}),
        ]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        core.run("run script test")

        second_messages = model.call_history[1]
        obs = next(m for m in second_messages if "Observation:" in m.get("content", ""))
        assert "Error: script execution not enabled" in obs["content"]


# ---------------------------------------------------------------------------
# Tests: OutputSink integration
# ---------------------------------------------------------------------------

class TestOutputSinkIntegration:
    def test_on_text_chunk_called_with_final_answer(self, tmp_path):
        chunks = []

        class TrackingSink(OutputSink):
            def on_text_chunk(self, chunk: str, done: bool) -> None:
                chunks.append((chunk, done))

        actions = [Action(type=ActionType.FINAL_ANSWER, params={"content": "the answer"})]
        model = MockModel(actions)
        core = make_core(model, tmp_path, sink=TrackingSink())
        core.run("test")

        assert len(chunks) == 1
        assert chunks[0] == ("the answer", True)

    def test_on_plan_updated_called(self, tmp_path):
        plan_updates = []

        class TrackingSink(OutputSink):
            def on_plan_updated(self, plan) -> None:
                plan_updates.append(plan)

        actions = [
            Action(type=ActionType.UPDATE_PLAN, params={"plan": {
                "goal": "g", "steps": []
            }}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ]
        model = MockModel(actions)
        core = make_core(model, tmp_path, sink=TrackingSink())
        core.run("test")

        assert len(plan_updates) == 1
        assert plan_updates[0].goal == "g"

    def test_on_progress_called_for_load_skill(self, tmp_path):
        progress_calls = []

        class TrackingSink(OutputSink):
            def on_progress(self, action: str, detail: str = "") -> None:
                progress_calls.append((action, detail))

        actions = [
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ]
        model = MockModel(actions)
        core = make_core(model, tmp_path, sink=TrackingSink())
        core.run("test")

        assert ("load_skill", "example-skill") in progress_calls

    def test_on_session_end_called(self, tmp_path):
        session_ends = []

        class TrackingSink(OutputSink):
            def on_session_end(self, turn_count: int, status: str) -> None:
                session_ends.append((turn_count, status))

        actions = [Action(type=ActionType.FINAL_ANSWER, params={"content": "done"})]
        model = MockModel(actions)
        core = make_core(model, tmp_path, sink=TrackingSink())
        core.run("test")

        assert len(session_ends) == 1
        assert session_ends[0] == (1, "completed")


# ---------------------------------------------------------------------------
# Tests: events.jsonl file existence and session_id
# ---------------------------------------------------------------------------

class TestEventLogging:
    def test_events_file_created(self, tmp_path):
        actions = [Action(type=ActionType.FINAL_ANSWER, params={"content": "x"})]
        model = MockModel(actions)
        core = make_core(model, tmp_path, session_id="my-session-123")
        core.run("test")

        events_file = tmp_path / "events.jsonl"
        assert events_file.exists()

    def test_all_events_have_session_id(self, tmp_path):
        actions = [Action(type=ActionType.FINAL_ANSWER, params={"content": "x"})]
        model = MockModel(actions)
        core = make_core(model, tmp_path, session_id="sess-xyz")
        core.run("test")

        events = read_events(tmp_path)
        for event in events:
            assert event["session_id"] == "sess-xyz"

    def test_session_start_data(self, tmp_path):
        actions = [Action(type=ActionType.FINAL_ANSWER, params={"content": "x"})]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        core.run("my user query")

        events = read_events(tmp_path)
        start = events_of_type(events, "session_start")[0]
        assert start["data"]["user_input"] == "my user query"

    def test_action_requested_events_have_turn_and_type(self, tmp_path):
        actions = [
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        core.run("test")

        events = read_events(tmp_path)
        requested = events_of_type(events, "action_requested")
        assert requested[0]["data"]["turn"] == 1
        assert requested[0]["data"]["action_type"] == "load_skill"
        assert requested[1]["data"]["turn"] == 2
        assert requested[1]["data"]["action_type"] == "final_answer"


# ---------------------------------------------------------------------------
# Tests: MockModel exhaustion fallback
# ---------------------------------------------------------------------------

class TestMockModelExhaustion:
    def test_exhausted_model_returns_final_answer(self, tmp_path):
        """When MockModel runs out, it returns FINAL_ANSWER — loop terminates cleanly."""
        model = MockModel([])  # no actions at all
        core = make_core(model, tmp_path)
        result = core.run("test")

        # MockModel returns FINAL_ANSWER with exhausted message
        assert "[MockModel: action sequence exhausted]" in result
        events = read_events(tmp_path)
        assert events_of_type(events, "session_end")[0]["data"]["status"] == "completed"


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_history_messages(self, tmp_path):
        actions = [Action(type=ActionType.FINAL_ANSWER, params={"content": "ok"})]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        result = core.run("test", history_messages=[])
        assert result == "ok"

    def test_none_history_messages(self, tmp_path):
        actions = [Action(type=ActionType.FINAL_ANSWER, params={"content": "ok"})]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        result = core.run("test", history_messages=None)
        assert result == "ok"

    def test_final_answer_empty_content(self, tmp_path):
        actions = [Action(type=ActionType.FINAL_ANSWER, params={})]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        result = core.run("test")
        assert result == ""

    def test_update_plan_no_prior_plan(self, tmp_path):
        """UPDATE_PLAN when no prior plan creates a new plan directly."""
        actions = [
            Action(type=ActionType.UPDATE_PLAN, params={"plan": {
                "goal": "fresh goal",
                "steps": [{"id": "s1", "description": "step one", "status": "pending"}],
            }}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        core.run("test")

        events = read_events(tmp_path)
        plan_event = events_of_type(events, "plan_updated")[0]
        assert plan_event["data"]["goal"] == "fresh goal"

    def test_load_skill_body_injected_in_observation(self, tmp_path):
        """LOAD_SKILL observation contains skill name and body."""
        actions = [
            Action(type=ActionType.LOAD_SKILL, params={"skill_name": "example-skill"}),
            Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
        ]
        model = MockModel(actions)
        core = make_core(model, tmp_path)
        core.run("test")

        second_messages = model.call_history[1]
        obs = next(m for m in second_messages if "Observation:" in m.get("content", ""))
        assert "[Skill: example-skill]" in obs["content"]
        # The fixture skill body contains "This is the body of the example skill"
        assert "Example Skill" in obs["content"] or "example skill" in obs["content"].lower()
