"""Unit tests for agent.recovery (B4.1)."""

import json
from pathlib import Path

import pytest

from agent.events import Event, EventLogger, EventType
from agent.plan import Plan, Step, StepStatus
from agent.recovery import build_resume_context, load_state, save_state
from agent.state import AgentState
from skills.frontmatter import SkillFrontmatter
from skills.metadata import SkillMetadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(session_id: str = "test-session") -> AgentState:
    return AgentState(session_id=session_id, user_input="test query")


def _make_state_with_plan(session_id: str = "test-session") -> AgentState:
    state = _make_state(session_id)
    state.plan = Plan(
        goal="do something",
        steps=[
            Step(id="step-1", description="Load skill", status=StepStatus.DONE),
            Step(id="step-2", description="Run script", status=StepStatus.PENDING),
        ],
    )
    return state


def _make_skill_metadata(name: str = "example-skill") -> SkillMetadata:
    return SkillMetadata(
        name=name,
        description="An example skill",
        source="project",
        skill_path="/tmp/skills/example-skill/SKILL.md",
    )


# ---------------------------------------------------------------------------
# load_state / save_state
# ---------------------------------------------------------------------------

class TestSaveLoadState:
    def test_save_and_load_roundtrip(self, tmp_path: Path):
        state = _make_state_with_plan()
        state.active_skills.append(_make_skill_metadata())

        save_state(tmp_path, state)

        assert (tmp_path / "state.json").exists()
        restored = load_state(tmp_path)

        assert restored is not None
        assert restored.session_id == state.session_id
        assert restored.user_input == state.user_input
        assert restored.plan is not None
        assert len(restored.plan.steps) == 2
        assert restored.plan.steps[0].status == StepStatus.DONE
        assert len(restored.active_skills) == 1
        assert restored.active_skills[0].name == "example-skill"

    def test_load_state_missing_file_returns_none(self, tmp_path: Path):
        result = load_state(tmp_path)
        assert result is None

    def test_load_state_invalid_json_returns_none(self, tmp_path: Path):
        (tmp_path / "state.json").write_text("not valid json {{{")
        result = load_state(tmp_path)
        assert result is None

    def test_save_creates_parent_dirs(self, tmp_path: Path):
        nested = tmp_path / "deep" / "nested"
        state = _make_state()
        save_state(nested, state)
        assert (nested / "state.json").exists()

    def test_load_state_preserves_status(self, tmp_path: Path):
        state = _make_state()
        state.status = "completed"
        state.turn_count = 5
        state.dead_loop_triggered = True

        save_state(tmp_path, state)
        restored = load_state(tmp_path)

        assert restored.status == "completed"
        assert restored.turn_count == 5
        assert restored.dead_loop_triggered is True


# ---------------------------------------------------------------------------
# build_resume_context
# ---------------------------------------------------------------------------

class TestBuildResumeContext:
    def _write_events(self, run_dir: Path, session_id: str, events: list[dict]) -> None:
        logger = EventLogger(path=str(run_dir / "events.jsonl"), session_id=session_id)
        for e in events:
            logger.emit(e["type"], e.get("data", {}))

    def test_no_events_file_returns_empty(self, tmp_path: Path):
        state = _make_state()
        result = build_resume_context(tmp_path, state)
        assert result == []

    def test_empty_events_file_returns_empty(self, tmp_path: Path):
        (tmp_path / "events.jsonl").write_text("")
        state = _make_state()
        result = build_resume_context(tmp_path, state)
        assert result == []

    def test_returns_resume_message_with_skill_loaded(self, tmp_path: Path):
        self._write_events(tmp_path, "s1", [
            {"type": EventType.SKILL_LOADED, "data": {"skill": "example-skill"}},
        ])
        state = _make_state()
        result = build_resume_context(tmp_path, state)

        assert len(result) == 1
        msg = result[0]
        assert msg["role"] == "user"
        assert "<resume>" in msg["content"]
        assert "example-skill" in msg["content"]
        assert "</resume>" in msg["content"]

    def test_includes_pending_steps(self, tmp_path: Path):
        self._write_events(tmp_path, "s1", [
            {"type": EventType.SKILL_LOADED, "data": {"skill": "some-skill"}},
        ])
        state = _make_state_with_plan()
        result = build_resume_context(tmp_path, state)

        assert len(result) == 1
        content = result[0]["content"]
        assert "step-2" in content      # pending step
        assert "Run script" in content

    def test_ignores_corrupted_lines(self, tmp_path: Path):
        events_path = tmp_path / "events.jsonl"
        events_path.write_text(
            '{"type": "skill_loaded", "data": {"skill": "ok-skill"}, "session_id": "s1", "id": "x", "timestamp": 1}\n'
            'NOT_JSON\n'
            '\n'
        )
        state = _make_state()
        result = build_resume_context(tmp_path, state)
        # should still produce output from the valid line
        assert len(result) == 1
        assert "ok-skill" in result[0]["content"]

    def test_action_completed_events_appear_in_summary(self, tmp_path: Path):
        events_path = tmp_path / "events.jsonl"
        event = {
            "type": "action_completed",
            "session_id": "s1",
            "id": "abc",
            "timestamp": 1.0,
            "data": {
                "turn": 2,
                "action_type": "load_skill",
                "observation": "skill body loaded",
            },
        }
        events_path.write_text(json.dumps(event) + "\n")
        state = _make_state()
        result = build_resume_context(tmp_path, state)

        assert len(result) == 1
        content = result[0]["content"]
        assert "load_skill" in content
        assert "skill body loaded" in content


# ---------------------------------------------------------------------------
# Integration: AgentCore persists state after plan update
# ---------------------------------------------------------------------------

class TestAgentCorePersistence:
    def test_state_json_written_after_plan_update(self, tmp_path: Path):
        from agent.core import AgentCore
        from agent.events import EventLogger
        from agent.plan import ActionType
        from model.mock import MockModel
        from skills.loader import SkillLoader
        from skills.registry import SkillRegistry

        run_dir = tmp_path / "runs" / "sess1"
        logger = EventLogger(
            path=str(run_dir / "events.jsonl"), session_id="sess1"
        )
        run_dir.mkdir(parents=True, exist_ok=True)

        registry = SkillRegistry([])
        loader = SkillLoader()

        actions = [
            {
                "type": "update_plan",
                "params": {
                    "plan": {
                        "goal": "test goal",
                        "steps": [{"id": "s1", "description": "step one"}],
                    }
                },
            },
            {"type": "final_answer", "params": {"content": "done"}},
        ]
        model = MockModel(actions=actions)
        core = AgentCore(
            model=model,
            registry=registry,
            loader=loader,
            event_logger=logger,
            run_dir=run_dir,
        )

        core.run("do a task")

        state_path = run_dir / "state.json"
        assert state_path.exists()
        restored = load_state(run_dir)
        assert restored is not None
        assert restored.plan is not None
        assert restored.plan.goal == "test goal"

    def test_no_persistence_without_run_dir(self, tmp_path: Path):
        """AgentCore without run_dir should not write state.json."""
        from agent.core import AgentCore
        from agent.events import EventLogger
        from model.mock import MockModel
        from skills.loader import SkillLoader
        from skills.registry import SkillRegistry

        logger = EventLogger(
            path=str(tmp_path / "events.jsonl"), session_id="s"
        )
        registry = SkillRegistry([])
        loader = SkillLoader()
        model = MockModel(actions=[
            {"type": "update_plan", "params": {"plan": {"goal": "g", "steps": []}}},
            {"type": "final_answer", "params": {"content": "done"}},
        ])
        core = AgentCore(
            model=model, registry=registry, loader=loader, event_logger=logger
        )
        core.run("query")

        # No state.json should be created anywhere near tmp_path
        assert not (tmp_path / "state.json").exists()
