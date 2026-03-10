"""Unit tests for ConversationCompressor (B5.1)."""
import pytest

from agent.plan import Action, ActionType
from model.mock import MockModel
from session.compressor import ConversationCompressor
from session.session import SessionContext


def test_no_compress_when_below_threshold():
    mock = MockModel(actions=[])
    compressor = ConversationCompressor(
        model=mock,
        threshold_ratio=1.0,  # 设很高，不触发
        context_limit_tokens=100_000,
    )
    ctx = SessionContext()
    ctx.record_turn("hello", "hi")
    result = compressor.maybe_compress(ctx)
    assert result is False
    assert mock.call_count == 0


def test_compress_oldest_turns():
    mock = MockModel(actions=[
        Action(type=ActionType.FINAL_ANSWER, params={"content": "摘要内容"})
    ])
    compressor = ConversationCompressor(
        model=mock,
        threshold_ratio=0.0,  # 强制触发
        context_limit_tokens=100_000,
        compress_oldest_m=1,
    )
    ctx = SessionContext()
    ctx.record_turn("问题1", "回答1")
    ctx.record_turn("问题2", "回答2")

    result = compressor.maybe_compress(ctx)
    assert result is True
    assert ctx.compressed_summary == "摘要内容"
    assert len(ctx.recent_turns) == 2  # 只剩第2轮（2条消息）


def test_compress_appends_to_existing_summary():
    mock = MockModel(actions=[
        Action(type=ActionType.FINAL_ANSWER, params={"content": "新摘要"})
    ])
    compressor = ConversationCompressor(
        model=mock,
        threshold_ratio=0.0,
        context_limit_tokens=100_000,
        compress_oldest_m=1,
    )
    ctx = SessionContext()
    ctx.compressed_summary = "旧摘要"
    ctx.record_turn("问题1", "回答1")
    ctx.record_turn("问题2", "回答2")

    compressor.maybe_compress(ctx)
    assert ctx.compressed_summary == "旧摘要\n新摘要"


def test_compress_model_fallback_on_missing_content():
    mock = MockModel(actions=[
        Action(type=ActionType.FINAL_ANSWER, params={})  # no "content" key
    ])
    compressor = ConversationCompressor(
        model=mock,
        threshold_ratio=0.0,
        context_limit_tokens=100_000,
        compress_oldest_m=1,
    )
    ctx = SessionContext()
    ctx.record_turn("问题1", "回答1")

    compressor.maybe_compress(ctx)
    assert ctx.compressed_summary == "（摘要生成失败）"


def test_compress_removes_correct_number_of_turns():
    mock = MockModel(actions=[
        Action(type=ActionType.FINAL_ANSWER, params={"content": "摘要"})
    ])
    compressor = ConversationCompressor(
        model=mock,
        threshold_ratio=0.0,
        context_limit_tokens=100_000,
        compress_oldest_m=2,
    )
    ctx = SessionContext()
    for i in range(4):
        ctx.record_turn(f"问题{i}", f"回答{i}")
    # 4 turns = 8 messages; compress_oldest_m=2 removes 4 messages
    compressor.maybe_compress(ctx)
    assert len(ctx.recent_turns) == 4


def test_no_compress_when_turns_empty():
    mock = MockModel(actions=[])
    compressor = ConversationCompressor(
        model=mock,
        threshold_ratio=0.0,
        context_limit_tokens=100_000,
    )
    ctx = SessionContext()
    # No turns: estimated_recent_tokens() == 0, which is <= threshold (0)
    result = compressor.maybe_compress(ctx)
    assert result is False
    assert mock.call_count == 0
