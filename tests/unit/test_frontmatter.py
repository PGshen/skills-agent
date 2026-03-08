"""Unit tests for SKILL.md frontmatter parser."""
import pytest
from skills.frontmatter import (
    FrontmatterParseError,
    ParsedSkillFile,
    ResourceLimitsConfig,
    SkillFrontmatter,
    parse_skill_file,
)

# ─── Test fixtures ────────────────────────────────────────────────────────────

VALID_SKILL = """---
name: example-skill
description: An example skill for testing
version: "1.0"
allowed-tools:
  - read_file
  - run_script
---

# Example Skill Body
This is the body.
"""

FULL_SKILL = """---
name: full-skill
description: A skill with all supported fields
version: "2.0"
author: test-author
disable-model-invocation: false
user-invocable: true
allowed-tools:
  - read_file
  - grep
requires:
  - base-utils
load-priority: high
resource-limits:
  max-script-time-sec: 60
  max-concurrent-scripts: 3
  allow-network: false
run-mode: inline
---

# Full Skill Body
"""

MINIMAL_SKILL = """---
name: minimal-skill
description: Minimal required fields only
---

Body here.
"""


# ─── SkillFrontmatter Pydantic model ─────────────────────────────────────────

class TestSkillFrontmatter:
    def test_required_fields_only(self):
        fm = SkillFrontmatter(name="x", description="y")
        assert fm.name == "x"
        assert fm.description == "y"

    def test_defaults(self):
        fm = SkillFrontmatter(name="x", description="y")
        assert fm.version == "1.0"
        assert fm.author is None
        assert fm.disable_model_invocation is False
        assert fm.user_invocable is True
        assert fm.allowed_tools == []
        assert fm.requires == []
        assert fm.load_priority == "normal"
        assert fm.run_mode == "inline"
        assert isinstance(fm.resource_limits, ResourceLimitsConfig)

    def test_alias_access(self):
        fm = SkillFrontmatter.model_validate({
            "name": "x",
            "description": "y",
            "allowed-tools": ["grep"],
            "disable-model-invocation": True,
        })
        assert fm.allowed_tools == ["grep"]
        assert fm.disable_model_invocation is True

    def test_version_float_coercion(self):
        # YAML parses `version: 1.0` as float; should be coerced to str
        fm = SkillFrontmatter.model_validate({"name": "x", "description": "y", "version": 1.0})
        assert fm.version == "1.0"

    def test_version_int_coercion(self):
        fm = SkillFrontmatter.model_validate({"name": "x", "description": "y", "version": 2})
        assert fm.version == "2"

    def test_extra_fields_forbidden(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SkillFrontmatter.model_validate({"name": "x", "description": "y", "unknown-field": "v"})

    def test_resource_limits_nested(self):
        fm = SkillFrontmatter.model_validate({
            "name": "x",
            "description": "y",
            "resource-limits": {"max-script-time-sec": 60, "allow-network": True},
        })
        assert fm.resource_limits.max_script_time_sec == 60
        assert fm.resource_limits.allow_network is True

    def test_resource_limits_defaults(self):
        fm = SkillFrontmatter(name="x", description="y")
        rl = fm.resource_limits
        assert rl.max_script_time_sec == 30
        assert rl.max_concurrent_scripts == 2
        assert rl.max_memory_mb is None
        assert rl.allow_network is False


# ─── ParsedSkillFile dataclass ────────────────────────────────────────────────

class TestParsedSkillFile:
    def test_is_dataclass(self):
        fm = SkillFrontmatter(name="x", description="y")
        result = ParsedSkillFile(frontmatter=fm, body="body")
        assert result.frontmatter is fm
        assert result.body == "body"

    def test_frontmatter_type(self):
        result = parse_skill_file(VALID_SKILL)
        assert isinstance(result.frontmatter, SkillFrontmatter)


# ─── Happy path parsing ───────────────────────────────────────────────────────

class TestParseValid:
    def test_basic_fields(self):
        result = parse_skill_file(VALID_SKILL)
        assert result.frontmatter.name == "example-skill"
        assert result.frontmatter.description == "An example skill for testing"
        assert result.frontmatter.version == "1.0"

    def test_allowed_tools_list(self):
        result = parse_skill_file(VALID_SKILL)
        assert result.frontmatter.allowed_tools == ["read_file", "run_script"]

    def test_body_content(self):
        result = parse_skill_file(VALID_SKILL)
        assert "Example Skill Body" in result.body
        assert "This is the body." in result.body

    def test_body_stripped(self):
        result = parse_skill_file(VALID_SKILL)
        assert not result.body.startswith("\n")

    def test_returns_parsed_skill_file_instance(self):
        result = parse_skill_file(VALID_SKILL)
        assert isinstance(result, ParsedSkillFile)

    def test_minimal_required_fields(self):
        result = parse_skill_file(MINIMAL_SKILL)
        assert result.frontmatter.name == "minimal-skill"
        assert result.frontmatter.description == "Minimal required fields only"
        assert result.body == "Body here."

    def test_minimal_has_defaults(self):
        result = parse_skill_file(MINIMAL_SKILL)
        fm = result.frontmatter
        assert fm.allowed_tools == []
        assert fm.requires == []
        assert fm.disable_model_invocation is False
        assert fm.user_invocable is True

    def test_all_allowed_fields(self):
        result = parse_skill_file(FULL_SKILL)
        fm = result.frontmatter
        assert fm.name == "full-skill"
        assert fm.author == "test-author"
        assert fm.disable_model_invocation is False
        assert fm.user_invocable is True
        assert fm.requires == ["base-utils"]
        assert fm.load_priority == "high"
        assert fm.resource_limits.max_script_time_sec == 60
        assert fm.run_mode == "inline"

    def test_empty_body(self):
        content = "---\nname: x\ndescription: y\n---\n"
        result = parse_skill_file(content)
        assert result.body == ""

    def test_multiline_body(self):
        content = "---\nname: x\ndescription: y\n---\nLine1\nLine2\nLine3"
        result = parse_skill_file(content)
        assert "Line1" in result.body
        assert "Line2" in result.body
        assert "Line3" in result.body

    def test_fixture_example_skill(self):
        from pathlib import Path
        fixture = Path("tests/fixtures/skills/example-skill/SKILL.md").read_text()
        result = parse_skill_file(fixture)
        assert result.frontmatter.name == "example-skill"
        assert result.frontmatter.allowed_tools == ["read_file", "grep"]

    def test_fixture_advanced_skill(self):
        from pathlib import Path
        fixture = Path("tests/fixtures/skills/advanced-skill/SKILL.md").read_text()
        result = parse_skill_file(fixture)
        assert result.frontmatter.name == "advanced-skill"


# ─── Security: angle bracket rejection ───────────────────────────────────────

class TestAngleBracketRejection:
    def test_no_angle_bracket_passes(self):
        content = "---\nname: test\ndescription: has angle bracket\n---\n"
        result = parse_skill_file(content)
        assert result.frontmatter.name == "test"

    def test_rejects_lt_in_frontmatter(self):
        with pytest.raises(FrontmatterParseError, match="'<'"):
            parse_skill_file("---\nname: test\ndescription: bad tag\n---\n".replace("bad tag", "has <b> tag"))

    def test_rejects_lt_in_description_value(self):
        with pytest.raises(FrontmatterParseError):
            parse_skill_file("---\nname: test\ndescription: bad\n---\n".replace(
                "bad", "has <script> tag"
            ))

    def test_rejects_lt_anywhere_in_content(self):
        content = "---\nname: x\ndescription: y\n---\n# Title\nsome text <here>"
        with pytest.raises(FrontmatterParseError):
            parse_skill_file(content)

    def test_rejects_yaml_tag_injection_attempt(self):
        with pytest.raises(FrontmatterParseError):
            parse_skill_file("---\nname: !!python/object:os.system\ndescription: y\n---\n")

    def test_gt_alone_is_allowed(self):
        content = "---\nname: x\ndescription: y\n---\nsome > content"
        result = parse_skill_file(content)
        assert ">" in result.body


# ─── Structural errors ────────────────────────────────────────────────────────

class TestStructuralErrors:
    def test_missing_opening_separator(self):
        with pytest.raises(FrontmatterParseError, match="must start with '---'"):
            parse_skill_file("name: test\ndescription: ok\n---\n")

    def test_missing_closing_separator(self):
        with pytest.raises(FrontmatterParseError, match="closing '---' not found"):
            parse_skill_file("---\nname: test\ndescription: ok\n")

    def test_empty_content(self):
        with pytest.raises(FrontmatterParseError):
            parse_skill_file("")

    def test_only_opening_separator(self):
        with pytest.raises(FrontmatterParseError):
            parse_skill_file("---\n")

    def test_frontmatter_not_mapping(self):
        with pytest.raises(FrontmatterParseError, match="must be a YAML mapping"):
            parse_skill_file("---\njust a string\n---\n")

    def test_frontmatter_list_not_mapping(self):
        with pytest.raises(FrontmatterParseError, match="must be a YAML mapping"):
            parse_skill_file("---\n- item1\n- item2\n---\n")

    def test_invalid_yaml_syntax(self):
        with pytest.raises(FrontmatterParseError, match="YAML parse error"):
            parse_skill_file("---\nname: [\nbad yaml\n---\n")


# ─── Field whitelist validation (via Pydantic extra="forbid") ─────────────────

class TestFieldWhitelist:
    def test_rejects_unknown_field(self):
        content = "---\nname: test\ndescription: ok\nunknown-field: bad\n---\n"
        with pytest.raises(FrontmatterParseError, match="validation error"):
            parse_skill_file(content)

    def test_rejects_multiple_unknown_fields(self):
        content = "---\nname: test\ndescription: ok\nfoo: 1\nbar: 2\n---\n"
        with pytest.raises(FrontmatterParseError, match="validation error"):
            parse_skill_file(content)

    def test_frontmatter_preserves_values_exactly(self):
        content = "---\nname: my-skill\ndescription: does stuff\nversion: '3.0'\n---\n"
        result = parse_skill_file(content)
        assert result.frontmatter.version == "3.0"

    def test_extra_separator_lines_in_body_ok(self):
        content = "---\nname: x\ndescription: y\n---\nBody text\n---\nMore body\n"
        result = parse_skill_file(content)
        assert "---" in result.body


# ─── Security: yaml.load must not be used ─────────────────────────────────────

class TestNoYamlLoad:
    def test_yaml_load_not_in_source(self):
        import inspect
        import skills.frontmatter as fm_module
        source = inspect.getsource(fm_module)
        assert "yaml.load(" not in source
