"""AgentCore: EntryAgent — classifies task complexity and routes to the right agent."""

from pathlib import Path
from typing import Optional

from agent.context import (
    ClassifyContextBuilder,
    OrchestratorContextBuilder,
    _plan_summary as _format_plan_summary,  # noqa: F401 — re-export for tests
)
from agent.events import EventLogger, EventType
from agent.multi_agent import SubTask, TaskComplexity
from agent.orchestrator import OrchestratorAgent, OrchestratorState
from agent.plan import ActionType
from agent.react_agent import ReactAgent
from agent.state import AgentState
from model.base import ModelAdapter, ModelResponseError
from output.sink import NullSink, OutputSink
from skills.loader import SkillLoader
from skills.registry import SkillRegistry


class AgentCore:
    """
    Entry agent: classifies task complexity, then either answers directly (simple)
    or delegates to OrchestratorAgent (complex).

    Public interface is identical to the old AgentCore so CLI code needs no changes.
    """

    def __init__(
        self,
        model: ModelAdapter,
        registry: SkillRegistry,
        loader: SkillLoader,
        event_logger: EventLogger,
        tools=None,
        sink: OutputSink = None,
        max_turns: int = 20,
        dead_loop_window: int = 6,
        dead_loop_stall_turns: int = 4,   # kept for API compatibility, unused
        max_context_tokens: int = 100_000,
        run_dir: Optional[Path] = None,
    ):
        self._model = model
        self._registry = registry
        self._loader = loader
        self._logger = event_logger
        self._tools = tools
        self._sink = sink or NullSink()
        self._max_turns = max_turns
        self._dead_loop_window = dead_loop_window
        self._max_context_tokens = max_context_tokens
        self._run_dir = run_dir

        self._orchestrator = OrchestratorAgent(
            model=model,
            registry=registry,
            loader=loader,
            event_logger=event_logger,
            tools=tools,
            sink=self._sink,
            max_turns_per_subtask=max(1, max_turns // 2),
            dead_loop_window=dead_loop_window,
            max_context_tokens=max_context_tokens,
            run_dir=run_dir,
        )

        self._react_agent = ReactAgent(
            model=model,
            registry=registry,
            loader=loader,
            event_logger=event_logger,
            tools=tools,
            sink=self._sink,
            max_turns=5,
            dead_loop_window=dead_loop_window,
            max_context_tokens=max_context_tokens,
        )

    def run(
        self,
        user_input: str,
        history_messages: list[dict] = None,
        initial_state: Optional[AgentState] = None,
    ) -> str:
        """
        Classify the request, then route:
          simple  → one model call for a direct answer
          complex → OrchestratorAgent (plan → execute → synthesize)

        history_messages: cross-task session history (chat mode)
        initial_state: restored AgentState for crash recovery (B4.1 compatibility)
        """
        history_messages = history_messages or []

        # Restore OrchestratorState from legacy AgentState if provided
        orch_state = self._restore_orchestrator_state(initial_state)

        # Classify
        complexity = self._classify(user_input)
        self._sink.on_route_decision(complexity.value)
        self._logger.emit(EventType.ROUTE_DECISION, {
            "complexity": complexity.value,
            "user_input": user_input,
        })

        if complexity == TaskComplexity.SIMPLE:
            return self._run_simple(user_input, history_messages)
        else:
            return self._orchestrator.run(
                user_input,
                history_messages=history_messages,
                initial_state=orch_state,
            )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _classify(self, user_input: str) -> TaskComplexity:
        """One model call to classify task complexity. Defaults to COMPLEX on any failure."""
        messages = ClassifyContextBuilder().build(user_input)
        self._sink.on_thinking_start(0, "Classifying…")
        try:
            action = self._model.next_action(messages)
            if action.type == ActionType.FINAL_ANSWER:
                content = action.params.get("content", "").strip().lower()
                if "simple" in content:
                    return TaskComplexity.SIMPLE
        except (ModelResponseError, Exception):
            pass
        return TaskComplexity.COMPLEX  # conservative default

    def _run_simple(self, user_input: str, history_messages: list[dict]) -> str:
        """Run a simple task through the ReactAgent (single-step, no planning)."""
        context = OrchestratorContextBuilder().build_subtask_context(
            prior_results=[],
            history_messages=history_messages,
        )
        task = SubTask(
            step_id="simple-0",
            description=user_input,
            context=context,
            goal=user_input,
        )
        result = self._react_agent.run(task)
        return result.output

    def _restore_orchestrator_state(
        self, initial_state: Optional[AgentState]
    ) -> Optional[OrchestratorState]:
        """
        Adapt legacy AgentState (crash recovery) to OrchestratorState.
        Only plan is carried over; results start empty (steps will re-run from last done).
        """
        if initial_state is None:
            return None
        return OrchestratorState(
            session_id=initial_state.session_id,
            user_input=initial_state.user_input,
            plan=initial_state.plan,
        )
