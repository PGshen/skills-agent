"""Context builders for each agent role (Phase B redesign).

Three specialised builders replace the old monolithic ContextBuilder:
  - ClassifyContextBuilder      : EntryAgent classification call
  - OrchestratorContextBuilder  : Orchestrator decompose / synthesize calls
                                  + SubTask.context assembly
  - ReactContextBuilder         : ReactAgent execution-loop message assembly
                                  (with optional ReactHistoryCompressor)
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import TYPE_CHECKING, Optional

from agent.multi_agent import SubTask, TaskResult
from agent.plan import Action, Plan

if TYPE_CHECKING:
    from session.react_compressor import ReactHistoryCompressor

_log = logging.getLogger(__name__)


# ── Shared utility ────────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """Rough estimate: char count / 4 (no tiktoken dependency)."""
    return len(text) // 4


def _plan_summary(plan: Optional[Plan]) -> str:
    """Format a Plan as a human-readable text block.

    Kept as a module-level function for backward-compat (re-exported by core.py).
    """
    if plan is None:
        return "(no plan yet)"
    lines = [f"Goal: {plan.goal}", "Steps:"]
    for step in plan.steps:
        lines.append(f"  [{step.status.value:<11}] {step.id}: {step.description}")
    return "\n".join(lines)


# ── ClassifyContextBuilder ────────────────────────────────────────────────────

_CLASSIFY_PROMPT = """\
Classify the following user request as "simple", "medium", or "complex".

simple:  Can be answered immediately from general knowledge — no tools, no file reading needed.
  Examples: "What is quicksort?", "Translate this sentence", "Explain this error message"

medium:  Requires using tools (read files, search) but is a single focused task.
  Examples: "Find all TODO comments in this repo", "What does this function do?", "Search X and summarize"

complex: Requires planning, writing files, or multiple distinct sequential steps.
  Examples: "Write a quicksort script and save it", "Build Y feature", "Refactor module X and update tests"

Output a single JSON object only:
{{"type": "final_answer", "params": {{"content": "simple"}}}}
or
{{"type": "final_answer", "params": {{"content": "medium"}}}}
or
{{"type": "final_answer", "params": {{"content": "complex"}}}}

User request: {user_input}"""


class ClassifyContextBuilder:
    """Builds the messages list for the EntryAgent classification call.

    No session history is injected — the classification decision does not
    depend on prior conversation turns.
    """

    def build(self, user_input: str) -> list[dict]:
        return [{"role": "user", "content": _CLASSIFY_PROMPT.format(user_input=user_input)}]


# ── DirectAnswerContextBuilder ────────────────────────────────────────────────

_DIRECT_ANSWER_SYSTEM = """\
You are a helpful assistant. Answer the user's question directly and concisely.
Output a single JSON object:
{"type": "final_answer", "params": {"content": "<your answer>"}}"""


class DirectAnswerContextBuilder:
    """Builds messages for a direct (simple) answer — includes session history."""

    def build(self, user_input: str, history_messages: list[dict]) -> list[dict]:
        msgs: list[dict] = [{"role": "system", "content": _DIRECT_ANSWER_SYSTEM}]
        msgs.extend(history_messages)
        msgs.append({"role": "user", "content": user_input})
        return msgs


# ── ClassifyAndAnswerContextBuilder ──────────────────────────────────────────

_CLASSIFY_AND_ANSWER_SYSTEM = """\
You are a helpful assistant. Decide whether the user's request can be answered
immediately from general knowledge, or whether it needs tool use or planning.

If it can be answered directly (no tools, no file reading, no planning needed):
  Answer now. Output: {"type": "final_answer", "params": {"content": "<answer>"}}

If it needs tool use or file access but is a single focused task (medium):
  Output: {"type": "final_answer", "params": {"route": "medium"}}

If it requires planning, writing files, or multiple distinct sequential steps (complex):
  Output: {"type": "final_answer", "params": {"route": "complex"}}

LANGUAGE: Always write the "content" value in the same language as the user's message, \
unless the user explicitly requests a different language.

CRITICAL: Output ONLY a single JSON object — no prose, no markdown."""


class ClassifyAndAnswerContextBuilder:
    """Combines classification and direct answering in a single model call.

    For simple requests the model answers directly (params.content is set).
    For medium/complex the model signals routing (params.route is set).
    Session history is included so direct answers are context-aware.
    """

    def build(
        self,
        user_input: str,
        history_messages: list[dict],
        skills_index: str = "",
    ) -> list[dict]:
        system = f"Today's date is {date.today().isoformat()}.\n" + _CLASSIFY_AND_ANSWER_SYSTEM
        if skills_index and skills_index != "(no skills available)":
            system += (
                "\n\nAVAILABLE SKILLS (specialized task instructions):\n"
                f"{skills_index}\n"
                "When the user's request matches or is closely related to one of these skills "
                '(e.g. reviewing code → code-review, committing → git-commit), route as "medium". '
                "Prefer routing to a skill over answering directly whenever a relevant skill exists."
            )
        msgs: list[dict] = [{"role": "system", "content": system}]
        msgs.extend(history_messages)
        msgs.append({"role": "user", "content": user_input})
        return msgs


# ── OrchestratorContextBuilder ────────────────────────────────────────────────

_ORCHESTRATOR_SYSTEM = """\
You are an orchestrator agent responsible for planning and synthesizing complex tasks.
CRITICAL: Every reply MUST be a single valid JSON object — no prose, no markdown."""

_DECOMPOSE_PROMPT = """\
You are a task planner. Break the user's request into a small number of meaningful, high-level steps.

Guidelines:
- Aim for 2–4 steps; only use more if the task genuinely requires distinct phases
- Each step should represent a coherent chunk of work, not a single line of code or one command
- Group closely related actions together (e.g., "implement X and write tests for it" is one step, not two)
- Simple, self-contained tasks (write a function, answer a question) need only 1 step
- Steps must be executable in sequence by a separate agent

Output a single JSON object (no prose, no markdown):
{{
  "type": "update_plan",
  "params": {{
    "plan": {{
      "goal": "<restate the user's goal concisely>",
      "steps": [
        {{"id": "1", "description": "<meaningful step>", "status": "pending"}},
        {{"id": "2", "description": "<meaningful step>", "status": "pending"}}
      ]
    }}
  }}
}}

User request: {user_input}"""

_SYNTHESIZE_PROMPT = """\
You are a result synthesizer. Given the user's original request and the results
of each completed step, write a comprehensive final answer for the user.

User request: {user_input}

Step results:
{step_results}

Write a clear, complete response. Include relevant outputs, file paths, or summaries.
Always write the "content" value in the same language as the user's request above, \
unless the user explicitly requested a different language.
Output a single JSON object:
{{"type": "final_answer", "params": {{"content": "<your complete answer>"}}}}"""


class OrchestratorContextBuilder:
    """Builds messages for the Orchestrator's decompose and synthesize phases,
    and assembles the SubTask.context string passed to each ReactAgent.

    Session history (history_messages) is injected into Orchestrator calls so
    the model is aware of user preferences and prior conversation turns.

    For SubTask.context, history is converted to plain text so ReactAgent can
    read it inside its system prompt without receiving full message history.

    Note: history_messages from SessionContext.build_history_messages() already
    contains *both* the compressed_summary (older turns) and recent verbatim
    turns, so injecting it covers the full conversation history.
    """

    def __init__(self, max_context_tokens: int = 64_000):
        self._max_tokens = max_context_tokens

    # ── Decompose ─────────────────────────────────────────────────────────────

    def build_decompose(
        self,
        user_input: str,
        history_messages: list[dict],
    ) -> list[dict]:
        """Messages for the decompose phase (produces a Plan)."""
        msgs: list[dict] = [{"role": "system", "content": _ORCHESTRATOR_SYSTEM}]
        msgs.extend(history_messages)
        msgs.append({"role": "user", "content": _DECOMPOSE_PROMPT.format(user_input=user_input)})
        return msgs

    # ── Synthesize ────────────────────────────────────────────────────────────

    def build_synthesize(
        self,
        user_input: str,
        results: list[TaskResult],
        history_messages: list[dict],
    ) -> list[dict]:
        """Messages for the synthesize phase (produces a final answer)."""
        step_results = self._format_results(results)
        msgs: list[dict] = [{"role": "system", "content": _ORCHESTRATOR_SYSTEM}]
        msgs.extend(history_messages)
        msgs.append({
            "role": "user",
            "content": _SYNTHESIZE_PROMPT.format(
                user_input=user_input,
                step_results=step_results,
            ),
        })
        return msgs

    # ── SubTask context ───────────────────────────────────────────────────────

    def build_subtask_context(
        self,
        prior_results: list[TaskResult],
        history_messages: list[dict],
    ) -> str:
        """Build the plain-text context string injected into SubTask.context.

        Combines two information sources so ReactAgent can see both:
        1. Session history — history_messages already merges compressed_summary
           (older turns) and recent_turns (latest K turns) from SessionContext,
           covering the full conversation including the most recent user prefs.
        2. Prior step results from the current task (if any).

        The result is injected into ReactAgent's system prompt via the
        BACKGROUND section, making session preferences (e.g. "reply in Chinese")
        visible to the executor without passing full message history through
        the react loop.
        """
        parts: list[str] = []

        # Session context: convert history_messages to plain text
        if history_messages:
            session_lines: list[str] = []
            for msg in history_messages:
                role = msg.get("role", "")
                content = msg.get("content", "")
                # Strip <summary>...</summary> wrapper added by SessionContext
                if content.startswith("<summary>") and content.endswith("</summary>"):
                    content = content[9:-10]
                if role == "user":
                    session_lines.append(f"User: {content}")
                elif role == "assistant":
                    session_lines.append(f"Assistant: {content}")
            if session_lines:
                parts.append("[Session context]\n" + "\n".join(session_lines))

        # Prior step results
        if prior_results:
            result_lines = ["[Completed steps so far]"]
            for r in prior_results:
                status = "✓" if r.success else "✗"
                result_lines.append(f"  [{status}] Step {r.step_id}: {r.output}")
            parts.append("\n".join(result_lines))

        return "\n\n".join(parts)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _format_results(self, results: list[TaskResult]) -> str:
        if not results:
            return "(no steps completed)"
        lines = []
        for r in results:
            status = "SUCCESS" if r.success else "FAILED"
            lines.append(f"Step {r.step_id} [{status}]: {r.output}")
            if r.artifacts:
                lines.append(f"  Artifacts: {', '.join(r.artifacts)}")
        return "\n".join(lines)


# ── ReactContextBuilder ───────────────────────────────────────────────────────

_REACT_SYSTEM_HEADER = """\
You are an executor agent. Today's date is {today}.
Your ONLY job is to complete this ONE task:

OVERALL GOAL: {goal}

TASK: {task_description}
{context_section}
CRITICAL: Every reply MUST be a single valid JSON object — no prose, no markdown.

## JSON format
{{"type": "<type>", "params": {{<params>}}}}

---

## 1. Work tools  (use these to gather information and make changes)
{tools_section}{skills_section}

## 2. Termination  (call exactly once when done — do NOT use as a tool)
- "final_answer": {{"content": "<concise result summary or FAILED: <reason>"}}

---

## Rules
- Use work tools to make progress; call final_answer only when the task is complete or definitively blocked.
- Do NOT plan, reflect, or narrate — just act.
- Output ONLY the JSON object — no extra keys, no null-valued keys.
- LANGUAGE: Write the final_answer "content" in the same language as the user's original request, unless the user explicitly requested a different language.
"""

_REACT_TOOL_DECLARATIONS: dict[str, str] = {
    "read_file":   '- "read_file":   {{"path": "<path>", "max_bytes": 100000}}',
    "list_dir":    '- "list_dir":    {{"path": "<directory>", "max_entries": 100}}',
    "grep":        '- "grep":        {{"pattern": "<regex>", "path": "<directory or file>", "max_results": 50}}',
    "write_file":  (
        '- "write_file" (overwrite): {{"path": "<path>", "content": "<full file content>"}}  [requires approval]\n'
        '- "write_file" (patch):     {{"path": "<path>", "old_str": "<exact text to replace>", "new_str": "<replacement text>"}}  [requires approval]\n'
        '  Patch rules: old_str must match exactly once; prefer patch over overwrite for targeted edits.'
    ),
    "delete_file": '- "delete_file": {{"path": "<path>"}}  [requires approval]',
    "web_search":  '- "web_search":  {{"query": "<search query>", "max_results": 5}}',
}

_REACT_TOOL_SECTIONS: list[tuple[str, list[str]]] = [
    ("### Read tools  (inspect files and search)", ["read_file", "list_dir", "grep"]),
    ("### Write tools  (modify filesystem, require approval)", ["write_file", "delete_file"]),
    ("### Web", ["web_search"]),
]


def _build_skills_section(skills_index: str) -> str:
    """Build the '### Skills' block injected into the React system prompt.

    Returns an empty string when no skills are available so the surrounding
    whitespace in _REACT_SYSTEM_HEADER is not affected.
    """
    if not skills_index or skills_index == "(no skills available)":
        return ""
    return (
        "\n\n### Skills  (task-specific instruction sets)\n"
        "IMPORTANT: If a skill below matches your task, call load_skill FIRST "
        "before using any other tools — it provides the instructions and approach you should follow.\n"
        f"{skills_index}\n"
        '- "load_skill":    {{"skill_name": "<name>"}}'
        "  — load task-specific instructions into context\n"
        '- "load_resource": {{"skill_name": "<name>", "resource": "<rel-path>", '
        '"section_hint": "<optional-section>"}}'
        "  — load a skill's reference file"
    )


def _build_react_tools_section(available_tools: list[str]) -> str:
    effective = set(available_tools)
    lines: list[str] = []
    for header, tools in _REACT_TOOL_SECTIONS:
        section_lines = [_REACT_TOOL_DECLARATIONS[t] for t in tools if t in effective]
        if section_lines:
            lines.append(header)
            lines.extend(section_lines)
    return "\n".join(lines)


def _build_react_system_prompt(
    task: SubTask, available_tools: list[str], skills_index: str = ""
) -> str:
    context_section = ""
    if task.context:
        context_section = f"BACKGROUND:\n{task.context}\n"
    return _REACT_SYSTEM_HEADER.format(
        today=date.today().isoformat(),
        task_description=task.description,
        context_section=context_section,
        goal=task.goal or task.description,
        tools_section=_build_react_tools_section(available_tools),
        skills_section=_build_skills_section(skills_index),
    )


class ReactContextBuilder:
    """Assembles messages for ReactAgent's execution loop.

    Session history is NOT injected here — background context arrives via
    SubTask.context (a plain-text string pre-built by
    OrchestratorContextBuilder.build_subtask_context).

    When react_history exceeds the token budget, the oldest pairs
    (action_msg, observation_msg) are compressed into a semantic summary
    by ReactHistoryCompressor, preserving information instead of discarding it.
    If no compressor is configured (e.g. tests with MockModel), a warning is
    logged and the messages are returned as-is.
    """

    # Tool names that appear in the system prompt — used by ReactAgent to
    # compute the allowed response format schema.
    # load_skill / load_resource are handled natively by ReactAgent (not via
    # ToolsRuntime) so they are added here explicitly rather than via
    # _REACT_TOOL_SECTIONS which only covers ToolsRuntime-managed tools.
    DECLARED_TOOLS: frozenset[str] = frozenset(
        t for _, tools in _REACT_TOOL_SECTIONS for t in tools
    ) | frozenset({"load_skill", "load_resource"})

    def __init__(
        self,
        max_context_tokens: int = 100_000,
        compressor: Optional["ReactHistoryCompressor"] = None,
    ):
        self._max_tokens = max_context_tokens
        self._compressor = compressor

    def build(
        self,
        task: SubTask,
        react_history: list[tuple],
        available_tools: list[str],
        stall_injection: Optional[str] = None,
        skills_index: str = "",
    ) -> list[dict]:
        """Build the messages list for one ReactAgent turn.

        react_history: list of (Action|dict|str, observation_str) tuples
        available_tools: tool names actually configured in ToolsRuntime
        stall_injection: optional nudge message appended last
        skills_index: registry index text injected into the Skills section;
                      empty string suppresses the section entirely
        """
        system_prompt = _build_react_system_prompt(task, available_tools, skills_index)
        msgs = self._assemble(system_prompt, react_history, stall_injection)

        total = sum(estimate_tokens(m.get("content", "")) for m in msgs)
        if total > self._max_tokens:
            msgs = self._compress_history(system_prompt, react_history, stall_injection)

        return msgs

    # ── Private helpers ───────────────────────────────────────────────────────

    def _assemble(
        self,
        system_prompt: str,
        react_history: list[tuple],
        stall_injection: Optional[str],
    ) -> list[dict]:
        msgs: list[dict] = [{"role": "system", "content": system_prompt}]
        for action, observation in react_history:
            msgs.append({"role": "assistant", "content": _action_to_json(action)})
            msgs.append({"role": "user", "content": f"Observation: {observation}"})
        if stall_injection:
            msgs.append({"role": "user", "content": stall_injection})
        return msgs

    def _compress_history(
        self,
        system_prompt: str,
        react_history: list[tuple],
        stall_injection: Optional[str],
    ) -> list[dict]:
        """Compress oldest react pairs to reclaim token budget.

        Strategy: keep the last 3 pairs verbatim; compress everything older
        into a single summary message via ReactHistoryCompressor.
        Falls back to returning the full (over-budget) messages when no
        compressor is configured or when history is too short to compress.
        """
        _MIN_KEEP_PAIRS = 3

        if self._compressor is None or len(react_history) <= _MIN_KEEP_PAIRS:
            _log.warning(
                "ReactContextBuilder: context exceeds %d tokens but %s; "
                "returning full history.",
                self._max_tokens,
                "no compressor configured" if self._compressor is None
                else "not enough history to compress",
            )
            return self._assemble(system_prompt, react_history, stall_injection)

        compress_count = len(react_history) - _MIN_KEEP_PAIRS

        # Convert oldest pairs to (assistant_msg_dict, user_observation_msg_dict)
        pairs_to_compress: list[tuple[dict, dict]] = [
            (
                {"role": "assistant", "content": _action_to_json(action)},
                {"role": "user", "content": f"Observation: {observation}"},
            )
            for action, observation in react_history[:compress_count]
        ]

        summary_msg = self._compressor.compress(pairs_to_compress)

        # Rebuild with summary replacing compressed pairs
        msgs: list[dict] = [{"role": "system", "content": system_prompt}, summary_msg]
        for action, observation in react_history[compress_count:]:
            msgs.append({"role": "assistant", "content": _action_to_json(action)})
            msgs.append({"role": "user", "content": f"Observation: {observation}"})
        if stall_injection:
            msgs.append({"role": "user", "content": stall_injection})
        return msgs


# ── Shared helper ─────────────────────────────────────────────────────────────

def _action_to_json(action) -> str:
    """Serialise an action (Action object, dict, or raw string) to JSON string."""
    if isinstance(action, Action):
        return json.dumps({"type": action.type, "params": action.params}, ensure_ascii=False)
    if isinstance(action, str):
        return action
    return json.dumps(action, ensure_ascii=False)
