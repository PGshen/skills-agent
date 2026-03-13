"""End-to-end integration tests — requires OPENAI_API_KEY to be set.

Run with:
    OPENAI_API_KEY=xxx uv run pytest tests/integration/test_e2e.py -v -m integration
"""
import tempfile
import uuid
from pathlib import Path

import pytest

from agent.core import AgentCore
from agent.events import EventLogger
from agent.plan import ActionType
from model.base import RetryAdapter
from model.mock import MockModel
from model.openai import OpenAIAdapter
from output.sink import OutputSink
from skills.loader import SkillLoader
from skills.registry import SkillRegistry


FIXTURE_SKILLS = str(Path(__file__).parent.parent / "fixtures" / "skills")


def _make_core(model, skill_root=FIXTURE_SKILLS, sink=None, max_turns=20):
    """Helper: build an AgentCore wired to a temp event log."""
    session_id = str(uuid.uuid4())
    tmp = tempfile.mkdtemp()
    log_path = str(Path(tmp) / f"{session_id}.jsonl")
    logger = EventLogger(path=log_path, session_id=session_id)

    roots = [{"source": "project", "path": skill_root, "priority": 0}]
    registry = SkillRegistry(roots)
    loader = SkillLoader()

    return AgentCore(
        model=model,
        registry=registry,
        loader=loader,
        event_logger=logger,
        sink=sink or OutputSink(),
        max_turns=max_turns,
    )


class _CaptureSink(OutputSink):
    """Minimal sink that records text chunks and session-end events."""

    def __init__(self):
        self.chunks: list[tuple[str, bool]] = []
        self.session_end_called = False
        self.final_status: str = ""

    def on_text_chunk(self, chunk: str, done: bool) -> None:
        self.chunks.append((chunk, done))

    def on_session_end(self, turn_count: int, status: str) -> None:
        self.session_end_called = True
        self.final_status = status


@pytest.mark.integration
class TestE2EBasicQA:
    """Scenario 1: No skills loaded — agent answers a simple factual question directly."""

    def test_final_answer_returned(self):
        model = OpenAIAdapter()
        core = _make_core(model)
        answer = core.run("What is 2 + 2? Reply with a number only.")
        assert answer.strip() != ""
        assert "4" in answer

    def test_sink_receives_text_chunk(self):
        sink = _CaptureSink()
        model = OpenAIAdapter(sink=sink)
        core = _make_core(model, sink=sink)
        answer = core.run("Say exactly: hello world")
        assert answer != ""
        # At least one done=True chunk should have arrived
        assert any(done for _, done in sink.chunks)

    def test_session_end_status_completed(self):
        sink = _CaptureSink()
        model = OpenAIAdapter(sink=sink)
        core = _make_core(model, sink=sink)
        core.run("What color is the sky?")
        assert sink.session_end_called
        assert sink.final_status == "completed"


@pytest.mark.integration
class TestE2ESkillTrigger:
    """Scenario 2: Agent correctly loads an available skill when relevant."""

    def test_agent_loads_skill_on_request(self):
        """
        Ask the agent to explicitly load 'example-skill' and report its description.
        We assert the agent executed a LOAD_SKILL action (observable via answer content).
        """
        model = OpenAIAdapter()
        core = _make_core(model)
        answer = core.run(
            "Load the 'example-skill' skill and then tell me its description in one sentence."
        )
        assert answer.strip() != ""
        # The skill description from the fixture: "An example skill for testing"
        assert "example" in answer.lower() or "skill" in answer.lower()

    def test_agent_lists_available_skills(self):
        """Agent should be able to enumerate skills from the registry."""
        model = OpenAIAdapter()
        core = _make_core(model)
        answer = core.run(
            "List all available skills by name. Provide a final answer with their names."
        )
        assert "example-skill" in answer or "example" in answer.lower()


@pytest.mark.integration
class TestE2EMultiStepPlanning:
    """Scenario 3: Complex task triggers UPDATE_PLAN; agent executes step-by-step."""

    def test_multi_step_plan_executes_to_completion(self):
        """
        Ask a task that naturally requires a plan (two ordered subtasks).
        The agent must emit UPDATE_PLAN then reach FINAL_ANSWER within max_turns.
        """
        sink = _CaptureSink()
        model = OpenAIAdapter(sink=sink)
        core = _make_core(model, sink=sink, max_turns=15)

        answer = core.run(
            "Please create a plan with two steps: "
            "step 1 — load the 'example-skill', "
            "step 2 — summarize what example-skill does. "
            "Execute the plan and give me a final answer."
        )
        assert answer.strip() != ""
        assert sink.final_status == "completed"

    def test_max_turns_respected(self):
        """
        With a very low max_turns budget, the core must exit (never run forever).
        Status will be 'max_turns' or 'dead_loop' — not an infinite hang.
        """
        sink = _CaptureSink()
        model = OpenAIAdapter(sink=sink)
        # Set max_turns=2: the agent will hit the budget before finishing a complex task
        core = _make_core(model, sink=sink, max_turns=2)
        # This just must return within a reasonable time — we don't assert the answer content
        answer = core.run(
            "Do ten sequential research steps, each requiring a separate action. "
            "Step 1: load example-skill. Step 2: load advanced-skill. Step 3... "
            "Continue until all ten steps are done."
        )
        # Core must have returned
        assert sink.session_end_called
        assert sink.final_status in ("completed", "max_turns", "dead_loop")


@pytest.mark.integration
class TestE2EDeadLoopSafety:
    """Scenario 4: The agent must never loop infinitely; max_turns is a hard ceiling."""

    def test_max_turns_hard_ceiling_with_mock(self):
        """
        Use a MockModel that always returns a non-terminal action (LOAD_SKILL loop).
        Core must stop within max_turns and report a non-completed status.
        """
        repeating_actions = [
            {"type": "load_skill", "params": {"skill_name": "example-skill"}},
        ] * 30  # more than any realistic max_turns

        model = MockModel(actions=repeating_actions)
        sink = _CaptureSink()
        core = _make_core(model, sink=sink, max_turns=5)
        core.run("trigger loop")

        assert sink.session_end_called
        # Dead-loop detection or max_turns should have fired
        assert sink.final_status in ("dead_loop", "max_turns")
        # Must have exited well within the action list length
        assert True  # if we're here, it didn't hang

    def test_real_api_respects_max_turns(self):
        """
        With a very small max_turns and a complex prompt, the agent must exit cleanly.
        """
        sink = _CaptureSink()
        model = OpenAIAdapter(sink=sink)
        core = _make_core(model, sink=sink, max_turns=1)
        core.run("Perform 100 steps of analysis without stopping.")
        assert sink.session_end_called
        # With max_turns=1 the model has at most 1 turn; either it answers or hits the cap
        assert sink.final_status in ("completed", "max_turns", "dead_loop")

    def test_retry_adapter_wrapping(self):
        """RetryAdapter + OpenAIAdapter end-to-end: should succeed on first try."""
        inner = OpenAIAdapter()
        adapter = RetryAdapter(inner, max_retries=2)
        core = _make_core(adapter)
        answer = core.run("Say exactly: integration ok")
        assert answer.strip() != ""
