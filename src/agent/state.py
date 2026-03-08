"""AgentState: runtime agent state."""

from typing import Optional

from pydantic import BaseModel, Field

from .plan import Plan
from skills.metadata import SkillMetadata


class AgentState(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    session_id: str
    user_input: str
    status: str = "running"
    plan: Optional[Plan] = None
    active_skills: list[SkillMetadata] = Field(default_factory=list)
    turn_count: int = 0
    last_plan_progress_turn: int = 0
    recent_action_hashes: list[str] = Field(default_factory=list)
    dead_loop_triggered: bool = False

    def is_done(self) -> bool:
        if self.dead_loop_triggered:
            return True
        if self.status == "completed":
            return True
        if self.plan is None:
            return False
        return all(s.status in ("done", "failed") for s in self.plan.steps)
