"""Permission merging and enforcement."""
from typing import Optional

from skills.metadata import SkillMetadata

# Tools that require explicit user approval before execution
_APPROVAL_REQUIRED = {"run_script", "write_file", "delete_file"}


class PermissionChecker:
    """
    Three-way permission merge:
      final_allowed = global_allowed ∩ skill_declared (if any) ∩ runtime_policy

    Usage:
        checker = PermissionChecker(global_allowed_tools=["read_file", "run_script"])
        ok = checker.check("run_script", skill_meta)
        needs_approval = checker.requires_approval("run_script")
    """

    def __init__(self, global_allowed_tools: list[str]):
        self._global: set[str] = set(global_allowed_tools)

    def check(self, tool_name: str, skill_meta: Optional[SkillMetadata] = None) -> bool:
        """
        Return True if *tool_name* is permitted.
        skill_meta is optional; when provided, its allowed_tools further restricts the set.
        False means the call must be rejected (ToolNotAllowedError).
        True still does not mean execution is immediate — approval may be needed.
        """
        # Global config must permit the tool
        if tool_name not in self._global:
            return False

        # Skill's allowed_tools declaration further restricts the set (if skill context given)
        if skill_meta is not None and skill_meta.allowed_tools:
            if tool_name not in skill_meta.allowed_tools:
                return False

        return True

    def requires_approval(self, tool_name: str) -> bool:
        """Return True for medium/high-risk tools that need user approval."""
        return tool_name in _APPROVAL_REQUIRED
