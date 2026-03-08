"""Unit tests for SkillRegistry."""
import textwrap
from pathlib import Path

import pytest

from skills.registry import SkillRegistry


# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_skill(tmp_path: Path, dir_name: str, name: str, description: str, extra: str = "") -> Path:
    """Create a skill directory with a minimal SKILL.md."""
    skill_dir = tmp_path / dir_name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(f"""\
            ---
            name: {name}
            description: {description}
            {extra}
            ---
            # Body
        """),
        encoding="utf-8",
    )
    return skill_dir


# ─── scan() ───────────────────────────────────────────────────────────────────

class TestScan:
    def test_returns_skills_from_fixtures(self, fixtures_skills_root):
        registry = SkillRegistry([{"source": "project", "path": str(fixtures_skills_root)}])
        skills = registry.scan()
        assert len(skills) == 2
        names = {s.name for s in skills}
        assert names == {"example-skill", "advanced-skill"}

    def test_skill_path_points_to_skill_md(self, fixtures_skills_root):
        registry = SkillRegistry([{"source": "project", "path": str(fixtures_skills_root)}])
        skills = registry.scan()
        for meta in skills:
            assert meta.skill_path.endswith("SKILL.md")
            assert Path(meta.skill_path).exists()

    def test_source_set_correctly(self, fixtures_skills_root):
        registry = SkillRegistry([{"source": "builtin", "path": str(fixtures_skills_root)}])
        for meta in registry.scan():
            assert meta.source == "builtin"

    def test_nonexistent_root_ignored(self, tmp_path):
        registry = SkillRegistry([{"source": "project", "path": str(tmp_path / "nonexistent")}])
        assert registry.scan() == []

    def test_empty_root_returns_empty(self, tmp_path):
        registry = SkillRegistry([{"source": "project", "path": str(tmp_path)}])
        assert registry.scan() == []

    def test_hidden_dirs_skipped(self, tmp_path):
        hidden = tmp_path / ".hidden-skill"
        hidden.mkdir()
        (hidden / "SKILL.md").write_text(
            "---\nname: hidden\ndescription: hidden skill\n---\n# Body\n",
            encoding="utf-8",
        )
        registry = SkillRegistry([{"source": "project", "path": str(tmp_path)}])
        assert registry.scan() == []

    def test_dir_without_skill_md_skipped(self, tmp_path):
        (tmp_path / "no-skill-md").mkdir()
        registry = SkillRegistry([{"source": "project", "path": str(tmp_path)}])
        assert registry.scan() == []

    def test_invalid_frontmatter_skill_ignored(self, tmp_path):
        """A bad skill doesn't prevent other valid skills from being indexed."""
        make_skill(tmp_path, "good-skill", "good-skill", "Good skill")
        bad_dir = tmp_path / "bad-skill"
        bad_dir.mkdir()
        (bad_dir / "SKILL.md").write_text("not-frontmatter at all", encoding="utf-8")

        registry = SkillRegistry([{"source": "project", "path": str(tmp_path)}])
        skills = registry.scan()
        assert len(skills) == 1
        assert skills[0].name == "good-skill"

    def test_missing_name_skill_ignored(self, tmp_path):
        bad_dir = tmp_path / "no-name"
        bad_dir.mkdir()
        (bad_dir / "SKILL.md").write_text(
            "---\ndescription: No name here\n---\n# Body\n", encoding="utf-8"
        )
        registry = SkillRegistry([{"source": "project", "path": str(tmp_path)}])
        assert registry.scan() == []

    def test_injection_char_skill_ignored(self, tmp_path):
        """SKILL.md containing '<' is rejected and the skill is skipped."""
        bad_dir = tmp_path / "inject-skill"
        bad_dir.mkdir()
        (bad_dir / "SKILL.md").write_text(
            "---\nname: inject\ndescription: bad <script>\n---\n# Body\n",
            encoding="utf-8",
        )
        registry = SkillRegistry([{"source": "project", "path": str(tmp_path)}])
        assert registry.scan() == []

    def test_scan_refreshes_cache(self, tmp_path):
        registry = SkillRegistry([{"source": "project", "path": str(tmp_path)}])
        assert registry.scan() == []
        make_skill(tmp_path, "new-skill", "new-skill", "A new skill")
        result = registry.scan()
        assert len(result) == 1


# ─── Conflict resolution ──────────────────────────────────────────────────────

class TestConflictResolution:
    def test_lower_priority_wins(self, tmp_path):
        """project (priority=0) should win over user (priority=1) for same skill name."""
        project_root = tmp_path / "project"
        user_root = tmp_path / "user"
        project_root.mkdir()
        user_root.mkdir()

        make_skill(project_root, "my-skill", "my-skill", "from project")
        make_skill(user_root, "my-skill", "my-skill", "from user")

        registry = SkillRegistry([
            {"source": "project", "path": str(project_root), "priority": 0},
            {"source": "user", "path": str(user_root), "priority": 1},
        ])
        skills = registry.scan()
        assert len(skills) == 1
        assert skills[0].source == "project"
        assert skills[0].description == "from project"

    def test_higher_priority_number_loses(self, tmp_path):
        """user (priority=2) loses to builtin (priority=1)."""
        builtin_root = tmp_path / "builtin"
        user_root = tmp_path / "user"
        builtin_root.mkdir()
        user_root.mkdir()

        make_skill(builtin_root, "shared", "shared", "from builtin")
        make_skill(user_root, "shared", "shared", "from user")

        registry = SkillRegistry([
            {"source": "builtin", "path": str(builtin_root), "priority": 1},
            {"source": "user", "path": str(user_root), "priority": 2},
        ])
        skills = registry.scan()
        assert len(skills) == 1
        assert skills[0].source == "builtin"

    def test_unique_skills_from_multiple_roots(self, tmp_path):
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        root_a.mkdir()
        root_b.mkdir()

        make_skill(root_a, "skill-a", "skill-a", "skill from a")
        make_skill(root_b, "skill-b", "skill-b", "skill from b")

        registry = SkillRegistry([
            {"source": "a", "path": str(root_a), "priority": 0},
            {"source": "b", "path": str(root_b), "priority": 1},
        ])
        assert len(registry.scan()) == 2


# ─── find() ───────────────────────────────────────────────────────────────────

class TestFind:
    def test_find_by_name(self, fixtures_skills_root):
        registry = SkillRegistry([{"source": "project", "path": str(fixtures_skills_root)}])
        meta = registry.find("example-skill")
        assert meta is not None
        assert meta.name == "example-skill"

    def test_find_skill_path_correct(self, fixtures_skills_root):
        registry = SkillRegistry([{"source": "project", "path": str(fixtures_skills_root)}])
        meta = registry.find("example-skill")
        assert meta is not None
        assert meta.skill_path == str(fixtures_skills_root / "example-skill" / "SKILL.md")

    def test_find_nonexistent_returns_none(self, fixtures_skills_root):
        registry = SkillRegistry([{"source": "project", "path": str(fixtures_skills_root)}])
        assert registry.find("nonexistent-skill") is None

    def test_find_with_source_filter(self, tmp_path):
        root = tmp_path / "skills"
        root.mkdir()
        make_skill(root, "my-skill", "my-skill", "test skill")

        registry = SkillRegistry([{"source": "project", "path": str(root)}])
        assert registry.find("my-skill", source="project") is not None
        assert registry.find("my-skill", source="user") is None

    def test_find_triggers_scan_if_not_scanned(self, fixtures_skills_root):
        registry = SkillRegistry([{"source": "project", "path": str(fixtures_skills_root)}])
        assert not registry._scanned
        result = registry.find("example-skill")
        assert result is not None
        assert registry._scanned


# ─── to_index_text() ──────────────────────────────────────────────────────────

class TestToIndexText:
    def test_format(self, fixtures_skills_root):
        registry = SkillRegistry([{"source": "project", "path": str(fixtures_skills_root)}])
        text = registry.to_index_text()
        assert "name=example-skill" in text
        assert "source=project" in text
        assert "description=" in text

    def test_no_control_fields_in_output(self, fixtures_skills_root):
        registry = SkillRegistry([{"source": "project", "path": str(fixtures_skills_root)}])
        text = registry.to_index_text()
        assert "allowed_tools" not in text
        assert "allowed-tools" not in text
        assert "skill_path" not in text
        assert "resource_limits" not in text

    def test_empty_returns_placeholder(self, tmp_path):
        registry = SkillRegistry([{"source": "project", "path": str(tmp_path)}])
        assert registry.to_index_text() == "(no skills available)"

    def test_disable_model_invocation_excluded(self, tmp_path):
        """Skills with disable-model-invocation: true must not appear in index text."""
        root = tmp_path / "skills"
        root.mkdir()
        make_skill(root, "visible-skill", "visible-skill", "Visible")
        make_skill(
            root, "hidden-skill", "hidden-skill", "Hidden",
            extra="disable-model-invocation: true",
        )
        registry = SkillRegistry([{"source": "project", "path": str(root)}])
        text = registry.to_index_text()
        assert "visible-skill" in text
        assert "hidden-skill" not in text


# ─── list_model_views() ───────────────────────────────────────────────────────

class TestListModelViews:
    def test_returns_only_name_description_source(self, fixtures_skills_root):
        registry = SkillRegistry([{"source": "project", "path": str(fixtures_skills_root)}])
        views = registry.list_model_views()
        assert len(views) == 2
        for view in views:
            assert set(view.keys()) == {"name", "description", "source"}

    def test_excludes_disabled_skills(self, tmp_path):
        root = tmp_path / "skills"
        root.mkdir()
        make_skill(root, "ok-skill", "ok-skill", "OK")
        make_skill(
            root, "disabled-skill", "disabled-skill", "Disabled",
            extra="disable-model-invocation: true",
        )
        registry = SkillRegistry([{"source": "project", "path": str(root)}])
        views = registry.list_model_views()
        names = [v["name"] for v in views]
        assert "ok-skill" in names
        assert "disabled-skill" not in names


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def fixtures_skills_root() -> Path:
    return Path(__file__).parent.parent / "fixtures" / "skills"
