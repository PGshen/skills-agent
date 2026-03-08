"""AgentCore: ReAct main loop."""

import hashlib
import json
from typing import Optional

from agent.events import EventLogger, EventType
from agent.plan import Action, ActionType, Plan
from agent.state import AgentState
from model.base import ModelAdapter
from output.sink import NullSink, OutputSink
from skills.loader import PathTraversalError, SkillLoader
from skills.metadata import SkillMetadata
from skills.registry import SkillRegistry

SYSTEM_PROMPT_TEMPLATE = """\
你是一个 AI Agent，通过 ReAct 循环（推理 → 行动 → 观察）完成用户任务。

## 可用技能索引
{skill_index}

## 当前计划
{plan_summary}

## 行动协议
每次回复必须是一个 JSON 对象，格式：
{{
  "type": "<load_skill|load_resource|run_script|update_plan|final_answer>",
  "params": {{ ... }}
}}

## 约束
- 每次只能输出一个 action
- load_resource / run_script 必须在 load_skill 之后
- update_plan 中的 plan 为完整新 Plan（全量替换）
- final_answer 时输出完整最终答案
"""


def _format_plan_summary(plan: Optional[Plan]) -> str:
    """Format plan as human-readable summary for system prompt."""
    if plan is None:
        return "(no plan yet)"
    lines = [f"Goal: {plan.goal}", "Steps:"]
    for step in plan.steps:
        lines.append(f"  [{step.status.value:<11}] {step.id}: {step.description}")
    return "\n".join(lines)


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

    def run(
        self,
        user_input: str,
        history_messages: list[dict] = None,
    ) -> str:
        """
        Execute full ReAct loop, return final answer string.

        history_messages: built by SessionContext.build_history_messages() in chat mode.
        Single-shot mode (run command): omit or pass None (treated as []).
        """
        if history_messages is None:
            history_messages = []

        # Initialization
        skill_metas = self._registry.scan()
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
            action = self._model.next_action(messages)

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

            observation = self._execute_action(action, state)

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
        skill_index = self._registry.to_index_text()
        plan_summary = _format_plan_summary(state.plan)

        system_content = SYSTEM_PROMPT_TEMPLATE.format(
            skill_index=skill_index,
            plan_summary=plan_summary,
        )

        messages: list[dict] = [
            {"role": "system", "content": system_content},
        ]

        # History layer (chat mode): pre-compressed summary + recent raw turns
        messages.extend(history_messages)

        # In-task ReAct history: alternating action / observation
        for prev_action, observation in react_history:
            messages.append({
                "role": "assistant",
                "content": json.dumps(
                    {"type": prev_action.type, "params": prev_action.params},
                    ensure_ascii=False,
                ),
            })
            messages.append({
                "role": "user",
                "content": f"Observation: {observation}",
            })

        # Current user input
        messages.append({"role": "user", "content": state.user_input})

        return messages

    def _execute_action(self, action: Action, state: AgentState) -> str:
        """Execute a single Action, return observation string for next context."""

        if action.type == ActionType.UPDATE_PLAN:
            new_plan = Plan.model_validate(action.params["plan"])
            state.plan = state.plan.replace(new_plan) if state.plan else new_plan
            self._sink.on_plan_updated(state.plan)
            self._logger.emit(EventType.PLAN_UPDATED, state.plan.model_dump())
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
            return f"[Resource: {resource}]\n{excerpt}"

        elif action.type == ActionType.RUN_SCRIPT:
            # Phase A: script execution not yet implemented
            return "Error: script execution not enabled in this phase."

        elif action.type == ActionType.FINAL_ANSWER:
            content = action.params.get("content", "")
            self._sink.on_text_chunk(content, done=True)
            self._logger.emit(EventType.FINAL_ANSWER, {"content": content})
            state.status = "completed"
            return content

        else:
            return f"Error: unknown action type '{action.type}'."

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
