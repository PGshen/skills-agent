"""AgentState persistence and crash recovery."""

import json
from pathlib import Path
from typing import Optional

from .events import EventType
from .state import AgentState


def load_state(run_dir: Path) -> Optional[AgentState]:
    """Load AgentState from run_dir/state.json. Returns None if missing or invalid."""
    state_path = run_dir / "state.json"
    if not state_path.exists():
        return None
    try:
        return AgentState.model_validate_json(state_path.read_text())
    except Exception:
        return None


def save_state(run_dir: Path, state: AgentState) -> None:
    """Write AgentState to run_dir/state.json (overwrite)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(state.model_dump_json(indent=2))


def build_resume_context(run_dir: Path, state: AgentState) -> list[dict]:
    """
    Read events.jsonl and build a resume summary message to inject as history_messages.

    Returns a list with one user message containing a <resume> block, or [] if empty.
    """
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        return []

    completed_actions: list[str] = []
    for line in events_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type", "")
        data = event.get("data", {})

        if event_type == EventType.SKILL_LOADED:
            skill = data.get("skill", "")
            completed_actions.append(f"  加载技能: {skill}")
        elif event_type == EventType.ACTION_COMPLETED:
            action_type = data.get("action_type", "")
            turn = data.get("turn", "?")
            obs = data.get("observation", "")
            if action_type and action_type not in ("update_plan", "final_answer"):
                completed_actions.append(f"  轮次{turn}({action_type}): {obs[:80]}")
        elif event_type == EventType.PLAN_UPDATED:
            steps = data.get("steps", [])
            done_ids = [s["id"] for s in steps if s.get("status") in ("done", "failed")]
            if done_ids:
                completed_actions.append(f"  计划步骤完成: {', '.join(done_ids)}")

    if not completed_actions:
        return []

    lines = ["已完成步骤："]
    lines.extend(completed_actions)

    # Pending steps from restored state
    if state.plan:
        pending = [
            s for s in state.plan.steps
            if s.status not in ("done", "failed")
        ]
        if pending:
            lines.append("当前待执行步骤：")
            for s in pending:
                lines.append(f"  {s.id}: {s.description} [{s.status}]")

    lines.append("请基于以上摘要继续推进任务。")
    content = "<resume>\n" + "\n".join(lines) + "\n</resume>"
    return [{"role": "user", "content": content}]
