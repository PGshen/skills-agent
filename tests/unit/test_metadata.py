"""Unit tests for SkillMetadata and ResourceLimits."""

from skills.metadata import ResourceLimits, SkillMetadata


def test_to_model_view_excludes_controls():
    meta = SkillMetadata(
        name="test-skill",
        description="A test skill",
        source="project",
        skill_path="/tmp/test/SKILL.md",
        allowed_tools=["read_file", "run_script"],
        requires=["base-utils"],
    )
    view = meta.to_model_view()
    assert set(view.keys()) == {"name", "description", "source"}
    assert "allowed_tools" not in view
    assert "resource_limits" not in view
    assert "requires" not in view
    assert "disable_model_invocation" not in view


def test_resource_limits_defaults():
    limits = ResourceLimits()
    assert limits.max_script_time_sec == 30
    assert limits.max_concurrent_scripts == 2
    assert limits.max_memory_mb is None


def test_disable_model_invocation_default_false():
    meta = SkillMetadata(
        name="test-skill",
        description="A test skill",
        source="project",
        skill_path="/tmp/test/SKILL.md",
    )
    assert meta.disable_model_invocation is False
    assert meta.load_priority == "normal"
    assert meta.requires == []
