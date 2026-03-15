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
        _RESET = "\033[0m"
        _BOLD = "\033[1m"
        _risk_color = {"high": "\033[91m", "medium": "\033[93m", "low": "\033[92m"}.get(req.risk, "\033[93m")
        print(
            f"\n{_BOLD}\033[33m[Approval Required]{_RESET}\n"
            f"  Tool:   {_BOLD}{req.tool}{_RESET}\n"
            f"  Risk:   {_risk_color}{_BOLD}{req.risk}{_RESET}\n"
            # f"  Skill:  {req.skill_name}\n"
            # f"  Params: {req.params}"
        )
        answer = input(f"{_BOLD}Allow?{_RESET} [y/N/run (allow for this run)] ").strip().lower()

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
