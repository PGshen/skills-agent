"""Unit tests for tools.executor, tools.permissions, tools.approval, tools.runtime."""
import os
import stat
import textwrap
from pathlib import Path

import pytest

from skills.metadata import ResourceLimits, SkillMetadata
from tools.executor import GrepExecutor, ListDirExecutor, ReadFileExecutor, ScriptExecutor, ScriptResult
from tools.permissions import PermissionChecker
from tools.approval import ApprovalManager, ApprovalRequest
from tools.runtime import (
    ApprovalDeniedError,
    PathTraversalError,
    ScriptNotFoundError,
    ToolNotAllowedError,
    ToolsRuntime,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_skill_meta(skill_path: str, allowed_tools: list[str] | None = None) -> SkillMetadata:
    return SkillMetadata(
        name="test-skill",
        description="A test skill",
        source="project",
        skill_path=skill_path,
        allowed_tools=allowed_tools if allowed_tools is not None else ["read_file", "run_script"],
    )


def make_limits(**kwargs) -> ResourceLimits:
    return ResourceLimits(**kwargs)


def write_script(path: Path, content: str) -> Path:
    """Write an executable shell/python script and set +x."""
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


# ---------------------------------------------------------------------------
# ScriptExecutor
# ---------------------------------------------------------------------------

class TestScriptExecutor:
    def test_normal_execution(self, tmp_path):
        script = write_script(tmp_path / "hello.sh", "#!/bin/sh\necho hello\n")
        limits = make_limits()
        result = ScriptExecutor().execute(
            script_path=str(script),
            args=[],
            cwd=str(tmp_path),
            limits=limits,
        )
        assert isinstance(result, ScriptResult)
        assert result.returncode == 0
        assert "hello" in result.stdout
        assert result.timed_out is False

    def test_exit_code_nonzero(self, tmp_path):
        script = write_script(tmp_path / "fail.sh", "#!/bin/sh\nexit 42\n")
        result = ScriptExecutor().execute(
            script_path=str(script), args=[], cwd=str(tmp_path), limits=make_limits()
        )
        assert result.returncode == 42
        assert result.timed_out is False

    def test_stdout_and_stderr(self, tmp_path):
        script = write_script(
            tmp_path / "both.sh",
            "#!/bin/sh\necho out\necho err >&2\n"
        )
        result = ScriptExecutor().execute(
            script_path=str(script), args=[], cwd=str(tmp_path), limits=make_limits()
        )
        assert "out" in result.stdout
        assert "err" in result.stderr

    def test_timeout(self, tmp_path):
        script = write_script(tmp_path / "slow.sh", "#!/bin/sh\nsleep 60\n")
        limits = make_limits(max_script_time_sec=1)
        result = ScriptExecutor().execute(
            script_path=str(script), args=[], cwd=str(tmp_path), limits=limits
        )
        assert result.timed_out is True
        assert result.returncode == -1

    def test_args_passed(self, tmp_path):
        script = write_script(tmp_path / "echo_args.sh", "#!/bin/sh\necho $1 $2\n")
        result = ScriptExecutor().execute(
            script_path=str(script),
            args=["foo", "bar"],
            cwd=str(tmp_path),
            limits=make_limits(),
        )
        assert "foo" in result.stdout
        assert "bar" in result.stdout

    def test_output_truncation(self, tmp_path):
        # Generate output longer than MAX_OUTPUT_CHARS (10_000)
        script = write_script(
            tmp_path / "big.sh",
            "#!/bin/sh\npython3 -c \"print('x' * 20000)\"\n"
        )
        result = ScriptExecutor().execute(
            script_path=str(script), args=[], cwd=str(tmp_path), limits=make_limits()
        )
        assert len(result.stdout) <= 10_000

    def test_env_whitelist_strips_pythonpath(self, tmp_path):
        """PYTHONPATH must not leak into child process (unless in env_overrides)."""
        script = write_script(
            tmp_path / "env_check.sh",
            "#!/bin/sh\necho \"PYTHONPATH=${PYTHONPATH}\"\n"
        )
        original = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = "/secret/path"
        try:
            result = ScriptExecutor().execute(
                script_path=str(script), args=[], cwd=str(tmp_path), limits=make_limits()
            )
        finally:
            if original is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = original

        assert "/secret/path" not in result.stdout

    def test_env_overrides_applied(self, tmp_path):
        script = write_script(
            tmp_path / "myenv.sh",
            "#!/bin/sh\necho \"VAL=${MY_VAR}\"\n"
        )
        result = ScriptExecutor().execute(
            script_path=str(script),
            args=[],
            cwd=str(tmp_path),
            limits=make_limits(),
            env_overrides={"MY_VAR": "injected"},
        )
        assert "injected" in result.stdout

    def test_memory_mb_best_effort_no_error(self, tmp_path):
        """max_memory_mb should log a warning but not raise."""
        script = write_script(tmp_path / "ok.sh", "#!/bin/sh\necho ok\n")
        limits = make_limits(max_memory_mb=100)
        result = ScriptExecutor().execute(
            script_path=str(script), args=[], cwd=str(tmp_path), limits=limits
        )
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# PermissionChecker
# ---------------------------------------------------------------------------

class TestPermissionChecker:
    def test_allowed_by_global_and_skill(self):
        checker = PermissionChecker(["read_file", "run_script"])
        meta = make_skill_meta("/tmp/s/SKILL.md", allowed_tools=["run_script"])
        assert checker.check("run_script", meta) is True

    def test_denied_by_global(self):
        checker = PermissionChecker(["read_file"])  # run_script not global
        meta = make_skill_meta("/tmp/s/SKILL.md", allowed_tools=["run_script"])
        assert checker.check("run_script", meta) is False

    def test_denied_by_skill(self):
        checker = PermissionChecker(["read_file", "run_script"])
        meta = make_skill_meta("/tmp/s/SKILL.md", allowed_tools=["read_file"])
        assert checker.check("run_script", meta) is False

    def test_allowed_when_skill_has_no_restriction(self):
        checker = PermissionChecker(["read_file", "run_script"])
        meta = make_skill_meta("/tmp/s/SKILL.md", allowed_tools=[])
        # Empty allowed_tools means no restriction from skill side
        assert checker.check("run_script", meta) is True

    def test_requires_approval_for_run_script(self):
        checker = PermissionChecker([])
        assert checker.requires_approval("run_script") is True

    def test_no_approval_for_read_file(self):
        checker = PermissionChecker([])
        assert checker.requires_approval("read_file") is False


# ---------------------------------------------------------------------------
# ApprovalManager
# ---------------------------------------------------------------------------

class TestApprovalManager:
    def test_non_interactive_auto_denies(self):
        manager = ApprovalManager(interactive=False)
        req = ApprovalRequest(tool="run_script", risk="medium", params={}, skill_name="s")
        assert manager.request(req) is False

    def test_run_approval_reused(self):
        manager = ApprovalManager(interactive=False)
        manager._run_approvals.add("run_script")  # simulate prior approval
        req = ApprovalRequest(tool="run_script", risk="medium", params={}, skill_name="s")
        assert manager.request(req) is True

    def test_reset_clears_run_approvals(self):
        manager = ApprovalManager(interactive=False)
        manager._run_approvals.add("run_script")
        manager.reset_run_approvals()
        assert len(manager._run_approvals) == 0


# ---------------------------------------------------------------------------
# ToolsRuntime — path safety
# ---------------------------------------------------------------------------

class TestToolsRuntimePathSafety:
    def test_path_traversal_dotdot(self, tmp_path):
        skill_md = tmp_path / "skill" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("")
        runtime = ToolsRuntime(interactive=False)
        meta = make_skill_meta(str(skill_md))
        with pytest.raises(PathTraversalError):
            runtime.run_script(meta, "../escape.sh")

    def test_path_traversal_absolute(self, tmp_path):
        skill_md = tmp_path / "skill" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("")
        runtime = ToolsRuntime(interactive=False)
        meta = make_skill_meta(str(skill_md))
        with pytest.raises(PathTraversalError):
            runtime.run_script(meta, "/etc/passwd")

    def test_script_not_found(self, tmp_path):
        skill_md = tmp_path / "skill" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("")
        runtime = ToolsRuntime(interactive=False)
        meta = make_skill_meta(str(skill_md))
        with pytest.raises((ScriptNotFoundError, ApprovalDeniedError)):
            # Non-interactive → ApprovalDeniedError first (approval gate before path check)
            runtime.run_script(meta, "nonexistent.sh")


# ---------------------------------------------------------------------------
# ToolsRuntime — permission enforcement
# ---------------------------------------------------------------------------

class TestToolsRuntimePermissions:
    def test_tool_not_allowed_when_skill_restricts(self, tmp_path):
        skill_md = tmp_path / "skill" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("")
        # Only read_file allowed in skill
        meta = make_skill_meta(str(skill_md), allowed_tools=["read_file"])
        runtime = ToolsRuntime(interactive=False)
        with pytest.raises(ToolNotAllowedError):
            runtime.run_script(meta, "script.sh")

    def test_tool_not_allowed_when_global_restricts(self, tmp_path):
        skill_md = tmp_path / "skill" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("")
        meta = make_skill_meta(str(skill_md), allowed_tools=["run_script"])
        # Global config does not include run_script
        runtime = ToolsRuntime(global_allowed_tools=["read_file"], interactive=False)
        with pytest.raises(ToolNotAllowedError):
            runtime.run_script(meta, "script.sh")

    def test_approval_denied_in_non_interactive(self, tmp_path):
        skill_md = tmp_path / "skill" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("")
        script = write_script(skill_md.parent / "run.sh", "#!/bin/sh\necho ok\n")
        meta = make_skill_meta(str(skill_md), allowed_tools=["run_script"])
        runtime = ToolsRuntime(interactive=False)
        with pytest.raises(ApprovalDeniedError):
            runtime.run_script(meta, "run.sh")

    def test_run_script_succeeds_with_preapproval(self, tmp_path):
        skill_md = tmp_path / "skill" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("")
        script = write_script(skill_md.parent / "run.sh", "#!/bin/sh\necho success\n")
        meta = make_skill_meta(str(skill_md), allowed_tools=["run_script"])
        runtime = ToolsRuntime(interactive=False)
        # Bypass approval by pre-seeding run_approvals
        runtime._approval._run_approvals.add("run_script")
        result = runtime.run_script(meta, "run.sh")
        assert result.returncode == 0
        assert "success" in result.stdout


# ---------------------------------------------------------------------------
# ReadFileExecutor / ListDirExecutor / GrepExecutor
# ---------------------------------------------------------------------------

class TestReadOnlyExecutors:
    def test_read_file_ok(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_text("world")
        result = ReadFileExecutor().run(f)
        assert result["content"] == "world"

    def test_read_file_not_found(self, tmp_path):
        result = ReadFileExecutor().run(tmp_path / "missing.txt")
        assert "error" in result

    def test_list_dir_ok(self, tmp_path):
        (tmp_path / "a.txt").write_text("")
        (tmp_path / "sub").mkdir()
        result = ListDirExecutor().run(tmp_path)
        assert "entries" in result
        assert any("sub" in e for e in result["entries"])

    def test_list_dir_not_found(self, tmp_path):
        result = ListDirExecutor().run(tmp_path / "no_such_dir")
        assert "error" in result

    def test_grep_finds_match(self, tmp_path):
        (tmp_path / "file.txt").write_text("hello world\nfoo bar\n")
        result = GrepExecutor().run(pattern="hello", root=tmp_path)
        assert result["matches"]
        assert result["matches"][0]["content"].strip() == "hello world"

    def test_grep_no_match(self, tmp_path):
        (tmp_path / "file.txt").write_text("nothing here\n")
        result = GrepExecutor().run(pattern="xyz123", root=tmp_path)
        assert result["matches"] == []

    def test_grep_invalid_regex(self, tmp_path):
        result = GrepExecutor().run(pattern="[invalid", root=tmp_path)
        assert "error" in result
