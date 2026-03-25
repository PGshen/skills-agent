"""ToolsRuntime: main tools runtime."""
from pathlib import Path
from typing import Optional

from skills.metadata import SkillMetadata
from tools.approval import ApprovalManager, ApprovalRequest
from tools.executor import (
    DeleteFileExecutor,
    GrepExecutor,
    ListDirExecutor,
    ReadFileExecutor,
    ScriptExecutor,
    ScriptResult,
    ShellCommandExecutor,
    ShellCommandResult,
    WebSearchAdapter,
    WebSearchExecutor,
    WriteFileExecutor,
    classify_shell_risk,
)
from tools.permissions import PermissionChecker


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------

class ToolsRuntimeError(Exception):
    """Base class for all ToolsRuntime errors."""


class ToolNotAllowedError(ToolsRuntimeError):
    """Tool is not in the allowed set (global config or skill declaration)."""


class ApprovalDeniedError(ToolsRuntimeError):
    """User (or CI policy) denied the approval request."""


class PathTraversalError(ToolsRuntimeError):
    """Script path escapes the skill directory."""


class ScriptNotFoundError(ToolsRuntimeError):
    """The requested script does not exist inside the skill directory."""


# ---------------------------------------------------------------------------
# ToolsRuntime
# ---------------------------------------------------------------------------

class ToolsRuntime:
    """
    Orchestrates all tool execution for AgentCore.

    Responsibilities:
    - Permission check (three-way merge via PermissionChecker)
    - Approval gate for medium/high-risk tools
    - Path-safety validation for run_script
    - Delegate execution to the appropriate executor
    """

    def __init__(
        self,
        global_allowed_tools: list[str] | None = None,
        interactive: bool = True,
        web_search_adapter: Optional[WebSearchAdapter] = None,
    ):
        self._permission = PermissionChecker(
            global_allowed_tools or [
                "read_file", "list_dir", "grep", "run_script",
                "write_file", "delete_file", "web_search", "run_shell",
            ]
        )
        self._approval = ApprovalManager(interactive=interactive)
        self._script_executor = ScriptExecutor()
        self._web_search: Optional[WebSearchExecutor] = (
            WebSearchExecutor(web_search_adapter) if web_search_adapter else None
        )

    def available_tools(self) -> list[str]:
        """
        Return the list of tools that are both permitted and actually configured.
        web_search is excluded when no adapter is provided.
        """
        tools = list(self._permission._global)
        if "web_search" in tools and self._web_search is None:
            tools.remove("web_search")
        return sorted(tools)

    # ------------------------------------------------------------------
    # run_script
    # ------------------------------------------------------------------

    def run_script(
        self,
        skill_meta: SkillMetadata,
        script: str,
        args: list[str] | None = None,
        env_overrides: dict | None = None,
    ) -> ScriptResult:
        """
        Execute a skill script with full permission + approval + path checks.

        Returns ScriptResult on success.
        Raises ToolNotAllowedError, ApprovalDeniedError, PathTraversalError,
               or ScriptNotFoundError on failure.
        """
        # 1. Permission check
        if not self._permission.check("run_script", skill_meta):
            raise ToolNotAllowedError(
                f"run_script is not allowed for skill '{skill_meta.name}'"
            )

        # 2. Path safety (fail fast before prompting the user)
        skill_dir = Path(skill_meta.skill_path).parent
        script_path = self._resolve_script_path(skill_dir, script)

        # 3. Approval gate
        if self._permission.requires_approval("run_script"):
            req = ApprovalRequest(
                tool="run_script",
                risk="medium",
                params={"script": script, "args": args or []},
                skill_name=skill_meta.name,
            )
            if not self._approval.request(req):
                raise ApprovalDeniedError("User denied script execution approval")

        # 4. Execute
        return self._script_executor.execute(
            script_path=str(script_path),
            args=args or [],
            cwd=str(skill_dir),
            limits=skill_meta.resource_limits,
            env_overrides=env_overrides,
        )

    # ------------------------------------------------------------------
    # Read-only tools (no permission/approval checks needed)
    # ------------------------------------------------------------------

    def read_file(self, path: str, max_bytes: int = 100_000) -> dict:
        """Read a file. Low-risk, no approval required."""
        if not path:
            return {"error": "read_file requires a non-empty 'path' parameter"}
        return ReadFileExecutor().run(Path(path), max_bytes=max_bytes)

    def list_dir(self, path: str, max_entries: int = 100) -> dict:
        """List directory contents. Low-risk, no approval required."""
        if not path:
            return {"error": "list_dir requires a non-empty 'path' parameter"}
        return ListDirExecutor().run(Path(path), max_entries=max_entries)

    def grep(self, pattern: str, path: str, max_results: int = 50) -> dict:
        """Search for a regex pattern. Low-risk, no approval required."""
        if not path:
            return {"error": "grep requires a non-empty 'path' parameter"}
        return GrepExecutor().run(
            pattern=pattern, root=Path(path), max_results=max_results
        )

    def web_search(self, query: str, max_results: int = 5) -> dict:
        """
        Search the web. Low-risk, no approval required.
        Returns {"results": [...]} or {"error": "..."}.
        Requires a WebSearchAdapter to be configured at init time.
        """
        if self._web_search is None:
            return {"error": "web_search not configured (no adapter provided)"}
        result = self._web_search.run(query, max_results=max_results)
        if result.error:
            return {"error": result.error}
        return {
            "results": [
                {"title": r.title, "url": r.url, "content": r.content}
                for r in result.results
            ]
        }

    # ------------------------------------------------------------------
    # Write tools (high-risk, require permission + approval)
    # ------------------------------------------------------------------

    def write_file(
        self,
        path: str,
        content: str | None = None,
        old_str: str | None = None,
        new_str: str | None = None,
    ) -> dict:
        """
        Write or patch a file. High-risk, requires approval.

        Overwrite mode:  pass ``content`` (full file text).
        Patch mode:      pass ``old_str`` + ``new_str`` (unique substring replacement).

        Returns {"success": True} or {"error": "..."}.
        """
        if not self._permission.check("write_file"):
            raise ToolNotAllowedError("write_file is not in the allowed tool set")

        if content is not None:
            approval_params: dict = {"path": path, "mode": "overwrite", "preview": content[:200]}
        else:
            approval_params = {
                "path": path,
                "mode": "patch",
                "old_str": (old_str or "")[:200],
                "new_str": (new_str or "")[:200],
            }

        req = ApprovalRequest(
            tool="write_file",
            risk="high",
            params=approval_params,
            skill_name="",
        )
        if not self._approval.request(req):
            raise ApprovalDeniedError("User denied write_file approval")

        result = WriteFileExecutor().run(Path(path), content, old_str=old_str, new_str=new_str)
        if not result.success:
            return {"error": result.error}
        return {"success": True}

    def run_shell(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int = 30,
        env_overrides: dict | None = None,
    ) -> dict:
        """
        Execute an arbitrary shell command.

        Risk is classified automatically:
          - ``high``   (rm, sudo, kill, …) — always requires explicit approval.
          - ``medium`` (cp, git, pip, …)   — requires approval.
          - ``low``    (ls, echo, cat, …)  — executes without prompting.

        Returns ``{"returncode": int, "stdout": str, "stderr": str}``
        or ``{"error": "..."}`` on permission / approval failure.
        """
        if not self._permission.check("run_shell"):
            raise ToolNotAllowedError("run_shell is not in the allowed tool set")

        risk = classify_shell_risk(command)

        if risk in ("high", "medium"):
            req = ApprovalRequest(
                tool="run_shell",
                risk=risk,
                params={"command": command, "cwd": cwd or ""},
                skill_name="",
            )
            if not self._approval.request(req):
                raise ApprovalDeniedError(
                    f"User denied run_shell approval for command: {command!r}"
                )

        result: ShellCommandResult = ShellCommandExecutor().execute(
            command=command,
            cwd=cwd,
            timeout=timeout,
            env_overrides=env_overrides,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
        }

    def delete_file(self, path: str) -> dict:
        """
        Delete a file. High-risk, requires approval.
        Returns {"success": True} or {"error": "..."}.
        """
        if not self._permission.check("delete_file"):
            raise ToolNotAllowedError("delete_file is not in the allowed tool set")

        req = ApprovalRequest(
            tool="delete_file",
            risk="high",
            params={"path": path},
            skill_name="",
        )
        if not self._approval.request(req):
            raise ApprovalDeniedError("User denied delete_file approval")

        result = DeleteFileExecutor().run(Path(path))
        if not result.success:
            return {"error": result.error}
        return {"success": True}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_script_path(self, skill_dir: Path, script: str) -> Path:
        """
        Validate that *script* is a relative path inside *skill_dir*.

        Raises PathTraversalError or ScriptNotFoundError.
        """
        if ".." in script or script.startswith("/"):
            raise PathTraversalError(f"Invalid script path: {script!r}")

        candidate = skill_dir / script
        resolved = candidate.resolve()
        resolved_root = skill_dir.resolve()

        if not str(resolved).startswith(str(resolved_root)):
            raise PathTraversalError(
                f"Script path escapes skill directory: {script!r}"
            )

        if not resolved.exists():
            raise ScriptNotFoundError(f"Script not found: {script}")

        return resolved
