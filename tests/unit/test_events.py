"""Events 数据结构单元测试"""
import json
import pytest
from datetime import datetime
from pathlib import Path

from src.agent.events import Event, EventStream, EventType


# ---------------------------------------------------------------------------
# Event 序列化 / 反序列化
# ---------------------------------------------------------------------------

class TestEventSerialization:
    def test_to_dict_contains_required_keys(self):
        event = Event(
            type=EventType.RUN_STARTED,
            run_id="run-001",
            turn=0,
            data={"request": "do something"},
        )
        d = event.to_dict()

        assert d["type"] == "run_started"
        assert d["run_id"] == "run-001"
        assert d["turn"] == 0
        assert d["data"] == {"request": "do something"}
        assert "timestamp" in d

    def test_to_dict_timestamp_is_isoformat(self):
        event = Event(
            type=EventType.TURN_STARTED,
            run_id="r1",
            turn=1,
        )
        d = event.to_dict()
        # Should be parseable as ISO format
        datetime.fromisoformat(d["timestamp"])

    def test_to_json_line_is_valid_json(self):
        event = Event(
            type=EventType.MODEL_REQUEST,
            run_id="r1",
            turn=2,
            data={"prompt": "hello"},
        )
        line = event.to_json_line()
        parsed = json.loads(line)
        assert parsed["type"] == "model_request"
        assert parsed["data"]["prompt"] == "hello"

    def test_to_json_line_no_trailing_newline(self):
        event = Event(type=EventType.RUN_FINISHED, run_id="r1", turn=5)
        line = event.to_json_line()
        assert "\n" not in line

    def test_from_dict_roundtrip(self):
        original = Event(
            type=EventType.ACTION_EXECUTED,
            run_id="run-abc",
            turn=3,
            data={"result": "ok"},
        )
        restored = Event.from_dict(original.to_dict())

        assert restored.type == original.type
        assert restored.run_id == original.run_id
        assert restored.turn == original.turn
        assert restored.data == original.data
        # Timestamps should be equal after ISO round-trip
        assert restored.timestamp.isoformat() == original.timestamp.isoformat()

    def test_from_dict_missing_data_defaults_to_empty(self):
        d = {
            "type": "plan_created",
            "run_id": "r1",
            "turn": 0,
            "timestamp": datetime.now().isoformat(),
        }
        event = Event.from_dict(d)
        assert event.data == {}

    def test_event_type_all_values_parseable(self):
        """确保所有 EventType 成员值都可从字符串重建"""
        for et in EventType:
            assert EventType(et.value) == et

    def test_default_data_field_is_empty_dict(self):
        event = Event(type=EventType.OBSERVATION_RECORDED, run_id="r", turn=0)
        assert event.data == {}

    def test_default_timestamp_set(self):
        before = datetime.now()
        event = Event(type=EventType.TURN_FINISHED, run_id="r", turn=0)
        after = datetime.now()
        assert before <= event.timestamp <= after


# ---------------------------------------------------------------------------
# EventStream 基本功能
# ---------------------------------------------------------------------------

class TestEventStreamHandlers:
    def test_emit_calls_single_handler(self):
        received: list = []
        stream = EventStream()
        stream.add_handler(received.append)

        event = Event(type=EventType.RUN_STARTED, run_id="r1", turn=0)
        stream.emit(event)

        assert len(received) == 1
        assert received[0] is event

    def test_emit_calls_multiple_handlers_in_order(self):
        order: list = []
        stream = EventStream()
        stream.add_handler(lambda e: order.append("A"))
        stream.add_handler(lambda e: order.append("B"))

        stream.emit(Event(type=EventType.TURN_STARTED, run_id="r", turn=1))
        assert order == ["A", "B"]

    def test_emit_without_handlers_does_not_raise(self):
        stream = EventStream()
        stream.emit(Event(type=EventType.ERROR_OCCURRED, run_id="r", turn=0))

    def test_remove_handler(self):
        received: list = []
        handler = received.append
        stream = EventStream()
        stream.add_handler(handler)
        stream.remove_handler(handler)

        stream.emit(Event(type=EventType.RUN_FINISHED, run_id="r", turn=0))
        assert received == []

    def test_remove_nonexistent_handler_raises(self):
        stream = EventStream()
        with pytest.raises(ValueError):
            stream.remove_handler(lambda e: None)

    def test_handler_receives_correct_event_data(self):
        captured: list = []
        stream = EventStream()
        stream.add_handler(captured.append)

        event = Event(
            type=EventType.SKILL_LOADED,
            run_id="run-xyz",
            turn=7,
            data={"skill": "example"},
        )
        stream.emit(event)

        assert captured[0].data["skill"] == "example"
        assert captured[0].run_id == "run-xyz"


# ---------------------------------------------------------------------------
# EventStream 文件写入
# ---------------------------------------------------------------------------

class TestEventStreamFileOutput:
    def test_emit_writes_jsonl_to_file(self, tmp_path):
        output_file = tmp_path / "events.jsonl"
        stream = EventStream(output_path=output_file)

        event = Event(
            type=EventType.MODEL_RESPONSE,
            run_id="r1",
            turn=2,
            data={"tokens": 42},
        )
        stream.emit(event)

        assert output_file.exists()
        lines = output_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["type"] == "model_response"
        assert parsed["data"]["tokens"] == 42

    def test_emit_appends_multiple_events(self, tmp_path):
        output_file = tmp_path / "events.jsonl"
        stream = EventStream(output_path=output_file)

        for i in range(5):
            stream.emit(Event(type=EventType.TURN_STARTED, run_id="r1", turn=i))

        lines = output_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5

    def test_emit_without_output_path_does_not_create_file(self, tmp_path):
        stream = EventStream()  # no output_path
        stream.emit(Event(type=EventType.RUN_STARTED, run_id="r1", turn=0))
        # No file should have been created
        assert list(tmp_path.iterdir()) == []

    def test_emit_writes_and_calls_handler(self, tmp_path):
        output_file = tmp_path / "events.jsonl"
        received: list = []
        stream = EventStream(output_path=output_file)
        stream.add_handler(received.append)

        event = Event(type=EventType.PLAN_CREATED, run_id="r1", turn=0)
        stream.emit(event)

        assert output_file.exists()
        assert len(received) == 1


# ---------------------------------------------------------------------------
# EventStream.replay
# ---------------------------------------------------------------------------

class TestEventStreamReplay:
    def _write_jsonl(self, path: Path, events: list[Event]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for e in events:
                f.write(e.to_json_line() + "\n")

    def test_replay_single_event(self, tmp_path):
        log_file = tmp_path / "run.jsonl"
        original = Event(
            type=EventType.RUN_STARTED,
            run_id="run-replay",
            turn=0,
            data={"request": "test"},
        )
        self._write_jsonl(log_file, [original])

        replayed = EventStream.replay(log_file)
        assert len(replayed) == 1
        assert replayed[0].type == EventType.RUN_STARTED
        assert replayed[0].run_id == "run-replay"
        assert replayed[0].data == {"request": "test"}

    def test_replay_multiple_events_in_order(self, tmp_path):
        log_file = tmp_path / "run.jsonl"
        types = [
            EventType.RUN_STARTED,
            EventType.TURN_STARTED,
            EventType.MODEL_REQUEST,
            EventType.MODEL_RESPONSE,
            EventType.RUN_FINISHED,
        ]
        events = [Event(type=t, run_id="r1", turn=i) for i, t in enumerate(types)]
        self._write_jsonl(log_file, events)

        replayed = EventStream.replay(log_file)
        assert len(replayed) == 5
        for original, restored in zip(events, replayed):
            assert restored.type == original.type
            assert restored.turn == original.turn

    def test_replay_preserves_timestamps(self, tmp_path):
        log_file = tmp_path / "run.jsonl"
        ts = datetime(2025, 1, 15, 12, 0, 0)
        event = Event(type=EventType.ACTION_EXECUTED, run_id="r1", turn=1, timestamp=ts)
        self._write_jsonl(log_file, [event])

        replayed = EventStream.replay(log_file)
        assert replayed[0].timestamp == ts

    def test_replay_via_emit_then_replay(self, tmp_path):
        """通过 stream.emit 写入文件，再用 replay 回放"""
        log_file = tmp_path / "stream.jsonl"
        stream = EventStream(output_path=log_file)

        emitted_events = [
            Event(type=EventType.RUN_STARTED, run_id="r1", turn=0),
            Event(type=EventType.SKILL_LOADED, run_id="r1", turn=1, data={"skill": "s1"}),
            Event(type=EventType.RUN_FINISHED, run_id="r1", turn=2),
        ]
        for e in emitted_events:
            stream.emit(e)

        replayed = EventStream.replay(log_file)
        assert len(replayed) == 3
        assert replayed[1].data == {"skill": "s1"}

    def test_replay_skips_blank_lines(self, tmp_path):
        log_file = tmp_path / "run.jsonl"
        event = Event(type=EventType.TURN_FINISHED, run_id="r1", turn=0)
        # Write with extra blank lines
        content = "\n" + event.to_json_line() + "\n\n"
        log_file.write_text(content, encoding="utf-8")

        replayed = EventStream.replay(log_file)
        assert len(replayed) == 1
