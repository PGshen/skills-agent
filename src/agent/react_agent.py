"""ReactAgent: atomic task executor with a focused ReAct loop.

Key design constraint: update_plan is NOT available here.
The model can only execute tools and call final_answer.
"""

import hashlib
import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from agent.context import ReactContextBuilder
from agent.events import EventLogger, EventType
from agent.multi_agent import SubTask, TaskResult
from agent.plan import Action, ActionType
from model.base import ModelAdapter, ModelResponseError
from model.openai import build_action_response_format
from output.sink import NullSink, OutputSink
from skills.loader import PathTraversalError, SkillLoader
from skills.metadata import SkillMetadata
from skills.registry import SkillRegistry
from tools.runtime import (
    ApprovalDeniedError,
    ToolNotAllowedError,
    ToolsRuntimeError,
)


# ── State ────────────────────────────────────────────────────────────────────

class ReactState(BaseModel):
    """Lightweight runtime state for a single ReactAgent run."""
    session_id: str
    task: SubTask
    status: str = "running"
    turn_count: int = 0
    recent_action_hashes: list[str] = Field(default_factory=list)
    active_skills: list[SkillMetadata] = Field(default_factory=list)
    dead_loop_triggered: bool = False

    model_config = {"arbitrary_types_allowed": True}

    def is_done(self) -> bool:
        return self.dead_loop_triggered or self.status in ("completed", "failed")


# ── ReactAgent ───────────────────────────────────────────────────────────────

class ReactAgent:
    """
    Atomic executor: runs a single SubTask to completion via a ReAct loop.

    Deliberately excludes update_plan from the action set — the model can
    only use execution tools and call final_answer.
    """

    def __init__(
        self,
        model: ModelAdapter,
        registry: SkillRegistry,
        loader: SkillLoader,
        event_logger: EventLogger,
        tools=None,
        sink: OutputSink = None,
        max_turns: int = 15,
        dead_loop_window: int = 4,
        max_context_tokens: int = 100_000,
        context_builder: Optional[ReactContextBuilder] = None,
    ):
        self._model = model
        self._registry = registry
        self._loader = loader
        self._logger = event_logger
        self._tools = tools
        self._sink = sink or NullSink()
        self._max_turns = max_turns
        self._dead_loop_window = dead_loop_window
        self._ctx_builder = context_builder or ReactContextBuilder(
            max_context_tokens=max_context_tokens
        )

    def run(self, task: SubTask) -> TaskResult:
        """
        Execute a single atomic SubTask. Returns TaskResult (never raises).
        """
        available_tools = self._tools.available_tools() if self._tools is not None else []
        skills_index = self._registry.to_index_text() if self._registry else ""

        # Build response format restricted to action types actually shown in the prompt.
        # Skill actions are always included when skills are available — they are handled
        # natively by ReactAgent, not via ToolsRuntime, so they don't appear in
        # available_tools but must still be in the schema.
        schema_action_types = list(
            (set(available_tools) & ReactContextBuilder.DECLARED_TOOLS) | {"final_answer"}
        )
        if skills_index and skills_index != "(no skills available)":
            for at in ("load_skill", "load_resource"):
                if at not in schema_action_types:
                    schema_action_types.append(at)
        react_format = build_action_response_format(schema_action_types)

        state = ReactState(
            session_id=self._logger.session_id,
            task=task,
        )
        self._logger.emit(EventType.SESSION_START, {
            "subtask": task.step_id,
            "description": task.description,
        })

        react_history: list[tuple[Action, str]] = []
        final_result: Optional[TaskResult] = None

        while not state.is_done() and state.turn_count < self._max_turns:
            state.turn_count += 1

            messages = self._ctx_builder.build(
                task, react_history, available_tools, skills_index=skills_index
            )
            self._sink.on_thinking_start(state.turn_count)

            try:
                if hasattr(self._model, "next_action_streaming"):
                    action = self._model.next_action_streaming(messages, response_format=react_format)
                else:
                    action = self._model.next_action(messages, response_format=react_format)
            except ModelResponseError as exc:
                self._sink.on_error(f"Model parse error: {exc}", recoverable=False)
                self._logger.emit(EventType.ACTION_FAILED, {
                    "subtask": task.step_id,
                    "reason": "parse_error",
                    "detail": str(exc),
                })
                break

            self._logger.emit(EventType.ACTION_REQUESTED, {
                "subtask": task.step_id,
                "turn": state.turn_count,
                "action_type": action.type,
                "params": action.params,
            })

            if self._check_dead_loop(action, state):
                state.dead_loop_triggered = True
                self._sink.on_error("Dead loop detected", recoverable=False)
                self._logger.emit(EventType.DEAD_LOOP_DETECTED, {"subtask": task.step_id})
                break

            observation, result = self._execute_action(action, state)

            self._logger.emit(EventType.ACTION_COMPLETED, {
                "subtask": task.step_id,
                "turn": state.turn_count,
                "action_type": action.type,
                "observation": observation[:200],
            })

            react_history.append((action, observation))

            if result is not None:
                final_result = result
                break

        if final_result is None:
            if state.dead_loop_triggered:
                final_result = TaskResult(
                    step_id=task.step_id,
                    success=False,
                    output="FAILED: dead_loop",
                )
            else:
                # Turns exhausted — ask the model to wrap up based on progress so far
                final_result = self._request_graceful_finish(
                    task, react_history, available_tools, skills_index, react_format
                )

        self._logger.emit(EventType.SESSION_END, {
            "subtask": task.step_id,
            "turns": state.turn_count,
            "success": final_result.success,
        })
        return final_result

    # ── Graceful finish ───────────────────────────────────────────────────────

    def _request_graceful_finish(
        self,
        task: SubTask,
        react_history: list[tuple[Action, str]],
        available_tools: list[str],
        skills_index: str,
        react_format,
    ) -> TaskResult:
        """Make one final model call when turns are exhausted.

        Injects a stall_injection message that instructs the model to call
        final_answer immediately with the best result it can provide from
        the work done so far. Falls back to a FAILED result if the model
        does not comply.
        """
        nudge = (
            "You have reached the maximum number of turns. "
            "You MUST call final_answer RIGHT NOW with the best result you can provide "
            "based on the work done so far. Do NOT call any more tools."
        )
        messages = self._ctx_builder.build(
            task, react_history, available_tools,
            stall_injection=nudge, skills_index=skills_index,
        )
        self._sink.on_thinking_start(0, "Wrapping up…")
        try:
            if hasattr(self._model, "next_action_streaming"):
                action = self._model.next_action_streaming(messages, response_format=react_format)
            else:
                action = self._model.next_action(messages, response_format=react_format)
        except ModelResponseError:
            return TaskResult(
                step_id=task.step_id,
                success=False,
                output="FAILED: max_turns_exceeded",
            )

        if action.type == ActionType.FINAL_ANSWER:
            content = action.params.get("content", "")
            if not hasattr(self._model, "next_action_streaming"):
                self._sink.on_text_chunk(content, done=True)
            self._logger.emit(EventType.FINAL_ANSWER, {"content": content, "graceful": True})
            success = not content.startswith("FAILED:")
            return TaskResult(step_id=task.step_id, success=success, output=content)

        # Model ignored the instruction — return partial failure with whatever we have
        last_output = react_history[-1][1] if react_history else ""
        partial = f"[Partial result — max turns reached]\n{last_output}" if last_output else "FAILED: max_turns_exceeded"
        return TaskResult(step_id=task.step_id, success=False, output=partial)

    # ── Action dispatch ───────────────────────────────────────────────────────

    def _execute_action(
        self, action: Action, state: ReactState
    ) -> tuple[str, Optional[TaskResult]]:
        """
        Execute one action. Returns (observation, TaskResult|None).
        TaskResult is only set when the task is complete (final_answer).
        """
        if action.type == ActionType.UPDATE_PLAN:
            # Explicitly rejected — model should not call this
            obs = (
                "Error: update_plan is not available in executor mode. "
                "Use write_file, run_script, or another work tool to complete your task."
            )
            return obs, None

        elif action.type == ActionType.LOAD_SKILL:
            skill_name = action.params.get("skill_name", "")
            meta = self._registry.find(skill_name)
            if not meta:
                return f"Error: skill '{skill_name}' not found.", None
            body, report = self._loader.load_body(meta)
            state.active_skills.append(meta)
            self._sink.on_progress("load_skill", skill_name)
            self._logger.emit(EventType.SKILL_LOADED, {"skill": skill_name, **report})
            display_lines = [f"description: {meta.description}"]
            if meta.allowed_tools:
                display_lines.append(f"tools: {', '.join(meta.allowed_tools)}")
            self._sink.on_observation(f"skill:{skill_name}", "\n".join(display_lines))
            return f"[Skill: {skill_name}]\n{body}", None

        elif action.type == ActionType.LOAD_RESOURCE:
            skill_name = action.params.get("skill_name", "")
            resource = action.params.get("resource", "")
            meta = self._registry.find(skill_name)
            if not meta:
                return f"Error: skill '{skill_name}' not found.", None
            try:
                excerpt, _report = self._loader.load_resource(
                    meta, resource, section_hint=action.params.get("section_hint")
                )
            except PathTraversalError as e:
                return f"PathTraversalBlocked: {e}", None
            self._sink.on_progress("load_resource", resource)
            self._sink.on_observation(f"resource:{resource}", excerpt)
            return f"[Resource: {resource}]\n{excerpt}", None

        elif action.type == ActionType.RUN_SCRIPT:
            return self._handle_run_script(action, state), None

        elif action.type == ActionType.READ_FILE:
            obs = self._handle_read_only_tool(
                action, "read_file",
                call=lambda: self._tools.read_file(
                    path=action.params["path"],
                    max_bytes=action.params.get("max_bytes", 100_000),
                ),
                fmt=lambda r: r["content"] + ("\n[truncated]" if r.get("truncated") else ""),
            )
            return obs, None

        elif action.type == ActionType.LIST_DIR:
            obs = self._handle_read_only_tool(
                action, "list_dir",
                call=lambda: self._tools.list_dir(
                    path=action.params["path"],
                    max_entries=action.params.get("max_entries", 100),
                ),
                fmt=lambda r: "\n".join(r["entries"]) + ("\n[truncated]" if r.get("truncated") else ""),
            )
            return obs, None

        elif action.type == ActionType.GREP:
            obs = self._handle_read_only_tool(
                action, "grep",
                call=lambda: self._tools.grep(
                    pattern=action.params.get("pattern", ""),
                    path=action.params.get("path", "."),
                    max_results=action.params.get("max_results", 50),
                ),
                fmt=lambda r: "\n".join(
                    f"{m['file']}:{m['line']}: {m['content']}" for m in r["matches"]
                ) + ("\n[truncated]" if r.get("truncated") else ""),
            )
            return obs, None

        elif action.type == ActionType.WEB_SEARCH:
            obs = self._handle_read_only_tool(
                action, "web_search",
                call=lambda: self._tools.web_search(
                    query=action.params.get("query", ""),
                    max_results=action.params.get("max_results", 5),
                ),
                fmt=lambda r: "\n\n".join(
                    f"[{i+1}] {res['title']}\n{res['url']}\n{res['content']}"
                    for i, res in enumerate(r["results"])
                ) or "(no results)",
            )
            return obs, None

        elif action.type == ActionType.WRITE_FILE:
            obs = self._handle_write_tool(action)
            artifact = action.params.get("path", "")
            return obs, None  # artifact tracking done separately if needed

        elif action.type == ActionType.DELETE_FILE:
            obs = self._handle_write_tool(action)
            return obs, None

        elif action.type == ActionType.FINAL_ANSWER:
            content = action.params.get("content", "")
            # Non-streaming path: emit here. Streaming path (next_action_streaming)
            # already emitted chunks via on_text_chunk during the model call.
            if not hasattr(self._model, "next_action_streaming"):
                self._sink.on_text_chunk(content, done=True)
            self._logger.emit(EventType.FINAL_ANSWER, {"content": content})
            state.status = "completed"
            success = not content.startswith("FAILED:")
            result = TaskResult(
                step_id=state.task.step_id,
                success=success,
                output=content,
            )
            return content, result

        else:
            return f"Error: unknown action type '{action.type}'.", None

    # ── Tool helpers (same pattern as original AgentCore) ─────────────────────

    def _handle_run_script(self, action: Action, state: ReactState) -> str:
        if self._tools is None:
            return "Error: script execution not enabled."

        skill_name = action.params.get("skill_name", "")
        script = action.params.get("script", "")
        args = action.params.get("args", [])
        env_overrides = action.params.get("env_overrides")

        meta = next((m for m in state.active_skills if m.name == skill_name), None)
        if meta is None:
            return f"ToolNotAllowed: skill '{skill_name}' is not loaded. Use LOAD_SKILL first."

        try:
            result = self._tools.run_script(
                skill_meta=meta, script=script, args=args, env_overrides=env_overrides,
            )
        except ToolNotAllowedError as exc:
            obs = f"ToolNotAllowed: {exc}"
            self._sink.on_error(obs, recoverable=True)
            return obs
        except ApprovalDeniedError as exc:
            obs = f"ApprovalDenied: {exc}"
            self._sink.on_error(obs, recoverable=True)
            return obs
        except ToolsRuntimeError as exc:
            obs = f"ScriptError: {exc}"
            self._sink.on_error(obs, recoverable=True)
            return obs

        self._sink.on_progress("run_script", script)

        if result.timed_out:
            return f"ScriptTimedOut: '{script}' exceeded time limit.\nstderr: {result.stderr}"

        obs_parts = [f"returncode={result.returncode}"]
        if result.stdout:
            obs_parts.append(f"stdout:\n{result.stdout}")
        if result.stderr:
            obs_parts.append(f"stderr:\n{result.stderr}")
        obs = "\n".join(obs_parts)
        self._sink.on_observation(f"script:{script}", obs)
        return obs

    def _handle_write_tool(self, action: Action) -> str:
        if self._tools is None:
            return f"Error: {action.type} not available (ToolsRuntime not configured)."

        label = action.params.get("path", "")
        self._sink.on_progress(str(action.type), label)

        try:
            if action.type == ActionType.WRITE_FILE:
                result = self._tools.write_file(
                    path=action.params["path"],
                    content=action.params["content"],
                )
            else:
                result = self._tools.delete_file(path=action.params["path"])
        except ToolNotAllowedError as exc:
            obs = f"ToolNotAllowed: {exc}"
            self._sink.on_error(obs, recoverable=True)
            return obs
        except ApprovalDeniedError as exc:
            obs = f"ApprovalDenied: {exc}"
            self._sink.on_error(obs, recoverable=True)
            return obs

        if "error" in result:
            return f"Error: {result['error']}"

        return f"{action.type} succeeded: {label}"

    def _handle_read_only_tool(self, action: Action, tool_name: str, call, fmt) -> str:
        if self._tools is None:
            return f"Error: {tool_name} not available (ToolsRuntime not configured)."

        detail = action.params.get("path") or action.params.get("query", "")
        self._sink.on_progress(tool_name, detail)

        result = call()
        if "error" in result:
            self._logger.emit(EventType.ACTION_FAILED, {
                "action_type": action.type,
                "reason": "tool_error",
                "detail": result["error"],
            })
            return f"Error: {result['error']}"

        obs = fmt(result)
        self._sink.on_observation(f"{tool_name}:{detail}", obs)
        return obs

    def _check_dead_loop(self, action: Action, state: ReactState) -> bool:
        key = json.dumps(
            {"type": action.type, "params": action.params},
            sort_keys=True, ensure_ascii=False,
        )
        h = hashlib.sha256(key.encode()).hexdigest()[:16]
        state.recent_action_hashes.append(h)
        if len(state.recent_action_hashes) > self._dead_loop_window:
            state.recent_action_hashes.pop(0)
        return len(state.recent_action_hashes) != len(set(state.recent_action_hashes))
