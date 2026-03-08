"""Unit tests for Plan, Step, Action data structures."""

from agent.plan import Action, ActionType, Plan, Step, StepStatus


def test_current_step_skips_done():
    plan = Plan(goal="test", steps=[
        Step(id="s1", description="first", status=StepStatus.DONE),
        Step(id="s2", description="second"),
        Step(id="s3", description="third"),
    ])
    assert plan.current_step().id == "s2"


def test_replace_preserves_goal():
    old = Plan(goal="my goal", steps=[Step(id="s1", description="old")])
    new_plan = Plan(goal="ignored", steps=[Step(id="s2", description="new")])
    result = old.replace(new_plan)
    assert result.goal == "my goal"
    assert result.steps[0].id == "s2"


def test_replace_preserves_created_at():
    old = Plan(goal="goal", steps=[])
    new_plan = Plan(goal="goal", steps=[Step(id="s1", description="step")])
    result = old.replace(new_plan)
    assert result.created_at == old.created_at
    assert result.updated_at >= old.updated_at


def test_current_step_all_done_returns_none():
    plan = Plan(goal="test", steps=[
        Step(id="s1", description="first", status=StepStatus.DONE),
        Step(id="s2", description="second", status=StepStatus.FAILED),
    ])
    assert plan.current_step() is None


def test_current_step_empty_returns_none():
    plan = Plan(goal="test", steps=[])
    assert plan.current_step() is None


def test_plan_serializes_to_json():
    plan = Plan(goal="test", steps=[Step(id="s1", description="step")])
    json_str = plan.model_dump_json()
    restored = Plan.model_validate_json(json_str)
    assert restored.goal == plan.goal
    assert restored.steps[0].id == "s1"


def test_action_load_skill():
    action = Action(type=ActionType.LOAD_SKILL, params={"skill_name": "my-skill"})
    assert action.type == ActionType.LOAD_SKILL
    assert action.params["skill_name"] == "my-skill"


def test_action_final_answer():
    action = Action(type=ActionType.FINAL_ANSWER, params={"content": "done"})
    assert action.type == ActionType.FINAL_ANSWER
