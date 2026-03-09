"""Approval mechanism: interactive and non-interactive."""
from dataclasses import dataclass
from enum import Enum


class ApprovalScope(str, Enum):
    ONCE = "once"    # Approved for this single call only
    RUN = "run"      # Approved for all calls to this tool in the current run
    ALWAYS = "always"  # Never ask again (global, not persisted)


@dataclass
class ApprovalRequest:
    tool: str
    risk: str          # "low" | "medium" | "high"
    params: dict
    skill_name: str


class ApprovalManager:
    """
    Manages user approval for medium/high-risk tools.

    Interactive mode (default):
        Prints a prompt and reads stdin.  Accepts:
          y      — approve this call once
          run    — approve all calls to this tool for the current run
          n / "" — deny

    Non-interactive mode (CI):
        All approval requests are automatically denied.
    """

    def __init__(self, interactive: bool = True):
        self._interactive = interactive
        # Tools approved for the lifetime of the current run
        self._run_approvals: set[str] = set()

    def request(self, req: ApprovalRequest) -> bool:
        """Return True if approved, False if denied."""
        # Already approved at run scope
        if req.tool in self._run_approvals:
            return True

        # CI / non-interactive: auto-deny
        if not self._interactive:
            return False

        # Prompt the user
        print(
            f"\n[Approval Required]\n"
            f"  Tool:   {req.tool}\n"
            f"  Risk:   {req.risk}\n"
            f"  Skill:  {req.skill_name}\n"
            f"  Params: {req.params}"
        )
        answer = input("Allow? [y/N/run (allow for this run)] ").strip().lower()

        if answer == "y":
            return True
        elif answer == "run":
            self._run_approvals.add(req.tool)
            return True
        else:
            return False

    def reset_run_approvals(self) -> None:
        """Clear run-scoped approvals (call between independent runs)."""
        self._run_approvals.clear()
