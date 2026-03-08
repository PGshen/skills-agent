"""Unit tests for SkillLoader."""
import textwrap
from pathlib import Path

import pytest

from skills.loader import PathTraversalError, SkillLoader
from skills.metadata import SkillMetadata


# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_metadata(skill_path: str) -> SkillMetadata:
    return SkillMetadata(
        name="test-skill",
        description="A test skill",
        source="project",
        skill_path=skill_path,
    )


def make_skill_dir(tmp_path: Path, body: str = "# Body\n\nSome content.\n") -> SkillMetadata:
    """Create a minimal skill directory and return its SkillMetadata."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f"---\nname: test-skill\ndescription: A test skill\n---\n{body}",
        encoding="utf-8",
    )
    return make_metadata(str(skill_md))


# ─── load_body() ──────────────────────────────────────────────────────────────

class TestLoadBody:
    def test_returns_body_without_frontmatter(self, tmp_path):
        meta = make_skill_dir(tmp_path, body="# Hello\n\nThis is the body.\n")
        loader = SkillLoader()
        body, _ = loader.load_body(meta)
        assert "Hello" in body
        assert "---" not in body
        assert "name:" not in body

    def test_returns_stats_dict(self, tmp_path):
        meta = make_skill_dir(tmp_path, body="# Hello\n\nLine two.\n")
        loader = SkillLoader()
        _, report = loader.load_body(meta)
        assert "chars" in report
        assert "lines" in report
        assert "skill_name" in report
        assert report["skill_name"] == "test-skill"
        assert isinstance(report["chars"], int)
        assert isinstance(report["lines"], int)

    def test_chars_matches_body_length(self, tmp_path):
        body_text = "# Title\n\nContent here.\n"
        meta = make_skill_dir(tmp_path, body=body_text)
        loader = SkillLoader()
        body, report = loader.load_body(meta)
        assert report["chars"] == len(body)

    def test_lines_count(self, tmp_path):
        body_text = "line1\nline2\nline3\n"
        meta = make_skill_dir(tmp_path, body=body_text)
        loader = SkillLoader()
        body, report = loader.load_body(meta)
        assert report["lines"] == body.count("\n") + (
            1 if body and not body.endswith("\n") else 0
        )

    def test_load_body_from_fixture(self):
        fixture = Path(__file__).parent.parent / "fixtures" / "skills" / "example-skill" / "SKILL.md"
        meta = make_metadata(str(fixture))
        loader = SkillLoader()
        body, report = loader.load_body(meta)
        assert "Example Skill" in body
        assert report["chars"] > 0
        assert "---" not in body


# ─── load_resource() ──────────────────────────────────────────────────────────

class TestLoadResource:
    def _make_skill_with_resource(
        self, tmp_path: Path, resource_path: str, content: str
    ) -> SkillMetadata:
        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir(exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: A test skill\n---\n# Body\n",
            encoding="utf-8",
        )
        res_file = skill_dir / resource_path
        res_file.parent.mkdir(parents=True, exist_ok=True)
        res_file.write_text(content, encoding="utf-8")
        return make_metadata(str(skill_dir / "SKILL.md"))

    def test_load_resource_returns_content(self, tmp_path):
        meta = self._make_skill_with_resource(tmp_path, "reference/api.md", "# API\n\nDetails.\n")
        loader = SkillLoader()
        content, report = loader.load_resource(meta, "reference/api.md")
        assert "API" in content
        assert report["resource"] == "reference/api.md"
        assert report["skill_name"] == "test-skill"

    def test_load_resource_stats(self, tmp_path):
        text = "hello world\n"
        meta = self._make_skill_with_resource(tmp_path, "notes.txt", text)
        loader = SkillLoader()
        content, report = loader.load_resource(meta, "notes.txt")
        assert report["chars"] == len(content)
        assert isinstance(report["lines"], int)

    def test_reject_dotdot(self, tmp_path):
        meta = make_skill_dir(tmp_path)
        loader = SkillLoader()
        with pytest.raises(PathTraversalError, match="'\\.\\.'"):
            loader.load_resource(meta, "../other/file.txt")

    def test_reject_dotdot_in_subpath(self, tmp_path):
        meta = make_skill_dir(tmp_path)
        loader = SkillLoader()
        with pytest.raises(PathTraversalError):
            loader.load_resource(meta, "reference/../../etc/passwd")

    def test_reject_absolute_path(self, tmp_path):
        meta = make_skill_dir(tmp_path)
        loader = SkillLoader()
        with pytest.raises(PathTraversalError, match="absolute"):
            loader.load_resource(meta, "/etc/passwd")

    def test_reject_path_outside_skill_dir(self, tmp_path):
        """Symlink-style escape should be caught by realpath check."""
        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: test\n---\n# Body\n",
            encoding="utf-8",
        )
        # Create a symlink pointing outside
        outside = tmp_path / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        (skill_dir / "evil.txt").symlink_to(outside)

        meta = make_metadata(str(skill_dir / "SKILL.md"))
        loader = SkillLoader()
        # The symlink resolves to outside the skill_dir — should be rejected
        with pytest.raises(PathTraversalError):
            loader.load_resource(meta, "evil.txt")


# ─── load_resource() with section_hint ────────────────────────────────────────

class TestLoadResourceSectionHint:
    MULTI_SECTION_MD = textwrap.dedent("""\
        # Introduction
        Intro text here.

        # API Reference
        API details here.
        More API text.

        # Examples
        Example content.
    """)

    def _make_skill_with_md(self, tmp_path: Path) -> SkillMetadata:
        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: test\n---\n# Body\n",
            encoding="utf-8",
        )
        (skill_dir / "guide.md").write_text(self.MULTI_SECTION_MD, encoding="utf-8")
        return make_metadata(str(skill_dir / "SKILL.md"))

    def test_section_hint_extracts_correct_section(self, tmp_path):
        meta = self._make_skill_with_md(tmp_path)
        loader = SkillLoader()
        content, _ = loader.load_resource(meta, "guide.md", section_hint="API Reference")
        assert "API details here." in content
        assert "Intro text here." not in content
        assert "Example content." not in content

    def test_section_hint_not_found_returns_full(self, tmp_path):
        meta = self._make_skill_with_md(tmp_path)
        loader = SkillLoader()
        content, _ = loader.load_resource(meta, "guide.md", section_hint="Nonexistent Section")
        assert "Introduction" in content
        assert "API Reference" in content
        assert "Examples" in content

    def test_no_section_hint_returns_full(self, tmp_path):
        meta = self._make_skill_with_md(tmp_path)
        loader = SkillLoader()
        content, _ = loader.load_resource(meta, "guide.md")
        assert "Introduction" in content
        assert "API Reference" in content
