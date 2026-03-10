"""ContextBuilder: context assembly and trimming (Phase B)."""
import json
from typing import Optional

from agent.plan import Action, Plan
from agent.state import AgentState
from skills.registry import SkillRegistry

SYSTEM_PROMPT_TEMPLATE = """你是一个 AI Agent，通过 ReAct 循环（推理 → 行动 → 观察）完成用户任务。

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

_MIN_REACT_TURNS = 3
_OBS_TRUNCATE_CHARS = 500


def _plan_summary(plan: Optional[Plan]) -> str:
    if plan is None:
        return "(no plan yet)"
    lines = [f"Goal: {plan.goal}", "Steps:"]
    for step in plan.steps:
        lines.append(f"  [{step.status.value:<11}] {step.id}: {step.description}")
    return "\n".join(lines)


class ContextBuilder:
    def __init__(self, max_context_tokens: int = 100_000):
        self._max_tokens = max_context_tokens

    def estimate_tokens(self, text: str) -> int:
        """Rough estimate: char count / 4 (no tiktoken dependency)."""
        return len(text) // 4

    def _msg_tokens(self, msg: dict) -> int:
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content)
        return self.estimate_tokens(content)

    def build(
        self,
        state: AgentState,
        registry: SkillRegistry,
        react_history: list[tuple],
        history_messages: list[dict],
    ) -> list[dict]:
        """
        Build the full messages list.
        If it exceeds _max_tokens, call _trim_to_limit.

        react_history: list of (action_dict_or_str, observation_str) tuples
        history_messages: cross-task session history from SessionContext
        """
        skill_index = registry.to_index_text()
        plan_sum = _plan_summary(state.plan)
        system_content = SYSTEM_PROMPT_TEMPLATE.format(
            skill_index=skill_index,
            plan_summary=plan_sum,
        )

        msgs: list[dict] = []

        # ── System layer ─────────────────────────────────────────
        msgs.append({"role": "system", "content": system_content})

        # ── Session history layer (compressed summary + recent turns) ─
        # history_messages is already formatted [{"role": ..., "content": ...}, ...]
        msgs.extend(history_messages)

        # ── ReAct history ────────────────────────────────────────
        for action, observation in react_history:
            if isinstance(action, str):
                action_content = action
            elif isinstance(action, Action):
                action_content = json.dumps(
                    {"type": action.type, "params": action.params}, ensure_ascii=False
                )
            else:
                action_content = json.dumps(action, ensure_ascii=False)
            msgs.append({"role": "assistant", "content": action_content})
            msgs.append({"role": "user", "content": f"Observation: {observation}"})

        # ── Current user input ───────────────────────────────────
        msgs.append({"role": "user", "content": state.user_input})

        total = sum(self._msg_tokens(m) for m in msgs)
        if total > self._max_tokens:
            msgs = self._trim_to_limit(msgs, self._max_tokens)

        return msgs

    def _trim_to_limit(self, msgs: list[dict], max_tokens: int) -> list[dict]:
        """
        Trimming strategy (least to most aggressive):
        1. Truncate observation content (keep first 500 chars)
        2. Drop oldest ReAct history turns (keep most recent 3)
        3. Truncate loaded skill body content (keep summary line)

        Never drop: system message, current user_input, plan summary, skill index.
        """
        # Identify fixed messages: system (index 0) and last user_input (index -1)
        # Everything in between is trimmable.
        if len(msgs) < 2:
            return msgs

        system_msg = msgs[0]
        current_input_msg = msgs[-1]
        middle = list(msgs[1:-1])

        # ── Step 1: Truncate observations ─────────────────────────
        for i, msg in enumerate(middle):
            content = msg.get("content", "")
            if isinstance(content, str) and content.startswith("Observation: "):
                body = content[len("Observation: "):]
                if len(body) > _OBS_TRUNCATE_CHARS:
                    middle[i] = {
                        **msg,
                        "content": "Observation: " + body[:_OBS_TRUNCATE_CHARS] + "…[truncated]",
                    }

        candidate = [system_msg] + middle + [current_input_msg]
        if sum(self._msg_tokens(m) for m in candidate) <= max_tokens:
            return candidate

        # ── Step 2: Drop oldest ReAct pairs, keep at least MIN_REACT_TURNS ──
        # ReAct pairs in `middle` are consecutive (assistant, user[Observation]) pairs.
        # We need to keep the non-ReAct history_messages intact and only drop
        # the oldest ReAct pairs.
        react_pairs: list[tuple[int, int]] = []  # (assistant_idx, observation_idx) in middle
        i = 0
        while i < len(middle) - 1:
            a = middle[i]
            b = middle[i + 1]
            if (
                a.get("role") == "assistant"
                and b.get("role") == "user"
                and isinstance(b.get("content", ""), str)
                and b["content"].startswith("Observation: ")
            ):
                react_pairs.append((i, i + 1))
                i += 2
            else:
                i += 1

        # Drop oldest pairs until under budget or only MIN_REACT_TURNS remain
        pairs_to_drop = max(0, len(react_pairs) - _MIN_REACT_TURNS)
        dropped_indices: set[int] = set()
        for k in range(pairs_to_drop):
            ai, oi = react_pairs[k]
            dropped_indices.add(ai)
            dropped_indices.add(oi)

        middle = [m for idx, m in enumerate(middle) if idx not in dropped_indices]

        candidate = [system_msg] + middle + [current_input_msg]
        if sum(self._msg_tokens(m) for m in candidate) <= max_tokens:
            return candidate

        # ── Step 3: Truncate skill body content (assistant messages that are not
        #    observations, i.e., loaded skill bodies injected as user messages) ──
        for i, msg in enumerate(middle):
            content = msg.get("content", "")
            if isinstance(content, str) and msg.get("role") == "user":
                if not content.startswith("Observation: ") and len(content) > _OBS_TRUNCATE_CHARS:
                    first_line = content.split("\n")[0]
                    middle[i] = {**msg, "content": first_line + "\n…[truncated]"}

        return [system_msg] + middle + [current_input_msg]
