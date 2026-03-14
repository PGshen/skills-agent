"""ContextBuilder: context assembly and trimming (Phase B)."""
import json
from typing import Optional

from agent.plan import Action, Plan
from agent.state import AgentState
from skills.registry import SkillRegistry

_SYSTEM_PROMPT_HEADER = """You are an AI Agent. Complete the user's task by executing tool actions.

CRITICAL: Every reply MUST be a single valid JSON object. No prose, no markdown, no explanation outside the JSON.

## How to complete tasks
Use the tools below to produce REAL outputs. Examples:
- User asks to write a file → call write_file with the actual file content in "content"
- User asks to search → call web_search
- User asks to run code → call run_script

## !! update_plan does NOT produce any real output !!
update_plan is bookkeeping only. It does NOT write files, run code, or complete any work.
Calling update_plan when you should be calling write_file or run_script is WRONG and wastes turns.

## Output format
One JSON object per turn:
{
  "type": "<action_type>",
  "params": { <only the keys listed for that action type, no extras> }
}

## Available tools
"""

_SYSTEM_PROMPT_PLAN_SECTION = """
## Plan bookkeeping (optional, use sparingly)
- "update_plan":  {"plan": {"goal": "...", "steps": [...]}}  — track task progress only
- "final_answer": {"content": "<complete answer text>"}  — call when all work is done

### Efficiency tip: piggyback plan_update on work actions
Instead of a separate update_plan turn, include "plan_update" inside any work action's params:
{"type": "write_file", "params": {"path": "...", "content": "...", "plan_update": {"goal": "...", "steps": [{"id": "1", "status": "done"}]}}}

"""

_SYSTEM_PROMPT_RULES = """
## Rules
- Output ONLY the JSON object — no extra keys, no null-valued keys
- load_resource / run_script require the skill to be loaded first via load_skill
- DO NOT call update_plan instead of doing actual work
- DO NOT call final_answer while the plan has steps in "pending" or "in_progress" status
- Call final_answer only when all real work is done; params.content must contain the full response
"""

# Per-tool declaration lines (shown only when the tool is available)
_TOOL_DECLARATIONS: dict[str, str] = {
    "load_skill":    '- "load_skill":    {"skill_name": "<name>"}',
    "load_resource": '- "load_resource": {"skill_name": "<name>", "resource": "<filename>"}',
    "run_script":    '- "run_script":    {"skill_name": "<name>", "script": "<path>", "args": []}',
    "read_file":     '- "read_file":     {"path": "<path>", "max_bytes": 100000}',
    "list_dir":      '- "list_dir":      {"path": "<directory>", "max_entries": 100}',
    "grep":          '- "grep":          {"pattern": "<regex>", "path": "<directory or file>", "max_results": 50}',
    "write_file":    '- "write_file":    {"path": "<path>", "content": "<full file content>"}  [requires user approval]',
    "delete_file":   '- "delete_file":   {"path": "<path>"}  [requires user approval]',
    "web_search":    '- "web_search":    {"query": "<search query>", "max_results": 5}',
}

# Grouping: section header → tool names in order
_TOOL_SECTIONS: list[tuple[str, list[str]]] = [
    ("Skill actions:", ["load_skill", "load_resource", "run_script"]),
    ("File tools (low-risk):", ["read_file", "list_dir", "grep"]),
    ("Write tools (high-risk):", ["write_file", "delete_file"]),
    ("Web:", ["web_search"]),
]


def _build_tools_section(available_tools: list[str]) -> str:
    """Render only the tool declarations that are in available_tools."""
    # Skill actions (load_skill / load_resource) are always present — they are
    # not in ToolsRuntime but are core ReAct actions.
    always_available = {"load_skill", "load_resource"}
    effective = set(available_tools) | always_available

    lines: list[str] = []
    for header, tools in _TOOL_SECTIONS:
        section_lines = [
            _TOOL_DECLARATIONS[t] for t in tools if t in effective
        ]
        if section_lines:
            lines.append(header)
            lines.extend(section_lines)
    return "\n".join(lines)

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
        available_tools: Optional[list[str]] = None,
        stall_injection: Optional[str] = None,
    ) -> list[dict]:
        """
        Build the full messages list.
        If it exceeds _max_tokens, call _trim_to_limit.

        react_history: list of (action_dict_or_str, observation_str) tuples
        history_messages: cross-task session history from SessionContext
        available_tools: tools actually configured; None means show all tool types
        """
        skill_index = registry.to_index_text()
        plan_sum = _plan_summary(state.plan)
        tools_section = _build_tools_section(available_tools if available_tools is not None else list(_TOOL_DECLARATIONS))
        system_content = (
            _SYSTEM_PROMPT_HEADER
            + tools_section
            + _SYSTEM_PROMPT_PLAN_SECTION
            + "## Current progress\n" + plan_sum + "\n"
            + "\n## Available Skills\n" + skill_index + "\n"
            + _SYSTEM_PROMPT_RULES
        )

        msgs: list[dict] = []

        # ── System layer ─────────────────────────────────────────
        msgs.append({"role": "system", "content": system_content})

        # ── Session history layer (compressed summary + recent turns) ─
        # history_messages is already formatted [{"role": ..., "content": ...}, ...]
        msgs.extend(history_messages)

        # ── Current user input ───────────────────────────────────
        msgs.append({"role": "user", "content": state.user_input})

        # Mark end of fixed prefix (system + history + user_input)
        fixed_count = len(msgs)

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

        # ── Stall injection (appended last so the model sees it immediately) ──
        if stall_injection:
            msgs.append({"role": "user", "content": stall_injection})

        total = sum(self._msg_tokens(m) for m in msgs)
        if total > self._max_tokens:
            msgs = self._trim_to_limit(msgs, self._max_tokens, fixed_count=fixed_count)

        return msgs

    def _trim_to_limit(self, msgs: list[dict], max_tokens: int, fixed_count: int = 1) -> list[dict]:
        """
        Trimming strategy (least to most aggressive):
        1. Truncate observation content (keep first 500 chars)
        2. Drop oldest ReAct history turns (keep most recent 3)
        3. Truncate loaded skill body content (keep summary line)

        Never drop: fixed prefix (system + history_messages + user_input).
        fixed_count: number of messages at the start that must not be trimmed.
        """
        if len(msgs) <= fixed_count:
            return msgs

        fixed = msgs[:fixed_count]
        react = list(msgs[fixed_count:])

        # ── Step 1: Truncate observations ─────────────────────────
        for i, msg in enumerate(react):
            content = msg.get("content", "")
            if isinstance(content, str) and content.startswith("Observation: "):
                body = content[len("Observation: "):]
                if len(body) > _OBS_TRUNCATE_CHARS:
                    react[i] = {
                        **msg,
                        "content": "Observation: " + body[:_OBS_TRUNCATE_CHARS] + "…[truncated]",
                    }

        candidate = fixed + react
        if sum(self._msg_tokens(m) for m in candidate) <= max_tokens:
            return candidate

        # ── Step 2: Drop oldest ReAct pairs, keep at least MIN_REACT_TURNS ──
        # react contains consecutive (assistant, user[Observation]) pairs only.
        react_pairs: list[tuple[int, int]] = []  # (assistant_idx, observation_idx) in react
        i = 0
        while i < len(react) - 1:
            a = react[i]
            b = react[i + 1]
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

        react = [m for idx, m in enumerate(react) if idx not in dropped_indices]

        candidate = fixed + react
        if sum(self._msg_tokens(m) for m in candidate) <= max_tokens:
            return candidate

        # ── Step 3: Truncate skill body observations ──────────────
        for i, msg in enumerate(react):
            content = msg.get("content", "")
            if isinstance(content, str) and msg.get("role") == "user":
                if not content.startswith("Observation: ") and len(content) > _OBS_TRUNCATE_CHARS:
                    first_line = content.split("\n")[0]
                    react[i] = {**msg, "content": first_line + "\n…[truncated]"}

        return fixed + react
