"""Unit tests for AgentState."""

from agent.plan import Plan, Step, StepStatus
from agent.state import AgentState
from skills.metadata import SkillMetadata


def _make_state(**kwargs) -> AgentState:
    defaults = {"session_id": "sess-1", "user_input": "do something"}
    return AgentState(**{**defaults, **kwargs})


def _make_skill(name: str) -> SkillMetadata:
    return SkillMetadata(
        name=name,
        description="test skill",
        source="project",
        skill_path=f"/tmp/{name}/SKILL.md",
    )


def test_is_done_no_plan_returns_false():
    state = _make_state()
    assert state.is_done() is False


def test_is_done_dead_loop_returns_true():
    state = _make_state(dead_loop_triggered=True)
    assert state.is_done() is True


def test_is_done_dead_loop_even_without_plan():
    state = _make_state(dead_loop_triggered=True, plan=None)
    assert state.is_done() is True


def test_is_done_all_steps_done():
    plan = Plan(goal="g", steps=[
        Step(id="s1", description="first", status=StepStatus.DONE),
        Step(id="s2", description="second", status=StepStatus.FAILED),
    ])
    state = _make_state(plan=plan)
    assert state.is_done() is True


def test_is_done_pending_step():
    plan = Plan(goal="g", steps=[
        Step(id="s1", description="first", status=StepStatus.DONE),
        Step(id="s2", description="second", status=StepStatus.PENDING),
    ])
    state = _make_state(plan=plan)
    assert state.is_done() is False


def test_is_done_in_progress_step():
    plan = Plan(goal="g", steps=[
        Step(id="s1", description="first", status=StepStatus.IN_PROGRESS),
    ])
    state = _make_state(plan=plan)
    assert state.is_done() is False


def test_is_done_empty_plan_steps():
    plan = Plan(goal="g", steps=[])
    state = _make_state(plan=plan)
    assert state.is_done() is True  # all() on empty iterable is True


def test_defaults():
    state = _make_state()
    assert state.turn_count == 0
    assert state.last_plan_progress_turn == 0
    assert state.recent_action_hashes == []
    assert state.active_skills == []
    assert state.dead_loop_triggered is False


def test_active_skills_mutable():
    state = _make_state()
    skill = _make_skill("my-skill")
    state.active_skills.append(skill)
    assert len(state.active_skills) == 1
    assert state.active_skills[0].name == "my-skill"


def test_recent_action_hashes_mutable():
    state = _make_state()
    state.recent_action_hashes.append("abc123")
    assert state.recent_action_hashes == ["abc123"]
