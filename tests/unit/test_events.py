"""Unit tests for EventType, Event, EventLogger."""

import json

import pytest

from agent.events import Event, EventLogger, EventType


def test_event_has_unique_ids():
    e1 = Event(type=EventType.SESSION_START, session_id="s1")
    e2 = Event(type=EventType.SESSION_START, session_id="s1")
    assert e1.id != e2.id


def test_event_timestamp_set_automatically():
    import time
    before = time.time()
    e = Event(type=EventType.SESSION_END, session_id="s1")
    after = time.time()
    assert before <= e.timestamp <= after


def test_event_data_defaults_to_empty_dict():
    e = Event(type=EventType.ERROR, session_id="s1")
    assert e.data == {}


def test_event_serializes_to_json():
    e = Event(type=EventType.PLAN_UPDATED, session_id="s1", data={"key": "value"})
    restored = Event.model_validate_json(e.model_dump_json())
    assert restored.type == EventType.PLAN_UPDATED
    assert restored.session_id == "s1"
    assert restored.data == {"key": "value"}
    assert restored.id == e.id


def test_event_logger_writes_jsonl(tmp_path):
    log_path = str(tmp_path / "events.jsonl")
    logger = EventLogger(path=log_path, session_id="sess-1")

    e1 = logger.emit(EventType.SESSION_START)
    e2 = logger.emit(EventType.ACTION_COMPLETED, {"result": "ok"})

    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2

    d1 = json.loads(lines[0])
    assert d1["type"] == "session_start"
    assert d1["session_id"] == "sess-1"
    assert d1["id"] == e1.id

    d2 = json.loads(lines[1])
    assert d2["type"] == "action_completed"
    assert d2["data"] == {"result": "ok"}
    assert d2["id"] == e2.id


def test_event_logger_returns_event(tmp_path):
    log_path = str(tmp_path / "events.jsonl")
    logger = EventLogger(path=log_path, session_id="sess-2")
    event = logger.emit(EventType.SKILL_LOADED, {"skill": "my-skill"})
    assert isinstance(event, Event)
    assert event.type == EventType.SKILL_LOADED
    assert event.session_id == "sess-2"


def test_event_logger_appends(tmp_path):
    log_path = str(tmp_path / "events.jsonl")
    logger = EventLogger(path=log_path, session_id="sess-3")
    for event_type in [EventType.SESSION_START, EventType.PLAN_CREATED, EventType.SESSION_END]:
        logger.emit(event_type)
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[2])["type"] == "session_end"
