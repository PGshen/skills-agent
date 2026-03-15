"""ReactAgent: atomic task executor with a focused ReAct loop.

Key design constraint: update_plan is NOT available here.
The model can only execute tools and call final_answer.
"""

import hashlib
import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

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


# ── System prompt templates ──────────────────────────────────────────────────

_REACT_SYSTEM_HEADER = """\
You are an executor agent. Your ONLY job is to complete this ONE task:

OVERALL GOAL: {goal}

TASK: {task_description}
{context_section}

CRITICAL: Every reply MUST be a single valid JSON object — no prose, no markdown.

## JSON format
{{"type": "<type>", "params": {{<params>}}}}

---

## 1. Work tools  (use these to gather information and make changes)
{tools_section}

## 2. Termination  (call exactly once when done — do NOT use as a tool)
- "final_answer": {{"content": "<concise result summary or FAILED: <reason>"}}

---

## Rules
- Use work tools to make progress; call final_answer only when the task is complete or definitively blocked.
- Do NOT plan, reflect, or narrate — just act.
- Output ONLY the JSON object — no extra keys, no null-valued keys.
"""

_TOOL_DECLARATIONS: dict[str, str] = {
    # "load_skill":    '- "load_skill":    {{"skill_name": "<name>"}}',
    # "load_resource": '- "load_resource": {{"skill_name": "<name>", "resource": "<filename>"}}',
    # "run_script":    '- "run_script":    {{"skill_name": "<name>", "script": "<path>", "args": []}}',
    "read_file":     '- "read_file":     {{"path": "<path>", "max_bytes": 100000}}',
    "list_dir":      '- "list_dir":      {{"path": "<directory>", "max_entries": 100}}',
    "grep":          '- "grep":          {{"pattern": "<regex>", "path": "<directory or file>", "max_results": 50}}',
    "write_file":    '- "write_file":    {{"path": "<path>", "content": "<full file content>"}}  [requires approval]',
    "delete_file":   '- "delete_file":   {{"path": "<path>"}}  [requires approval]',
    "web_search":    '- "web_search":    {{"query": "<search query>", "max_results": 5}}',
}

_TOOL_SECTIONS: list[tuple[str, list[str]]] = [
    # ("### Skill actions  (load a skill before running its scripts)",
    #  ["load_skill", "load_resource", "run_script"]),
    ("### Read tools  (inspect files and search)",
     ["read_file", "list_dir", "grep"]),
    ("### Write tools  (modify filesystem, require approval)",
     ["write_file", "delete_file"]),
    ("### Web",
     ["web_search"]),
]


def _build_tools_section(available_tools: list[str]) -> str:
    always_available = {"load_skill", "load_resource"}
    effective = set(available_tools) | always_available
    lines: list[str] = []
    for header, tools in _TOOL_SECTIONS:
        section_lines = [_TOOL_DECLARATIONS[t] for t in tools if t in effective]
        if section_lines:
            lines.append(header)
            lines.extend(section_lines)
    return "\n".join(lines)


def _build_system_prompt(task: SubTask, available_tools: list[str]) -> str:
    context_section = ""
    if task.context:
        context_section = f"\nBACKGROUND (completed steps):\n{task.context}\n"
    return _REACT_SYSTEM_HEADER.format(
        task_description=task.description,
        context_section=context_section,
        goal=task.goal or task.description,
        tools_section=_build_tools_section(available_tools),
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
        max_turns: int = 10,
        dead_loop_window: int = 4,
        max_context_tokens: int = 80_000,
    ):
        self._model = model
        self._registry = registry
        self._loader = loader
        self._logger = event_logger
        self._tools = tools
        self._sink = sink or NullSink()
        self._max_turns = max_turns
        self._dead_loop_window = dead_loop_window

    def run(self, task: SubTask) -> TaskResult:
        """
        Execute a single atomic SubTask. Returns TaskResult (never raises).
        """
        available_tools = self._tools.available_tools() if self._tools is not None else []
        system_prompt = _build_system_prompt(task, available_tools)

        # Build response format restricted to action types actually shown in the prompt
        declared_in_prompt = {t for _, tools in _TOOL_SECTIONS for t in tools}
        schema_action_types = list((set(available_tools) & declared_in_prompt) | {"final_answer"})
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

            messages = self._build_messages(system_prompt, task, react_history)
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
            # Exhausted turns or dead loop — treat as failure
            reason = "dead_loop" if state.dead_loop_triggered else "max_turns_exceeded"
            final_result = TaskResult(
                step_id=task.step_id,
                success=False,
                output=f"FAILED: {reason}",
            )

        self._logger.emit(EventType.SESSION_END, {
            "subtask": task.step_id,
            "turns": state.turn_count,
            "success": final_result.success,
        })
        return final_result

    # ── Context assembly ──────────────────────────────────────────────────────

    def _build_messages(
        self,
        system_prompt: str,
        task: SubTask,
        react_history: list[tuple[Action, str]],
    ) -> list[dict]:
        msgs: list[dict] = [
            {"role": "system", "content": system_prompt},
            # {"role": "user", "content": task.description},
        ]
        for action, observation in react_history:
            if isinstance(action, Action):
                action_content = json.dumps(
                    {"type": action.type, "params": action.params}, ensure_ascii=False
                )
            else:
                action_content = json.dumps(action, ensure_ascii=False)
            msgs.append({"role": "assistant", "content": action_content})
            msgs.append({"role": "user", "content": f"Observation: {observation}"})
        return msgs

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
                    pattern=action.params["pattern"],
                    path=action.params["path"],
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
