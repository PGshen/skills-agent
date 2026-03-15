"""Shared data structures for multi-agent communication."""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TaskComplexity(str, Enum):
    SIMPLE = "simple"    # Can be answered directly with general knowledge — no tools needed
    MEDIUM = "medium"    # Requires tool use but is a single focused task → ReactAgent
    COMPLEX = "complex"  # Requires planning, file writing, or multiple distinct steps → Orchestrator


class SubTask(BaseModel):
    """A single atomic task dispatched from OrchestratorAgent to ReactAgent."""
    step_id: str
    description: str          # Concrete objective for this step
    context: str = ""         # Summary of results from completed prior steps
    goal: str = ""            # Original user goal (for background understanding)


class TaskResult(BaseModel):
    """Result returned by ReactAgent after completing (or failing) a SubTask."""
    step_id: str
    success: bool
    output: str               # Natural-language result summary for the Orchestrator
    artifacts: list[str] = Field(default_factory=list)  # File paths created/modified
