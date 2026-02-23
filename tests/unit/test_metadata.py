import pytest
from pathlib import Path
from src.skills.metadata import ResourceLimits, SkillMetadata, LoadedSkill


# ──────────────────────────────────────────
# ResourceLimits
# ──────────────────────────────────────────

def test_resource_limits_defaults():
    limits = ResourceLimits()
    assert limits.max_script_time_sec == 30
    assert limits.max_memory_mb == 512
    assert limits.max_concurrent_scripts == 2
    assert limits.allow_network is False


def test_resource_limits_serialization():
    limits = ResourceLimits(
        max_script_time_sec=60,
        max_memory_mb=256,
        max_concurrent_scripts=4,
        allow_network=True,
    )
    data = limits.to_dict()
    restored = ResourceLimits.from_dict(data)
    assert restored.max_script_time_sec == 60
    assert restored.max_memory_mb == 256
    assert restored.max_concurrent_scripts == 4
    assert restored.allow_network is True


def test_resource_limits_from_dict_defaults():
    restored = ResourceLimits.from_dict({})
    assert restored.max_script_time_sec == 30
    assert restored.max_memory_mb == 512
    assert restored.allow_network is False


# ──────────────────────────────────────────
# SkillMetadata — generate_skill_id
# ──────────────────────────────────────────

def test_generate_skill_id_with_version():
    sid = SkillMetadata.generate_skill_id("project", "my-skill", "1.2.3")
    assert sid == "project:my-skill:1.2.3"


def test_generate_skill_id_no_version():
    sid = SkillMetadata.generate_skill_id("builtin", "base-utils")
    assert sid == "builtin:base-utils:unversioned"


def test_generate_skill_id_none_version():
    sid = SkillMetadata.generate_skill_id("user", "tool", None)
    assert sid == "user:tool:unversioned"


# ──────────────────────────────────────────
# SkillMetadata — serialization
# ──────────────────────────────────────────

def _make_metadata(**kwargs) -> SkillMetadata:
    defaults = dict(
        skill_id="project:test-skill:1.0",
        name="test-skill",
        description="A test skill",
        source="project",
        path=Path("/skills/test-skill"),
        version="1.0",
    )
    defaults.update(kwargs)
    return SkillMetadata(**defaults)


def test_skill_metadata_serialization():
    meta = _make_metadata()
    data = meta.to_dict()
    restored = SkillMetadata.from_dict(data)

    assert restored.skill_id == meta.skill_id
    assert restored.name == meta.name
    assert restored.description == meta.description
    assert restored.source == meta.source
    assert restored.path == meta.path
    assert restored.version == meta.version


def test_skill_metadata_optional_fields():
    meta = _make_metadata(
        author="Alice",
        allowed_tools=["read_file", "grep"],
        disable_model_invocation=True,
        user_invocable=False,
        requires=["base-utils"],
        load_priority="high",
        frontmatter_hash="abc123",
        scanned_at="2024-01-01T00:00:00",
    )
    data = meta.to_dict()
    restored = SkillMetadata.from_dict(data)

    assert restored.author == "Alice"
    assert restored.allowed_tools == ["read_file", "grep"]
    assert restored.disable_model_invocation is True
    assert restored.user_invocable is False
    assert restored.requires == ["base-utils"]
    assert restored.load_priority == "high"
    assert restored.frontmatter_hash == "abc123"
    assert restored.scanned_at == "2024-01-01T00:00:00"


def test_skill_metadata_from_dict_defaults():
    data = {
        "skill_id": "x:y:z",
        "name": "y",
        "description": "desc",
        "source": "x",
        "path": "/tmp/skill",
    }
    meta = SkillMetadata.from_dict(data)
    assert meta.version is None
    assert meta.author is None
    assert meta.allowed_tools is None
    assert meta.disable_model_invocation is False
    assert meta.user_invocable is True
    assert meta.requires == []
    assert meta.load_priority == "normal"


def test_skill_metadata_path_roundtrip():
    meta = _make_metadata(path=Path("/some/deep/path/skill"))
    data = meta.to_dict()
    assert isinstance(data["path"], str)
    restored = SkillMetadata.from_dict(data)
    assert restored.path == Path("/some/deep/path/skill")


def test_skill_metadata_resource_limits_roundtrip():
    limits = ResourceLimits(max_script_time_sec=90, allow_network=True)
    meta = _make_metadata(resource_limits=limits)
    data = meta.to_dict()
    restored = SkillMetadata.from_dict(data)
    assert restored.resource_limits.max_script_time_sec == 90
    assert restored.resource_limits.allow_network is True


# ──────────────────────────────────────────
# SkillMetadata — get_priority_score
# ──────────────────────────────────────────

def test_priority_score_high():
    meta = _make_metadata(load_priority="high")
    assert meta.get_priority_score() == 0


def test_priority_score_normal():
    meta = _make_metadata(load_priority="normal")
    assert meta.get_priority_score() == 1


def test_priority_score_low():
    meta = _make_metadata(load_priority="low")
    assert meta.get_priority_score() == 2


def test_priority_score_unknown_defaults_to_normal():
    meta = _make_metadata(load_priority="unknown")
    assert meta.get_priority_score() == 1


def test_priority_sorting():
    metas = [
        _make_metadata(name="low-skill", load_priority="low"),
        _make_metadata(name="high-skill", load_priority="high"),
        _make_metadata(name="normal-skill", load_priority="normal"),
    ]
    sorted_metas = sorted(metas, key=lambda m: m.get_priority_score())
    assert sorted_metas[0].name == "high-skill"
    assert sorted_metas[1].name == "normal-skill"
    assert sorted_metas[2].name == "low-skill"


# ──────────────────────────────────────────
# SkillMetadata — __str__
# ──────────────────────────────────────────

def test_str_with_version():
    meta = _make_metadata(source="project", name="my-skill", version="2.0")
    s = str(meta)
    assert "project" in s
    assert "my-skill" in s
    assert "2.0" in s


def test_str_without_version():
    meta = _make_metadata(source="builtin", name="base", version=None)
    s = str(meta)
    assert "builtin" in s
    assert "base" in s
    assert "@" not in s


# ──────────────────────────────────────────
# LoadedSkill
# ──────────────────────────────────────────

def test_loaded_skill_to_dict():
    meta = _make_metadata()
    skill = LoadedSkill(
        metadata=meta,
        body="# Skill content\nDo stuff.",
        loaded_at_turn=3,
        token_estimate=42,
        body_hash="deadbeef",
    )
    data = skill.to_dict()
    assert data["loaded_at_turn"] == 3
    assert data["token_estimate"] == 42
    assert data["body_hash"] == "deadbeef"
    assert "metadata" in data
    assert "body_preview" in data


def test_loaded_skill_body_preview_short():
    meta = _make_metadata()
    body = "Short body"
    skill = LoadedSkill(metadata=meta, body=body, loaded_at_turn=0, token_estimate=5)
    data = skill.to_dict()
    assert data["body_preview"] == body


def test_loaded_skill_body_preview_truncated():
    meta = _make_metadata()
    body = "x" * 300
    skill = LoadedSkill(metadata=meta, body=body, loaded_at_turn=0, token_estimate=75)
    data = skill.to_dict()
    assert data["body_preview"].endswith("...")
    assert len(data["body_preview"]) == 203  # 200 + "..."


def test_loaded_skill_token_estimate():
    meta = _make_metadata()
    skill = LoadedSkill(metadata=meta, body="content", loaded_at_turn=1, token_estimate=100)
    assert skill.token_estimate == 100


def test_loaded_skill_no_body_hash():
    meta = _make_metadata()
    skill = LoadedSkill(metadata=meta, body="text", loaded_at_turn=0, token_estimate=1)
    assert skill.body_hash is None
    data = skill.to_dict()
    assert data["body_hash"] is None
