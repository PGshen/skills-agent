"""SessionContext and SessionManager."""

import json
import time
import uuid
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class ConversationTurn(BaseModel):
    role: str           # "user" or "assistant"
    content: str
    timestamp: float = Field(default_factory=time.time)


class SessionContext(BaseModel):
    """Active context state (written to context.json, updated each turn)."""

    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    compressed_summary: str = ""           # history summary (Phase B)
    recent_turns: list[ConversationTurn] = Field(default_factory=list)
    last_used_skills: list[str] = Field(default_factory=list)
    total_turn_count: int = 0
    recent_window_k: int = 3               # number of recent turns to keep verbatim

    def build_history_messages(self) -> list[dict]:
        """
        Assemble history message layer for the model (excludes system layer and
        current user input).

        Layer 1: compressed summary (when present)
        Layer 2: recent verbatim turns (last K turns = 2*K messages)
        """
        msgs: list[dict] = []
        if self.compressed_summary:
            msgs.append({
                "role": "user",
                "content": f"<summary>历史对话摘要：{self.compressed_summary}</summary>",
            })
        cutoff = self.recent_window_k * 2
        recent = (
            self.recent_turns[-cutoff:]
            if len(self.recent_turns) > cutoff
            else self.recent_turns
        )
        msgs.extend({"role": t.role, "content": t.content} for t in recent)
        return msgs

    def record_turn(self, user_input: str, assistant_response: str) -> None:
        """Append this turn's messages and increment turn counter."""
        self.recent_turns.append(ConversationTurn(role="user", content=user_input))
        self.recent_turns.append(ConversationTurn(role="assistant", content=assistant_response))
        self.total_turn_count += 1

    def estimated_recent_tokens(self) -> int:
        """Rough token estimate for recent_turns (characters / 4)."""
        total_chars = sum(len(t.content) for t in self.recent_turns)
        return total_chars // 4


class SessionManager:
    """
    Session file manager.
    Handles creating, persisting, and loading session files:
      <sessions_root>/<session_id>/session.json      – immutable metadata
      <sessions_root>/<session_id>/context.json      – mutable active state
      <sessions_root>/<session_id>/conversation.jsonl – full conversation log
    """

    def __init__(self, sessions_root: str = ".agent/sessions") -> None:
        self._root = Path(sessions_root)

    def create(self, model_id: str = "mock", config: Optional[dict] = None) -> SessionContext:
        """Create a new session, write session.json, return empty SessionContext."""
        ctx = SessionContext()
        session_dir = self._root / ctx.session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "session_id": ctx.session_id,
            "created_at": time.time(),
            "model_id": model_id,
            "config_snapshot": config or {},
        }
        (session_dir / "session.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2)
        )
        return ctx

    def load(self, session_id: str) -> Optional[SessionContext]:
        """Load an existing session's context.json; return None if not found."""
        context_path = self._root / session_id / "context.json"
        if not context_path.exists():
            return None
        return SessionContext.model_validate_json(context_path.read_text())

    def save(self, ctx: SessionContext) -> None:
        """Persist context.json (overwrite)."""
        session_dir = self._root / ctx.session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "context.json").write_text(ctx.model_dump_json(indent=2))

    def append_log(self, ctx: SessionContext, user_input: str, response: str) -> None:
        """Append one turn to conversation.jsonl (full permanent record)."""
        session_dir = self._root / ctx.session_id
        entry = {
            "turn": ctx.total_turn_count,
            "timestamp": time.time(),
            "user": user_input,
            "assistant": response,
        }
        with open(session_dir / "conversation.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def list_sessions(self) -> list[dict]:
        """List metadata for all sessions (session.json)."""
        result: list[dict] = []
        if not self._root.exists():
            return result
        for session_dir in sorted(self._root.iterdir(), reverse=True):
            meta_path = session_dir / "session.json"
            if meta_path.exists():
                result.append(json.loads(meta_path.read_text()))
        return result
