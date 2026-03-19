"""Unit tests for SessionContext and SessionManager."""

import json

import pytest

from session.session import ConversationTurn, SessionContext, SessionManager


# ─── ConversationTurn ─────────────────────────────────────────────────────────

class TestConversationTurn:
    def test_basic_fields(self):
        t = ConversationTurn(role="user", content="hello")
        assert t.role == "user"
        assert t.content == "hello"
        assert t.timestamp > 0

    def test_timestamp_auto_set(self):
        import time
        before = time.time()
        t = ConversationTurn(role="assistant", content="hi")
        after = time.time()
        assert before <= t.timestamp <= after


# ─── SessionContext ───────────────────────────────────────────────────────────

class TestSessionContext:
    def test_default_fields(self):
        ctx = SessionContext()
        assert ctx.compressed_summary == ""
        assert ctx.recent_turns == []
        assert ctx.last_used_skills == []
        assert ctx.total_turn_count == 0
        assert ctx.recent_window_k == 5
        assert len(ctx.session_id) == 36  # UUID format

    def test_unique_session_ids(self):
        ctx1 = SessionContext()
        ctx2 = SessionContext()
        assert ctx1.session_id != ctx2.session_id

    def test_record_turn_appends_messages(self):
        ctx = SessionContext()
        ctx.record_turn("hello", "world")
        assert len(ctx.recent_turns) == 2
        assert ctx.recent_turns[0].role == "user"
        assert ctx.recent_turns[0].content == "hello"
        assert ctx.recent_turns[1].role == "assistant"
        assert ctx.recent_turns[1].content == "world"

    def test_record_turn_increments_counter(self):
        ctx = SessionContext()
        ctx.record_turn("a", "b")
        assert ctx.total_turn_count == 1
        ctx.record_turn("c", "d")
        assert ctx.total_turn_count == 2

    def test_build_history_messages_empty(self):
        ctx = SessionContext()
        msgs = ctx.build_history_messages()
        assert msgs == []

    def test_build_history_messages_with_turns(self):
        ctx = SessionContext()
        ctx.record_turn("hello", "world")
        msgs = ctx.build_history_messages()
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "hello"}
        assert msgs[1] == {"role": "assistant", "content": "world"}

    def test_build_history_messages_respects_window(self):
        ctx = SessionContext(recent_window_k=1)  # keep 1 turn = 2 messages
        ctx.record_turn("first", "answer1")
        ctx.record_turn("second", "answer2")
        msgs = ctx.build_history_messages()
        # Only the most recent 1 turn (2 messages) should be included
        assert len(msgs) == 2
        assert msgs[0]["content"] == "second"
        assert msgs[1]["content"] == "answer2"

    def test_build_history_messages_with_summary(self):
        ctx = SessionContext()
        ctx.compressed_summary = "Prior context summary"
        ctx.record_turn("hello", "world")
        msgs = ctx.build_history_messages()
        assert msgs[0]["role"] == "user"
        assert "Prior context summary" in msgs[0]["content"]
        assert len(msgs) == 3  # summary + user + assistant

    def test_build_history_messages_no_summary_when_empty(self):
        ctx = SessionContext()
        ctx.record_turn("hello", "world")
        msgs = ctx.build_history_messages()
        # No summary message when compressed_summary is empty string
        assert not any("<summary>" in m["content"] for m in msgs)

    def test_estimated_recent_tokens_empty(self):
        ctx = SessionContext()
        assert ctx.estimated_recent_tokens() == 0

    def test_estimated_recent_tokens_approximate(self):
        ctx = SessionContext()
        ctx.record_turn("a" * 400, "b" * 400)  # 800 chars total
        tokens = ctx.estimated_recent_tokens()
        assert tokens == 200  # 800 // 4

    def test_window_exactly_at_boundary(self):
        ctx = SessionContext(recent_window_k=2)  # keep 2 turns = 4 messages
        for i in range(2):
            ctx.record_turn(f"q{i}", f"a{i}")
        msgs = ctx.build_history_messages()
        assert len(msgs) == 4  # exactly at window — all included

    def test_window_exceeds_stored(self):
        ctx = SessionContext(recent_window_k=10)
        ctx.record_turn("only", "one")
        msgs = ctx.build_history_messages()
        assert len(msgs) == 2  # fewer turns than window


# ─── SessionManager ───────────────────────────────────────────────────────────

class TestSessionManager:
    def test_create_returns_session_context(self, tmp_path):
        mgr = SessionManager(sessions_root=str(tmp_path))
        ctx = mgr.create()
        assert isinstance(ctx, SessionContext)
        assert ctx.total_turn_count == 0

    def test_create_writes_session_json(self, tmp_path):
        mgr = SessionManager(sessions_root=str(tmp_path))
        ctx = mgr.create(model_id="test-model")
        session_file = tmp_path / ctx.session_id / "session.json"
        assert session_file.exists()
        meta = json.loads(session_file.read_text())
        assert meta["session_id"] == ctx.session_id
        assert meta["model_id"] == "test-model"

    def test_create_config_snapshot(self, tmp_path):
        mgr = SessionManager(sessions_root=str(tmp_path))
        ctx = mgr.create(config={"key": "val"})
        meta = json.loads((tmp_path / ctx.session_id / "session.json").read_text())
        assert meta["config_snapshot"] == {"key": "val"}

    def test_save_writes_context_json(self, tmp_path):
        mgr = SessionManager(sessions_root=str(tmp_path))
        ctx = mgr.create()
        ctx.record_turn("hi", "hello")
        mgr.save(ctx)
        context_file = tmp_path / ctx.session_id / "context.json"
        assert context_file.exists()
        loaded = json.loads(context_file.read_text())
        assert loaded["total_turn_count"] == 1

    def test_load_returns_none_for_missing(self, tmp_path):
        mgr = SessionManager(sessions_root=str(tmp_path))
        result = mgr.load("nonexistent-id")
        assert result is None

    def test_load_restores_context(self, tmp_path):
        mgr = SessionManager(sessions_root=str(tmp_path))
        ctx = mgr.create()
        ctx.record_turn("q1", "a1")
        mgr.save(ctx)

        restored = mgr.load(ctx.session_id)
        assert restored is not None
        assert restored.session_id == ctx.session_id
        assert restored.total_turn_count == 1
        assert len(restored.recent_turns) == 2

    def test_append_log_creates_jsonl(self, tmp_path):
        mgr = SessionManager(sessions_root=str(tmp_path))
        ctx = mgr.create()
        ctx.record_turn("hello", "world")
        mgr.append_log(ctx, "hello", "world")
        log_file = tmp_path / ctx.session_id / "conversation.jsonl"
        assert log_file.exists()
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["user"] == "hello"
        assert entry["assistant"] == "world"

    def test_append_log_accumulates(self, tmp_path):
        mgr = SessionManager(sessions_root=str(tmp_path))
        ctx = mgr.create()
        ctx.record_turn("q1", "a1")
        mgr.append_log(ctx, "q1", "a1")
        ctx.record_turn("q2", "a2")
        mgr.append_log(ctx, "q2", "a2")
        log_file = tmp_path / ctx.session_id / "conversation.jsonl"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_list_sessions_empty_root(self, tmp_path):
        mgr = SessionManager(sessions_root=str(tmp_path / "nonexistent"))
        assert mgr.list_sessions() == []

    def test_list_sessions_returns_metadata(self, tmp_path):
        mgr = SessionManager(sessions_root=str(tmp_path))
        ctx1 = mgr.create(model_id="m1")
        ctx2 = mgr.create(model_id="m2")
        sessions = mgr.list_sessions()
        assert len(sessions) == 2
        ids = {s["session_id"] for s in sessions}
        assert ctx1.session_id in ids
        assert ctx2.session_id in ids

    def test_roundtrip_preserves_history(self, tmp_path):
        mgr = SessionManager(sessions_root=str(tmp_path))
        ctx = mgr.create()
        ctx.record_turn("question one", "answer one")
        ctx.record_turn("question two", "answer two")
        mgr.save(ctx)

        restored = mgr.load(ctx.session_id)
        assert restored is not None
        msgs = restored.build_history_messages()
        contents = [m["content"] for m in msgs]
        assert "question one" in contents
        assert "answer one" in contents
        assert "question two" in contents
        assert "answer two" in contents
