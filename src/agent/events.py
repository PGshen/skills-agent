"""EventType, Event, EventLogger."""

import time
import uuid
from enum import Enum

from pydantic import BaseModel, Field


class EventType(str, Enum):
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    PLAN_CREATED = "plan_created"
    PLAN_UPDATED = "plan_updated"
    ACTION_REQUESTED = "action_requested"
    ACTION_COMPLETED = "action_completed"
    ACTION_FAILED = "action_failed"
    SKILL_LOADED = "skill_loaded"
    FINAL_ANSWER = "final_answer"
    DEAD_LOOP_DETECTED = "dead_loop_detected"
    ERROR = "error"


class Event(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: EventType
    timestamp: float = Field(default_factory=time.time)
    session_id: str
    data: dict = Field(default_factory=dict)


class EventLogger:
    def __init__(self, path: str, session_id: str):
        self.path = path
        self.session_id = session_id

    def emit(self, event_type: EventType, data: dict = None) -> Event:
        event = Event(type=event_type, session_id=self.session_id, data=data or {})
        with open(self.path, "a") as f:
            f.write(event.model_dump_json() + "\n")
        return event
