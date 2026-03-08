"""Unit tests for OutputSink, NullSink, CLISink, and SSESink."""
import json
import pytest

from src.output.sink import OutputSink, NullSink
from src.output.cli_sink import CLISink
from src.output.sse_sink import SSESink
from src.agent.plan import Plan, Step, StepStatus


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def simple_plan():
    return Plan(
        goal="test goal",
        steps=[
            Step(id="s1", description="first step", status=StepStatus.PENDING),
            Step(id="s2", description="second step", status=StepStatus.IN_PROGRESS),
            Step(id="s3", description="third step", status=StepStatus.DONE),
        ],
    )


# ─── NullSink / OutputSink ────────────────────────────────────────────────────

class TestNullSink:
    def test_null_sink_is_output_sink(self):
        assert NullSink is OutputSink

    def test_null_sink_instantiable(self):
        sink = NullSink()
        assert isinstance(sink, OutputSink)

    def test_all_methods_do_nothing(self, simple_plan):
        sink = NullSink()
        # None of these should raise
        sink.on_progress("load_skill", "my-skill")
        sink.on_progress("run_script")
        sink.on_plan_updated(simple_plan)
        sink.on_text_chunk("hello", done=False)
        sink.on_text_chunk("world", done=True)
        sink.on_observation("script", "output text")
        sink.on_error("something went wrong", recoverable=True)
        sink.on_error("fatal error", recoverable=False)
        sink.on_session_end(5, "completed")

    def test_methods_return_none(self, simple_plan):
        sink = NullSink()
        assert sink.on_progress("x") is None
        assert sink.on_plan_updated(simple_plan) is None
        assert sink.on_text_chunk("hi", done=True) is None
        assert sink.on_observation("src", "content") is None
        assert sink.on_error("err") is None
        assert sink.on_session_end(1, "completed") is None


# ─── CLISink ─────────────────────────────────────────────────────────────────

class TestCLISink:
    def test_progress_goes_to_stderr(self, capsys):
        sink = CLISink(color=False)
        sink.on_progress("load_skill", "data-analysis")
        captured = capsys.readouterr()
        assert "load_skill: data-analysis" in captured.err
        assert captured.out == ""

    def test_progress_without_detail(self, capsys):
        sink = CLISink(color=False)
        sink.on_progress("run_script")
        captured = capsys.readouterr()
        assert "run_script" in captured.err
        assert captured.out == ""

    def test_text_chunk_goes_to_stdout(self, capsys):
        sink = CLISink(color=False)
        sink.on_text_chunk("hello world", done=True)
        captured = capsys.readouterr()
        assert "hello world" in captured.out

    def test_text_chunk_stdout_only(self, capsys):
        sink = CLISink(color=False)
        sink.on_text_chunk("answer text", done=True)
        captured = capsys.readouterr()
        assert "answer text" in captured.out
        # stderr should only have the blank line separator (if any)
        assert "answer text" not in captured.err

    def test_text_chunk_adds_newline_when_missing(self, capsys):
        sink = CLISink(color=False)
        sink.on_text_chunk("no newline", done=True)
        captured = capsys.readouterr()
        assert captured.out.endswith("\n")

    def test_text_chunk_no_double_newline(self, capsys):
        sink = CLISink(color=False)
        sink.on_text_chunk("has newline\n", done=True)
        captured = capsys.readouterr()
        assert captured.out == "has newline\n"

    def test_text_chunk_streaming_multiple(self, capsys):
        sink = CLISink(color=False)
        sink.on_text_chunk("chunk1", done=False)
        sink.on_text_chunk("chunk2", done=False)
        sink.on_text_chunk("chunk3", done=True)
        captured = capsys.readouterr()
        assert "chunk1chunk2chunk3" in captured.out

    def test_observation_hidden_by_default(self, capsys):
        sink = CLISink(verbose=False, color=False)
        sink.on_observation("script", "some output")
        captured = capsys.readouterr()
        assert "some output" not in captured.err
        assert captured.out == ""

    def test_observation_visible_when_verbose(self, capsys):
        sink = CLISink(verbose=True, color=False)
        sink.on_observation("script", "some output")
        captured = capsys.readouterr()
        assert "some output" in captured.err
        assert captured.out == ""

    def test_observation_truncates_long_content(self, capsys):
        sink = CLISink(verbose=True, color=False)
        long_content = "x" * 500
        sink.on_observation("script", long_content)
        captured = capsys.readouterr()
        assert "..." in captured.err
        # Should be truncated, not the full 500 chars raw
        assert len(captured.err) < 500

    def test_error_recoverable_goes_to_stderr(self, capsys):
        sink = CLISink(color=False)
        sink.on_error("something failed", recoverable=True)
        captured = capsys.readouterr()
        assert "something failed" in captured.err
        assert "[WARN]" in captured.err
        assert captured.out == ""

    def test_error_fatal_goes_to_stderr(self, capsys):
        sink = CLISink(color=False)
        sink.on_error("fatal crash", recoverable=False)
        captured = capsys.readouterr()
        assert "fatal crash" in captured.err
        assert "[ERROR]" in captured.err
        assert captured.out == ""

    def test_session_end_to_stderr(self, capsys):
        sink = CLISink(color=False)
        sink.on_session_end(7, "completed")
        captured = capsys.readouterr()
        assert "7" in captured.err
        assert "completed" in captured.err
        assert captured.out == ""

    def test_plan_updated_to_stderr(self, capsys, simple_plan):
        sink = CLISink(color=False)
        sink.on_plan_updated(simple_plan)
        captured = capsys.readouterr()
        assert "first step" in captured.err
        assert "second step" in captured.err
        assert "third step" in captured.err
        assert captured.out == ""

    def test_plan_shows_step_icons(self, capsys):
        plan = Plan(
            goal="g",
            steps=[
                Step(id="s1", description="pending", status=StepStatus.PENDING),
                Step(id="s2", description="done", status=StepStatus.DONE),
                Step(id="s3", description="failed", status=StepStatus.FAILED),
            ],
        )
        sink = CLISink(color=False)
        sink.on_plan_updated(plan)
        captured = capsys.readouterr()
        assert "○" in captured.err   # PENDING
        assert "✓" in captured.err   # DONE
        assert "✗" in captured.err   # FAILED

    def test_no_exception_on_any_method(self, simple_plan):
        sink = CLISink(verbose=True, color=False)
        # All methods must not raise
        sink.on_progress("action", "detail")
        sink.on_plan_updated(simple_plan)
        sink.on_text_chunk("text", done=True)
        sink.on_observation("src", "content")
        sink.on_error("err", recoverable=True)
        sink.on_session_end(3, "failed")


# ─── SSESink ─────────────────────────────────────────────────────────────────

class TestSSESink:
    def _make_sink(self):
        events = []
        sink = SSESink(write_fn=events.append)
        return sink, events

    def _parse(self, event: str) -> dict:
        assert event.startswith("data: "), f"Expected 'data: ' prefix: {event!r}"
        assert event.endswith("\n\n"), f"Expected '\\n\\n' suffix: {event!r}"
        return json.loads(event[6:])

    def test_sse_event_format(self):
        sink, events = self._make_sink()
        sink.on_progress("load_skill", "data-analysis")
        assert len(events) == 1
        assert events[0].startswith("data: ")
        assert events[0].endswith("\n\n")

    def test_progress_event(self):
        sink, events = self._make_sink()
        sink.on_progress("load_skill", "data-analysis")
        data = self._parse(events[0])
        assert data == {"type": "progress", "action": "load_skill", "detail": "data-analysis"}

    def test_progress_event_no_detail(self):
        sink, events = self._make_sink()
        sink.on_progress("run_script")
        data = self._parse(events[0])
        assert data["type"] == "progress"
        assert data["action"] == "run_script"
        assert data["detail"] == ""

    def test_text_chunk_event(self):
        sink, events = self._make_sink()
        sink.on_text_chunk("hello", done=False)
        data = self._parse(events[0])
        assert data["type"] == "text_chunk"
        assert data["content"] == "hello"

    def test_text_done_event(self):
        sink, events = self._make_sink()
        sink.on_text_chunk("", done=True)
        data = self._parse(events[0])
        assert data["type"] == "text_done"
        assert "content" not in data

    def test_observation_event(self):
        sink, events = self._make_sink()
        sink.on_observation("script", "output content")
        data = self._parse(events[0])
        assert data["type"] == "observation"
        assert data["source"] == "script"
        assert data["content"] == "output content"

    def test_observation_truncates_at_500(self):
        sink, events = self._make_sink()
        long = "x" * 600
        sink.on_observation("script", long)
        data = self._parse(events[0])
        assert len(data["content"]) == 500

    def test_error_event_recoverable(self):
        sink, events = self._make_sink()
        sink.on_error("something broke", recoverable=True)
        data = self._parse(events[0])
        assert data["type"] == "error"
        assert data["message"] == "something broke"
        assert data["recoverable"] is True

    def test_error_event_fatal(self):
        sink, events = self._make_sink()
        sink.on_error("fatal", recoverable=False)
        data = self._parse(events[0])
        assert data["recoverable"] is False

    def test_session_end_event(self):
        sink, events = self._make_sink()
        sink.on_session_end(5, "completed")
        data = self._parse(events[0])
        assert data["type"] == "session_end"
        assert data["turn_count"] == 5
        assert data["status"] == "completed"

    def test_plan_updated_event(self, simple_plan):
        sink, events = self._make_sink()
        sink.on_plan_updated(simple_plan)
        data = self._parse(events[0])
        assert data["type"] == "plan_updated"
        assert data["goal"] == "test goal"
        assert len(data["steps"]) == 3
        assert data["steps"][0]["id"] == "s1"
        assert data["steps"][0]["description"] == "first step"
        assert data["steps"][0]["status"] == "pending"

    def test_plan_step_includes_notes(self):
        plan = Plan(
            goal="g",
            steps=[Step(id="s1", description="step", status=StepStatus.DONE, notes="note here")],
        )
        sink, events = self._make_sink()
        sink.on_plan_updated(plan)
        data = self._parse(events[0])
        assert data["steps"][0]["notes"] == "note here"

    def test_multiple_events_sequence(self):
        sink, events = self._make_sink()
        sink.on_progress("load_skill", "data-analysis")
        sink.on_text_chunk("hello", done=False)
        sink.on_text_chunk("", done=True)
        sink.on_session_end(3, "completed")

        assert len(events) == 4
        for event in events:
            assert event.startswith("data: ")
            assert event.endswith("\n\n")

        types = [self._parse(e)["type"] for e in events]
        assert types == ["progress", "text_chunk", "text_done", "session_end"]

    def test_unicode_content(self):
        sink, events = self._make_sink()
        sink.on_text_chunk("你好世界", done=False)
        data = self._parse(events[0])
        assert data["content"] == "你好世界"

    def test_no_exception_on_any_method(self, simple_plan):
        sink, events = self._make_sink()
        sink.on_progress("action", "detail")
        sink.on_plan_updated(simple_plan)
        sink.on_text_chunk("text", done=False)
        sink.on_text_chunk("", done=True)
        sink.on_observation("src", "content")
        sink.on_error("err", recoverable=True)
        sink.on_session_end(3, "failed")
        assert len(events) == 7
