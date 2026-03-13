"""AgentCore: ReAct main loop."""

import hashlib
import json
from pathlib import Path
from typing import Optional

from agent.context import ContextBuilder, _plan_summary as _format_plan_summary
from agent.events import EventLogger, EventType
from agent.plan import Action, ActionType, Plan
from agent.recovery import save_state
from agent.state import AgentState
from model.base import ModelAdapter, ModelResponseError
from output.sink import NullSink, OutputSink
from skills.loader import PathTraversalError, SkillLoader
from skills.metadata import SkillMetadata
from skills.registry import SkillRegistry
from tools.runtime import (
    ApprovalDeniedError,
    ToolNotAllowedError,
    ToolsRuntimeError,
)


class AgentCore:
    def __init__(
        self,
        model: ModelAdapter,
        registry: SkillRegistry,
        loader: SkillLoader,
        event_logger: EventLogger,
        tools=None,                      # Phase B: ToolsRuntime
        sink: OutputSink = None,
        max_turns: int = 20,
        dead_loop_window: int = 6,       # action hash detection window size
        dead_loop_stall_turns: int = 4,  # Phase B: plan stall warning threshold
        max_context_tokens: int = 100_000,
        run_dir: Optional[Path] = None,  # B4.1: persistence directory
    ):
        self._model = model
        self._registry = registry
        self._loader = loader
        self._logger = event_logger
        self._tools = tools
        self._sink = sink or NullSink()
        self._max_turns = max_turns
        self._dead_loop_window = dead_loop_window
        self._dead_loop_stall_turns = dead_loop_stall_turns
        self._context_builder = ContextBuilder(max_context_tokens=max_context_tokens)
        self._run_dir = run_dir

    def run(
        self,
        user_input: str,
        history_messages: list[dict] = None,
        initial_state: Optional[AgentState] = None,
    ) -> str:
        """
        Execute full ReAct loop, return final answer string.

        history_messages: built by SessionContext.build_history_messages() in chat mode,
            or by build_resume_context() for crash recovery (B4.1).
        initial_state: restored AgentState for crash recovery; if None, a fresh state is created.
        Single-shot mode (run command): omit both or pass None (treated as []).
        """
        if history_messages is None:
            history_messages = []

        # Initialization
        skill_metas = self._registry.scan()
        if initial_state is not None:
            state = initial_state
            state.user_input = user_input
        else:
            state = AgentState(
                session_id=self._logger.session_id,
                user_input=user_input,
            )
        self._logger.emit(EventType.SESSION_START, {
            "user_input": user_input,
            "skill_count": len(skill_metas),
        })

        react_history: list[tuple[Action, str]] = []
        final_answer = ""

        # Turn loop
        while not state.is_done() and state.turn_count < self._max_turns:
            state.turn_count += 1

            messages = self._build_context(state, history_messages, react_history)
            self._sink.on_thinking_start(state.turn_count)
            try:
                if hasattr(self._model, "next_action_streaming"):
                    action = self._model.next_action_streaming(messages)
                    streaming = True
                else:
                    action = self._model.next_action(messages)
                    streaming = False
            except ModelResponseError as exc:
                self._sink.on_error(f"Model response parse error: {exc}", recoverable=False)
                self._logger.emit(EventType.ACTION_FAILED, {
                    "turn": state.turn_count,
                    "reason": "parse_error",
                    "detail": str(exc),
                })
                break

            self._logger.emit(EventType.ACTION_REQUESTED, {
                "turn": state.turn_count,
                "action_type": action.type,
                "params": action.params,
            })

            # Dead loop detection (primary mechanism: action hash)
            if self._check_dead_loop_hash(action, state):
                state.dead_loop_triggered = True
                self._sink.on_error("Dead loop detected: repeated action", recoverable=False)
                self._logger.emit(EventType.DEAD_LOOP_DETECTED, {"reason": "repeated_action"})
                break

            # Track plan progress before/after action (for Phase B stall detection)
            old_statuses = {s.id: s.status for s in state.plan.steps} if state.plan else {}

            observation = self._execute_action(action, state, streaming=streaming)

            new_statuses = {s.id: s.status for s in state.plan.steps} if state.plan else {}
            if old_statuses != new_statuses:
                state.last_plan_progress_turn = state.turn_count

            self._logger.emit(EventType.ACTION_COMPLETED, {
                "turn": state.turn_count,
                "action_type": action.type,
                "observation": observation[:200],
            })

            react_history.append((action, observation))

            if action.type == ActionType.FINAL_ANSWER:
                final_answer = observation
                break

        # Determine final status and notify
        if state.dead_loop_triggered:
            final_status = "dead_loop"
        elif state.status == "completed":
            final_status = "completed"
        else:
            # max_turns exhausted without completion
            self._sink.on_error("Budget exhausted: max turns reached", recoverable=False)
            final_status = "max_turns"

        self._logger.emit(EventType.SESSION_END, {
            "turns": state.turn_count,
            "status": final_status,
        })
        self._sink.on_session_end(state.turn_count, final_status)

        return final_answer

    def _build_context(
        self,
        state: AgentState,
        history_messages: list[dict],
        react_history: list[tuple[Action, str]],
    ) -> list[dict]:
        """Assemble the messages list for the current turn."""
        return self._context_builder.build(
            state=state,
            registry=self._registry,
            react_history=react_history,
            history_messages=history_messages,
        )

    def _execute_action(self, action: Action, state: AgentState, streaming: bool = False) -> str:
        """Execute a single Action, return observation string for next context."""

        if action.type == ActionType.UPDATE_PLAN:
            new_plan = Plan.model_validate(action.params["plan"])
            state.plan = state.plan.replace(new_plan) if state.plan else new_plan
            self._sink.on_plan_updated(state.plan)
            self._logger.emit(EventType.PLAN_UPDATED, state.plan.model_dump())
            if self._run_dir is not None:
                save_state(self._run_dir, state)
            return "Plan updated."

        elif action.type == ActionType.LOAD_SKILL:
            skill_name = action.params["skill_name"]
            meta = self._registry.find(skill_name)
            if not meta:
                return f"Error: skill '{skill_name}' not found."
            body, report = self._loader.load_body(meta)
            state.active_skills.append(meta)
            self._sink.on_progress("load_skill", skill_name)
            self._logger.emit(EventType.SKILL_LOADED, {"skill": skill_name, **report})
            # Display summary for user (not full body — that goes to model context only)
            display_lines = [f"description: {meta.description}"]
            if meta.allowed_tools:
                display_lines.append(f"tools: {', '.join(meta.allowed_tools)}")
            self._sink.on_observation(f"skill:{skill_name}", "\n".join(display_lines))
            return f"[Skill: {skill_name}]\n{body}"

        elif action.type == ActionType.LOAD_RESOURCE:
            skill_name = action.params["skill_name"]
            resource = action.params["resource"]
            meta = self._registry.find(skill_name)
            if not meta:
                return f"Error: skill '{skill_name}' not found."
            try:
                excerpt, _report = self._loader.load_resource(
                    meta, resource, section_hint=action.params.get("section_hint")
                )
            except PathTraversalError as e:
                return f"PathTraversalBlocked: {e}"
            self._sink.on_progress("load_resource", resource)
            self._sink.on_observation(f"resource:{resource}", excerpt)
            return f"[Resource: {resource}]\n{excerpt}"

        elif action.type == ActionType.RUN_SCRIPT:
            return self._handle_run_script(action, state)

        elif action.type == ActionType.FINAL_ANSWER:
            content = action.params.get("content", "")
            if not streaming:
                self._sink.on_text_chunk(content, done=True)
            self._logger.emit(EventType.FINAL_ANSWER, {"content": content})
            state.status = "completed"
            return content

        else:
            return f"Error: unknown action type '{action.type}'."

    def _handle_run_script(self, action: Action, state: AgentState) -> str:
        """
        Handle RUN_SCRIPT action: permission check → path safety → approval → execute.

        Returns an observation string in all cases (never raises).
        Emits ACTION_FAILED on any error so the model can observe the failure.
        """
        if self._tools is None:
            obs = "Error: script execution not enabled (ToolsRuntime not configured)."
            self._logger.emit(EventType.ACTION_FAILED, {
                "action_type": ActionType.RUN_SCRIPT,
                "reason": "tools_not_configured",
            })
            return obs

        skill_name = action.params.get("skill_name", "")
        script = action.params.get("script", "")
        args = action.params.get("args", [])
        env_overrides = action.params.get("env_overrides")

        # Resolve the active skill metadata (must have been loaded first)
        meta = next(
            (m for m in state.active_skills if m.name == skill_name),
            None,
        )
        if meta is None:
            obs = (
                f"ToolNotAllowed: skill '{skill_name}' is not loaded. "
                "Use LOAD_SKILL first."
            )
            self._logger.emit(EventType.ACTION_FAILED, {
                "action_type": ActionType.RUN_SCRIPT,
                "reason": "skill_not_loaded",
                "skill": skill_name,
            })
            return obs

        try:
            result = self._tools.run_script(
                skill_meta=meta,
                script=script,
                args=args,
                env_overrides=env_overrides,
            )
        except ToolNotAllowedError as exc:
            obs = f"ToolNotAllowed: {exc}"
            self._logger.emit(EventType.ACTION_FAILED, {
                "action_type": ActionType.RUN_SCRIPT,
                "reason": "permission_denied",
                "skill": skill_name,
                "script": script,
            })
            self._sink.on_error(obs, recoverable=True)
            return obs
        except ApprovalDeniedError as exc:
            obs = f"ApprovalDenied: {exc}"
            self._logger.emit(EventType.ACTION_FAILED, {
                "action_type": ActionType.RUN_SCRIPT,
                "reason": "approval_denied",
                "skill": skill_name,
                "script": script,
            })
            self._sink.on_error(obs, recoverable=True)
            return obs
        except ToolsRuntimeError as exc:
            obs = f"ScriptError: {exc}"
            self._logger.emit(EventType.ACTION_FAILED, {
                "action_type": ActionType.RUN_SCRIPT,
                "reason": "runtime_error",
                "detail": str(exc),
            })
            self._sink.on_error(obs, recoverable=True)
            return obs

        self._sink.on_progress("run_script", script)

        if result.timed_out:
            obs = (
                f"ScriptTimedOut: '{script}' exceeded time limit.\n"
                f"stderr: {result.stderr}"
            )
            self._logger.emit(EventType.ACTION_FAILED, {
                "action_type": ActionType.RUN_SCRIPT,
                "reason": "timed_out",
                "script": script,
            })
            return obs

        obs_parts = [f"returncode={result.returncode}"]
        if result.stdout:
            obs_parts.append(f"stdout:\n{result.stdout}")
        if result.stderr:
            obs_parts.append(f"stderr:\n{result.stderr}")
        obs = "\n".join(obs_parts)
        self._sink.on_observation(f"script:{script}", obs)
        return obs

    def _check_dead_loop_hash(self, action: Action, state: AgentState) -> bool:
        """
        Maintain a sliding window of the last K action hashes.
        Return True if any hash appears >= 2 times (dead loop detected).
        """
        key = json.dumps(
            {"type": action.type, "params": action.params},
            sort_keys=True,
            ensure_ascii=False,
        )
        h = hashlib.sha256(key.encode()).hexdigest()[:16]

        state.recent_action_hashes.append(h)
        if len(state.recent_action_hashes) > self._dead_loop_window:
            state.recent_action_hashes.pop(0)

        # Dead loop if any hash appears >= 2 times within the window
        return len(state.recent_action_hashes) != len(set(state.recent_action_hashes))
