import pytest
from src.agent.actions import (
    SkillReference,
    SelectSkillsAction,
    LoadResourceAction,
    RunScriptAction,
    FinalAnswerAction,
    PlanUpdateAction,
    parse_action,
)


# ──────────────────────────────────────────
# SkillReference
# ──────────────────────────────────────────

def test_skill_reference_serialization():
    ref = SkillReference(name="my-skill", source="project")
    data = ref.to_dict()
    restored = SkillReference.from_dict(data)
    assert restored.name == "my-skill"
    assert restored.source == "project"


def test_skill_reference_no_source():
    ref = SkillReference(name="test")
    data = ref.to_dict()
    restored = SkillReference.from_dict(data)
    assert restored.source is None


# ──────────────────────────────────────────
# SelectSkillsAction
# ──────────────────────────────────────────

def test_select_skills_action():
    """测试选择技能动作"""
    action = SelectSkillsAction(
        skills=[SkillReference(name="test", source="project")],
        reason="Testing",
    )
    assert action.validate() is True
    data = action.to_dict()
    assert data["action"] == "select_skills"

    restored = parse_action(data)
    assert isinstance(restored, SelectSkillsAction)
    assert restored.skills[0].name == "test"
    assert restored.reason == "Testing"


def test_select_skills_action_type():
    action = SelectSkillsAction(skills=[SkillReference(name="x")], reason="r")
    assert action.action_type() == "select_skills"


def test_select_skills_validate_empty_skills():
    action = SelectSkillsAction(skills=[], reason="some reason")
    assert action.validate() is False


def test_select_skills_validate_empty_reason():
    action = SelectSkillsAction(skills=[SkillReference(name="x")], reason="")
    assert action.validate() is False


def test_select_skills_multiple_skills():
    action = SelectSkillsAction(
        skills=[
            SkillReference(name="skill-a", source="builtin"),
            SkillReference(name="skill-b", source="user"),
        ],
        reason="Need both",
    )
    assert action.validate() is True
    data = action.to_dict()
    restored = SelectSkillsAction.from_dict(data)
    assert len(restored.skills) == 2
    assert restored.skills[1].name == "skill-b"


# ──────────────────────────────────────────
# LoadResourceAction
# ──────────────────────────────────────────

def test_load_resource_action():
    action = LoadResourceAction(
        skill=SkillReference(name="test"),
        relative_path="docs/guide.md",
        section_hint="Installation",
    )
    assert action.validate() is True
    data = action.to_dict()
    assert data["action"] == "load_resource"

    restored = parse_action(data)
    assert isinstance(restored, LoadResourceAction)
    assert restored.relative_path == "docs/guide.md"
    assert restored.section_hint == "Installation"


def test_load_resource_path_traversal():
    """测试路径穿越检测"""
    action = LoadResourceAction(
        skill=SkillReference(name="test"),
        relative_path="../../../etc/passwd",
    )
    assert action.validate() is False


def test_load_resource_path_traversal_embedded():
    action = LoadResourceAction(
        skill=SkillReference(name="test"),
        relative_path="docs/../../../secret",
    )
    assert action.validate() is False


def test_load_resource_empty_path():
    action = LoadResourceAction(
        skill=SkillReference(name="test"),
        relative_path="",
    )
    assert action.validate() is False


def test_load_resource_no_section_hint():
    action = LoadResourceAction(
        skill=SkillReference(name="test"),
        relative_path="README.md",
    )
    assert action.validate() is True
    data = action.to_dict()
    restored = LoadResourceAction.from_dict(data)
    assert restored.section_hint is None


def test_load_resource_action_type():
    action = LoadResourceAction(skill=SkillReference(name="x"), relative_path="f.md")
    assert action.action_type() == "load_resource"


# ──────────────────────────────────────────
# RunScriptAction
# ──────────────────────────────────────────

def test_run_script_action():
    action = RunScriptAction(
        skill=SkillReference(name="test", source="project"),
        relative_path="scripts/run.sh",
        args=["--verbose"],
        env={"DEBUG": "1"},
    )
    assert action.validate() is True
    data = action.to_dict()
    assert data["action"] == "run_script"

    restored = parse_action(data)
    assert isinstance(restored, RunScriptAction)
    assert restored.relative_path == "scripts/run.sh"
    assert restored.args == ["--verbose"]
    assert restored.env == {"DEBUG": "1"}


def test_run_script_path_traversal():
    action = RunScriptAction(
        skill=SkillReference(name="test"),
        relative_path="../evil.sh",
    )
    assert action.validate() is False


def test_run_script_empty_path():
    action = RunScriptAction(
        skill=SkillReference(name="test"),
        relative_path="",
    )
    assert action.validate() is False


def test_run_script_defaults():
    action = RunScriptAction(
        skill=SkillReference(name="test"),
        relative_path="scripts/go.py",
    )
    assert action.args == []
    assert action.env == {}
    data = action.to_dict()
    restored = RunScriptAction.from_dict(data)
    assert restored.args == []
    assert restored.env == {}


def test_run_script_action_type():
    action = RunScriptAction(skill=SkillReference(name="x"), relative_path="s.sh")
    assert action.action_type() == "run_script"


# ──────────────────────────────────────────
# FinalAnswerAction
# ──────────────────────────────────────────

def test_final_answer_action():
    action = FinalAnswerAction(answer="Done!", completed=True)
    assert action.validate() is True
    data = action.to_dict()
    assert data["action"] == "final_answer"

    restored = parse_action(data)
    assert isinstance(restored, FinalAnswerAction)
    assert restored.answer == "Done!"
    assert restored.completed is True


def test_final_answer_empty_answer():
    action = FinalAnswerAction(answer="")
    assert action.validate() is False


def test_final_answer_not_completed():
    action = FinalAnswerAction(answer="Partial", completed=False)
    assert action.validate() is True
    data = action.to_dict()
    restored = FinalAnswerAction.from_dict(data)
    assert restored.completed is False


def test_final_answer_default_completed():
    data = {"action": "final_answer", "answer": "Hi"}
    restored = FinalAnswerAction.from_dict(data)
    assert restored.completed is True


def test_final_answer_action_type():
    action = FinalAnswerAction(answer="x")
    assert action.action_type() == "final_answer"


# ──────────────────────────────────────────
# PlanUpdateAction
# ──────────────────────────────────────────

def test_plan_update_action():
    action = PlanUpdateAction(updates={"step": "s1", "status": "completed"})
    assert action.validate() is True
    data = action.to_dict()
    assert data["action"] == "plan_update"

    restored = parse_action(data)
    assert isinstance(restored, PlanUpdateAction)
    assert restored.updates["step"] == "s1"


def test_plan_update_empty_updates():
    action = PlanUpdateAction(updates={})
    assert action.validate() is False


def test_plan_update_action_type():
    action = PlanUpdateAction(updates={"k": "v"})
    assert action.action_type() == "plan_update"


# ──────────────────────────────────────────
# parse_action 工厂函数
# ──────────────────────────────────────────

def test_parse_action_unknown_type():
    with pytest.raises(ValueError, match="Unknown action type"):
        parse_action({"action": "do_something_weird"})


def test_parse_action_missing_type():
    with pytest.raises(ValueError, match="Unknown action type"):
        parse_action({})


def test_parse_action_all_types():
    """验证所有 5 种动作类型均可通过 parse_action 正确解析"""
    cases = [
        SelectSkillsAction(skills=[SkillReference(name="s")], reason="r"),
        LoadResourceAction(skill=SkillReference(name="s"), relative_path="f.md"),
        RunScriptAction(skill=SkillReference(name="s"), relative_path="run.sh"),
        FinalAnswerAction(answer="done"),
        PlanUpdateAction(updates={"k": "v"}),
    ]
    for original in cases:
        data = original.to_dict()
        restored = parse_action(data)
        assert type(restored) is type(original)
        assert restored.action_type() == original.action_type()
