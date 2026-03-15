"""OrchestratorAgent: planner + dispatcher for complex multi-step tasks.

Responsibilities:
  1. Decompose: one model call → Plan (list of atomic steps)
  2. Dispatch: drive ReactAgent for each step sequentially
  3. Synthesize: one model call → final answer from all step results
"""

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from agent.context import OrchestratorContextBuilder
from agent.events import EventLogger, EventType
from agent.multi_agent import SubTask, TaskResult
from agent.plan import ActionType, Plan, Step, StepStatus
from agent.react_agent import ReactAgent
from model.base import ModelAdapter, ModelResponseError
from model.openai import build_action_response_format
from output.sink import NullSink, OutputSink
from skills.loader import SkillLoader
from skills.registry import SkillRegistry

# Response format schemas for each orchestrator phase
_DECOMPOSE_FORMAT = build_action_response_format(["update_plan"])
_SYNTHESIZE_FORMAT = build_action_response_format(["final_answer"])


# ── State ─────────────────────────────────────────────────────────────────────

class OrchestratorState(BaseModel):
    """Runtime state for one OrchestratorAgent invocation."""
    session_id: str
    user_input: str
    plan: Optional[Plan] = None
    results: list[TaskResult] = Field(default_factory=list)
    status: str = "running"


# ── OrchestratorAgent ─────────────────────────────────────────────────────────

class OrchestratorAgent:
    """
    Plans a complex task and drives ReactAgents to execute each step.

    The model is called exactly twice by the orchestrator itself:
      1. _decompose() → produce Plan
      2. _synthesize() → produce final answer

    All tool use happens inside ReactAgent sub-instances.
    """

    def __init__(
        self,
        model: ModelAdapter,
        registry: SkillRegistry,
        loader: SkillLoader,
        event_logger: EventLogger,
        tools=None,
        sink: OutputSink = None,
        max_turns_per_subtask: int = 10,
        dead_loop_window: int = 4,
        max_context_tokens: int = 80_000,
        run_dir: Optional[Path] = None,
        context_builder: Optional[OrchestratorContextBuilder] = None,
    ):
        self._model = model
        self._registry = registry
        self._loader = loader
        self._logger = event_logger
        self._tools = tools
        self._sink = sink or NullSink()
        self._max_turns_per_subtask = max_turns_per_subtask
        self._dead_loop_window = dead_loop_window
        self._max_context_tokens = max_context_tokens
        self._run_dir = run_dir
        self._ctx_builder = context_builder or OrchestratorContextBuilder()

    def run(
        self,
        user_input: str,
        history_messages: list[dict] = None,
        initial_state: Optional[OrchestratorState] = None,
    ) -> str:
        """
        Full decompose → execute → synthesize flow. Returns final answer string.

        history_messages: cross-task session history (passed to decompose/synthesize context)
        initial_state: restored OrchestratorState for crash recovery (skips already-done steps)
        """
        history_messages = history_messages or []

        if initial_state is not None:
            state = initial_state
            state.user_input = user_input
        else:
            state = OrchestratorState(
                session_id=self._logger.session_id,
                user_input=user_input,
            )

        self._logger.emit(EventType.SESSION_START, {"user_input": user_input, "role": "orchestrator"})

        # ── Phase 1: Decompose ────────────────────────────────────────────────
        if state.plan is None:
            plan = self._decompose(user_input, history_messages)
            state.plan = plan
            self._sink.on_plan_updated(plan)
            self._logger.emit(EventType.PLAN_UPDATED, plan.model_dump())
        else:
            # Resuming: plan already exists, use it
            self._sink.on_plan_updated(state.plan)

        # ── Phase 2: Execute steps sequentially ───────────────────────────────
        while True:
            step = state.plan.current_step()
            if step is None:
                break

            step.status = StepStatus.IN_PROGRESS
            self._sink.on_subtask_start(step.id, step.description)
            self._logger.emit(EventType.SUBTASK_START, {
                "step_id": step.id,
                "description": step.description,
            })

            context = self._ctx_builder.build_subtask_context(
                prior_results=state.results,
                history_messages=history_messages,
            )
            subtask = SubTask(
                step_id=step.id,
                description=step.description,
                context=context,
                goal=user_input,
            )

            react = self._make_react_agent()
            result = react.run(subtask)
            state.results.append(result)

            step.status = StepStatus.DONE if result.success else StepStatus.FAILED
            self._sink.on_subtask_done(step.id, result.success, result.output[:120])
            self._logger.emit(EventType.SUBTASK_DONE, {
                "step_id": step.id,
                "success": result.success,
                "output": result.output[:200],
            })

            if self._run_dir is not None:
                self._save_state(state)

        # ── Phase 3: Synthesize ───────────────────────────────────────────────
        final_answer = self._synthesize(user_input, state.results, history_messages)
        state.status = "completed"

        self._logger.emit(EventType.SESSION_END, {
            "role": "orchestrator",
            "steps": len(state.plan.steps),
            "status": "completed",
        })
        return final_answer

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _decompose(self, user_input: str, history_messages: list[dict]) -> Plan:
        """Call model once to produce a Plan. Falls back to a single-step plan on failure."""
        messages = self._ctx_builder.build_decompose(user_input, history_messages)
        self._sink.on_thinking_start(0, "Planning…")
        try:
            action = self._model.next_action(messages, response_format=_DECOMPOSE_FORMAT)
            if action.type == ActionType.UPDATE_PLAN:
                return Plan.model_validate(action.params["plan"])
        except (ModelResponseError, KeyError, Exception):
            pass

        # Fallback: treat the entire task as one step
        return Plan(
            goal=user_input,
            steps=[Step(id="1", description=user_input, status=StepStatus.PENDING)],
        )

    def _synthesize(
        self,
        user_input: str,
        results: list[TaskResult],
        history_messages: list[dict],
    ) -> str:
        """Call model once to synthesize a final answer from all step results."""
        messages = self._ctx_builder.build_synthesize(user_input, results, history_messages)
        self._sink.on_thinking_start(0, "Synthesizing…")
        try:
            if hasattr(self._model, "next_action_streaming"):
                action = self._model.next_action_streaming(messages, response_format=_SYNTHESIZE_FORMAT)
            else:
                action = self._model.next_action(messages, response_format=_SYNTHESIZE_FORMAT)
            if action.type == ActionType.FINAL_ANSWER:
                content = action.params.get("content", "")
                if not hasattr(self._model, "next_action_streaming"):
                    # Streaming model already emitted chunks via on_text_chunk;
                    # non-streaming model needs a single emit here.
                    self._sink.on_text_chunk(content, done=True)
                self._logger.emit(EventType.FINAL_ANSWER, {"content": content})
                return content
        except (ModelResponseError, Exception):
            pass

        # Fallback: concatenate step outputs
        fallback = "\n\n".join(
            f"Step {r.step_id}: {r.output}" for r in results
        )
        self._sink.on_text_chunk(fallback, done=True)
        return fallback

    def _make_react_agent(self) -> ReactAgent:
        return ReactAgent(
            model=self._model,
            registry=self._registry,
            loader=self._loader,
            event_logger=self._logger,
            tools=self._tools,
            sink=self._sink,
            max_turns=self._max_turns_per_subtask,
            dead_loop_window=self._dead_loop_window,
            max_context_tokens=self._max_context_tokens,
        )

    def _save_state(self, state: OrchestratorState) -> None:
        """
        Persist state for crash recovery.

        Writes two files:
          orchestrator_state.json  — full OrchestratorState (step results, etc.)
          state.json               — AgentState subset for backward-compat with
                                     load_state() / cli/run.py --resume
        """
        if self._run_dir is None:
            return
        self._run_dir.mkdir(parents=True, exist_ok=True)
        try:
            (self._run_dir / "orchestrator_state.json").write_text(
                state.model_dump_json(indent=2)
            )
        except Exception:
            pass

        # Write AgentState-compatible state.json so existing recovery tools work
        try:
            from agent.recovery import save_state
            from agent.state import AgentState
            agent_state = AgentState(
                session_id=state.session_id,
                user_input=state.user_input,
                plan=state.plan,
                status=state.status,
            )
            save_state(self._run_dir, agent_state)
        except Exception:
            pass
